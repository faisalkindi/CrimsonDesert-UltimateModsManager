"""PAMT index parser for Crimson Desert PAZ archives.

Parses .pamt files to discover file entries, their locations in PAZ archives,
sizes, and compression info.

Usage:
    python paz_parse.py <file.pamt> [--paz-dir <dir>] [--filter <pattern>]

Library usage:
    from paz_parse import parse_pamt
    entries = parse_pamt("0.pamt", paz_dir="./0003")
    for e in entries:
        print(e.path, e.comp_size, e.orig_size)
"""

import logging
import os
import struct
import fnmatch
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PazEntry:
    """A single file entry in a PAZ archive."""
    path: str           # Full path within the archive
    paz_file: str       # Path to the .paz file containing this entry
    offset: int         # Byte offset within the PAZ file
    comp_size: int      # Compressed/stored size in the PAZ
    orig_size: int      # Original decompressed size
    flags: int          # Raw PAMT flags
    paz_index: int      # PAZ file index (from flags & 0xFFFF, u16 chunk id)
    _encrypted_override: bool | None = field(default=None, repr=False)

    @property
    def compressed(self) -> bool:
        return self.comp_size != self.orig_size

    @property
    def compression_type(self) -> int:
        """0=none, 2=LZ4, 3=custom, 4=zlib"""
        return (self.flags >> 16) & 0x0F

    @property
    def encrypted(self) -> bool:
        """Whether this entry is ChaCha20-encrypted.

        The PAMT has no reliable encrypted flag. The engine encrypts
        text formats inside ui/xml/ — XML, CSS, HTML, JS — and the
        heuristic must catch all of them. v2.1.2 originally fixed
        Dark Mode Map (CSS crash on map open) by widening this past
        XML-only; the v3.0 rewrite silently narrowed it back to
        '.xml' alone, regressing the fix. Bug report from
        TheUnLuckyOnes 2026-04-26 caught it.

        When extraction detects actual encryption, set
        _encrypted_override = True so repack re-encrypts correctly.
        """
        if self._encrypted_override is not None:
            return self._encrypted_override
        return self.path.lower().endswith(
            ('.xml', '.css', '.html', '.js'))


_MAX_SANE_PAZ_COUNT = 4096


def parse_pamt(pamt_path: str, paz_dir: str = None, *,
               full_paths: bool = False) -> list[PazEntry]:
    """Parse a .pamt index file and return all file entries.

    Args:
        pamt_path: path to the .pamt file
        paz_dir: directory containing .paz files (default: same dir as .pamt)
        full_paths: resolve folder records instead of the historical root prefix

    Returns:
        list of PazEntry

    Raises:
        ValueError: the PAMT is truncated, claims a section larger than
            the file itself, or declares a wildly impossible paz_count.
            Message names the file so callers can surface which mod
            shipped the corrupt archive.
    """
    try:
        return _parse_pamt_impl(pamt_path, paz_dir, full_paths=full_paths)
    except ValueError as e:
        if str(e).startswith(f"Corrupt PAMT {os.path.basename(pamt_path)}:"):
            raise
        raise ValueError(f"Corrupt PAMT {os.path.basename(pamt_path)}: {e}") from e
    except (struct.error, IndexError, UnicodeError) as e:
        # Surface low-level parse errors as a clean ValueError. Callers
        # (import_handler precheck, apply_engine precheck) need one
        # exception type to distinguish "corrupt archive, skip this
        # mod" from actual bugs.
        raise ValueError(
            f"Corrupt PAMT {os.path.basename(pamt_path)}: {e}") from e


def _bounds_check(pamt_path: str, what: str, declared: int,
                  limit: int) -> None:
    """Raise ValueError naming the file if ``declared`` exceeds ``limit``."""
    if declared > limit:
        raise ValueError(
            f"Corrupt PAMT {os.path.basename(pamt_path)}: {what} "
            f"exceeds file size ({declared} > {limit})")


def _read_name_tree(data: bytes, start: int, end: int, compact: bool) -> dict:
    """Read either absolute-u32 parents or 2.04 backwards-varint parents.

    References are byte offsets within the section in both formats. A zero
    distance is the compact root sentinel; it is not a self-reference.
    """
    nodes = {}
    pos = start
    while pos < end:
        rel = pos - start
        if compact:
            distance = 0
            for shift in range(0, 35, 7):
                if pos >= end:
                    raise ValueError('Corrupt PAMT: truncated parent varint')
                value = data[pos]
                pos += 1
                distance |= (value & 0x7F) << shift
                if not value & 0x80:
                    break
            else:
                raise ValueError('Corrupt PAMT: oversized parent varint')
            parent = rel - distance if distance else 0xFFFFFFFF
            if distance and parent not in nodes:
                raise ValueError('Corrupt PAMT: parent is not an earlier tree node')
        else:
            if pos + 4 > end:
                raise ValueError('Corrupt PAMT: truncated tree parent')
            parent = struct.unpack_from('<I', data, pos)[0]
            pos += 4
        if pos >= end or pos + 1 + data[pos] > end:
            raise ValueError('Corrupt PAMT: truncated tree name')
        length = data[pos]
        pos += 1
        name = data[pos:pos + length].decode('utf-8')
        pos += length
        nodes[rel] = (parent, name)
    return nodes


def _parse_pamt_impl(pamt_path: str, paz_dir: str = None, *,
                     full_paths: bool = False) -> list[PazEntry]:
    with open(pamt_path, 'rb') as f:
        data = f.read()

    if paz_dir is None:
        paz_dir = os.path.dirname(pamt_path) or '.'

    pamt_stem = os.path.splitext(os.path.basename(pamt_path))[0]

    # Minimum useful header = magic(4) + paz_count(4) + hash+zero(8)
    # + folder_size(4) + node_size(4) + folder_count(4) + file_count(4)
    # = 32 bytes.
    if len(data) < 32:
        raise ValueError(
            f"Corrupt PAMT {os.path.basename(pamt_path)}: truncated "
            f"(file is {len(data)} bytes, need at least 32)")

    off = 0
    off += 4  # skip magic (varies between game versions)

    paz_count = struct.unpack_from('<I', data, off)[0]; off += 4
    if paz_count > _MAX_SANE_PAZ_COUNT:
        raise ValueError(
            f"Corrupt PAMT {os.path.basename(pamt_path)}: paz_count "
            f"{paz_count} is implausibly large (max {_MAX_SANE_PAZ_COUNT})")
    off += 8  # hash + zero

    # PAZ table
    for i in range(paz_count):
        off += 4  # hash
        off += 4  # size
        if i < paz_count - 1:
            off += 4  # separator

    # Folder section
    folder_size = struct.unpack_from('<I', data, off)[0]; off += 4
    _bounds_check(pamt_path, "folder_size", folder_size, len(data))
    folder_end = off + folder_size
    if folder_end > len(data):
        raise ValueError(
            f"Corrupt PAMT {os.path.basename(pamt_path)}: folder "
            f"section extends past end of file ({folder_end} > {len(data)})")
    compact = bool(folder_size and data[off] == 0)
    folders = _read_name_tree(data, off, folder_end, compact)
    folder_prefix = ""
    for parent, name in folders.values():
        if parent == 0xFFFFFFFF:
            folder_prefix = name
    off = folder_end

    # Node section (path tree)
    node_size = struct.unpack_from('<I', data, off)[0]; off += 4
    _bounds_check(pamt_path, "node_size", node_size, len(data))
    node_start = off
    if node_start + node_size > len(data):
        raise ValueError(
            f"Corrupt PAMT {os.path.basename(pamt_path)}: node section "
            f"extends past end of file "
            f"({node_start + node_size} > {len(data)})")
    if not folder_size and node_size:
        compact = data[node_start] == 0
    nodes = _read_name_tree(data, node_start, node_start + node_size, compact)
    off = node_start + node_size

    def build_path(tree, node_ref):
        parts = []
        cur = node_ref
        while cur != 0xFFFFFFFF and len(parts) < 64:
            if cur not in tree:
                if compact:
                    raise ValueError('Corrupt PAMT: invalid name tree reference')
                break  # Preserve legacy handling of incomplete name trees.
            p, n = tree[cur]
            parts.append(n)
            cur = p
        if compact and cur != 0xFFFFFFFF:
            raise ValueError('Corrupt PAMT: cyclic or excessively deep name tree')
        return ''.join(reversed(parts))

    # Folder record section
    folder_count = struct.unpack_from('<I', data, off)[0]; off += 4
    folder_records_start = off
    off += folder_count * 16  # skip folder records (16 bytes each)
    # 2.04 emits only its empty extension table for an empty archive: the
    # zero file-count field is omitted. Legacy empty indexes keep that field.
    if (not paz_count and not folder_size and not node_size and not folder_count
            and data[off:] == b'\x01\x00\x00\x00\x00'):
        return []
    _bounds_check(pamt_path, 'folder records', off + 4, len(data))

    # File record section
    file_count = struct.unpack_from('<I', data, off)[0]; off += 4
    records_end = off + file_count * 20
    _bounds_check(pamt_path, 'file records', records_end, len(data))
    file_folders = {}
    if full_paths:
        for pos in range(folder_records_start, folder_records_start + folder_count * 16, 16):
            _, folder_ref, first, count = struct.unpack_from('<IIII', data, pos)
            _bounds_check(pamt_path, 'folder file range', first + count, file_count)
            folder_path = build_path(folders, folder_ref).strip('/')
            for index in range(first, first + count):
                file_folders[index] = folder_path
    extensions = []
    if compact:
        _bounds_check(pamt_path, 'extension size', records_end + 4, len(data))
        extension_size = struct.unpack_from('<I', data, records_end)[0]
        extension_end = records_end + 4 + extension_size
        if extension_end != len(data):
            raise ValueError('Corrupt PAMT: extension table does not tile the file')
        extension_data = data[records_end + 4:extension_end]
        if not extension_data.endswith(b'\0'):
            raise ValueError('Corrupt PAMT: unterminated extension table')
        extensions = extension_data[:-1].decode('utf-8').split('\0')
    entries = []
    import time as _time
    _entry_count = 0
    while off < records_end:
        node_ref, paz_offset, comp_size, orig_size, flags = \
            struct.unpack_from('<IIIII', data, off)
        off += 20

        # The chunk id (PAZ file index) is a u16 in the vendored RE's
        # flags layout (low 16 bits; compression type sits at bit 16).
        # Masking with 0xFF truncated indices above 255, so dirs with
        # more than 256 PAZ files resolved entries to the wrong archive.
        paz_index = flags & 0xFFFF
        node_path = build_path(nodes, node_ref)
        if compact:
            extension_id = flags >> 24
            if extension_id >= len(extensions):
                raise ValueError('Corrupt PAMT: invalid extension index')
            node_path += extensions[extension_id]
        prefix = file_folders.get(_entry_count, '') if full_paths else folder_prefix
        full_path = f"{prefix}/{node_path}" if prefix else node_path

        paz_num = int(pamt_stem) + paz_index
        paz_file = os.path.join(paz_dir, f"{paz_num}.paz")

        entries.append(PazEntry(
            path=full_path,
            paz_file=paz_file,
            offset=paz_offset,
            comp_size=comp_size,
            orig_size=orig_size,
            flags=flags,
            paz_index=paz_index,
        ))

        # Yield GIL every 200 entries so the GUI thread can paint
        _entry_count += 1
        if _entry_count % 200 == 0:
            _time.sleep(0)

    return entries


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Parse PAMT index and list PAZ archive contents")
    parser.add_argument("pamt", help="Path to .pamt file")
    parser.add_argument("--paz-dir", help="Directory containing .paz files (default: same as .pamt)")
    parser.add_argument("--filter", help="Filter entries by glob pattern (e.g. '*.xml', '*renderconfig*')")
    parser.add_argument("--stats", action="store_true", help="Show summary statistics")
    args = parser.parse_args()

    entries = parse_pamt(args.pamt, paz_dir=args.paz_dir)

    if args.filter:
        pattern = args.filter.lower()
        entries = [e for e in entries if fnmatch.fnmatch(e.path.lower(), f"*{pattern}*")
                   or fnmatch.fnmatch(os.path.basename(e.path).lower(), pattern)]

    if args.stats:
        compressed = sum(1 for e in entries if e.compressed)
        encrypted = sum(1 for e in entries if e.encrypted)
        total_comp = sum(e.comp_size for e in entries)
        total_orig = sum(e.orig_size for e in entries)
        print(f"Entries:     {len(entries):,}")
        print(f"Compressed:  {compressed:,}")
        print(f"Encrypted:   {encrypted:,} (XML files)")
        print(f"Total stored: {total_comp:,} bytes ({total_comp / 1024 / 1024:.1f} MB)")
        print(f"Total orig:   {total_orig:,} bytes ({total_orig / 1024 / 1024:.1f} MB)")
        return

    for e in entries:
        comp = "LZ4" if e.compression_type == 2 else "   "
        enc = "ENC" if e.encrypted else "   "
        print(f"[{comp}] [{enc}] {e.comp_size:>10,} -> {e.orig_size:>10,}  "
              f"paz:{e.paz_index} @0x{e.offset:08X}  {e.path}")

    print(f"\n{len(entries):,} entries")


def rewrite_pamt_localization_filename(
    pamt_data: bytes, from_suffix: str, to_suffix: str,
) -> bytes | None:
    """Port of JMM ``RewritePamtLocalizationFilename`` (ModManager.cs:562).

    Swaps the filename ``localizationstring_<from_suffix>.paloc`` for
    ``localizationstring_<to_suffix>.paloc`` inside a PAMT's node (filename)
    section. Used when a localisation PAZ mod targets language X but the
    user's Steam language is Y — rewriting the filename inside the PAMT
    makes the game resolve the mod's `.paloc` payload under the correct
    per-language VFS key.

    Returns the rewritten PAMT bytes, or ``None`` if the PAMT's file count
    isn't exactly 1, the suffix is unchanged, or the target string isn't
    present in the node section.
    """
    if from_suffix == to_suffix:
        return None

    from_name = f"localizationstring_{from_suffix}.paloc".encode("utf-8")
    to_name = f"localizationstring_{to_suffix}.paloc".encode("utf-8")

    # --- Walk the PAMT header to locate the node section (filename trie) ---
    try:
        off = 0
        off += 4  # outer hash
        paz_count = struct.unpack_from("<I", pamt_data, off)[0]
        off += 4
        off += 8  # constant + zero
        for i in range(paz_count):
            off += 8  # hash + size
            if i < paz_count - 1:
                off += 4  # separator
        # Folder section
        folder_size = struct.unpack_from("<I", pamt_data, off)[0]
        off += 4
        off += folder_size
        # Node section — this is the fn_block. off currently points at the
        # uint32 size prefix.
        fn_block_offset = off
        fn_block_size = struct.unpack_from("<I", pamt_data, off)[0]
        off += 4
        names_start = off
        names_end = off + fn_block_size
        # Folder record count comes after the node bytes — walk further only
        # if we need file_count, but here we use JMM's convention of treating
        # a single-file PAMT as the only valid input.
        off = names_end
        folder_count = struct.unpack_from("<I", pamt_data, off)[0]
        off += 4 + folder_count * 16
        file_count = struct.unpack_from("<I", pamt_data, off)[0]
    except (struct.error, IndexError) as e:
        logger.debug("PAMT rewrite: header parse failed: %s", e)
        return None

    if file_count != 1:
        # JMM restricts this path to single-file localisation PAMTs.
        return None

    # --- Scan the node section for the target filename ---
    found_at = -1
    for i in range(names_start, names_end - len(from_name) + 1):
        if pamt_data[i:i + len(from_name)] == from_name:
            found_at = i
            break
    if found_at < 0:
        return None

    # The length byte precedes the filename in every node record.
    length_byte_pos = found_at - 1
    if length_byte_pos < names_start or pamt_data[length_byte_pos] != len(from_name):
        # Either out of bounds or not actually a node entry (false positive).
        return None

    out = bytearray()
    out += pamt_data[:length_byte_pos]
    out.append(len(to_name))
    out += to_name
    out += pamt_data[found_at + len(from_name):]

    delta = len(to_name) - len(from_name)
    if delta != 0:
        struct.pack_into("<I", out, fn_block_offset,
                         fn_block_size + delta)

    # Recompute the outer integrity hash (hashlittle over bytes[12:]).
    from cdumm.archive.hashlittle import compute_pamt_hash
    struct.pack_into("<I", out, 0, compute_pamt_hash(bytes(out)))
    return bytes(out)


if __name__ == "__main__":
    main()
