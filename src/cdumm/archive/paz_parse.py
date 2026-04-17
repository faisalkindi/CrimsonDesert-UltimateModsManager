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
    paz_index: int      # PAZ file index (from flags & 0xFF)
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

        The PAMT has no reliable encrypted flag — the heuristic (XML only)
        misses some files. When extraction detects actual encryption,
        set _encrypted_override = True so repack re-encrypts correctly.
        """
        if self._encrypted_override is not None:
            return self._encrypted_override
        return self.path.lower().endswith('.xml')


def parse_pamt(pamt_path: str, paz_dir: str = None) -> list[PazEntry]:
    """Parse a .pamt index file and return all file entries.

    Args:
        pamt_path: path to the .pamt file
        paz_dir: directory containing .paz files (default: same dir as .pamt)

    Returns:
        list of PazEntry
    """
    with open(pamt_path, 'rb') as f:
        data = f.read()

    if paz_dir is None:
        paz_dir = os.path.dirname(pamt_path) or '.'

    pamt_stem = os.path.splitext(os.path.basename(pamt_path))[0]

    off = 0
    off += 4  # skip magic (varies between game versions)

    paz_count = struct.unpack_from('<I', data, off)[0]; off += 4
    off += 8  # hash + zero

    # PAZ table
    for i in range(paz_count):
        off += 4  # hash
        off += 4  # size
        if i < paz_count - 1:
            off += 4  # separator

    # Folder section
    folder_size = struct.unpack_from('<I', data, off)[0]; off += 4
    folder_end = off + folder_size
    folder_prefix = ""
    while off < folder_end:
        parent = struct.unpack_from('<I', data, off)[0]
        slen = data[off + 4]
        name = data[off + 5:off + 5 + slen].decode('utf-8', errors='replace')
        if parent == 0xFFFFFFFF:
            folder_prefix = name
        off += 5 + slen

    # Node section (path tree)
    node_size = struct.unpack_from('<I', data, off)[0]; off += 4
    node_start = off
    nodes = {}
    while off < node_start + node_size:
        rel = off - node_start
        parent = struct.unpack_from('<I', data, off)[0]
        slen = data[off + 4]
        name = data[off + 5:off + 5 + slen].decode('utf-8', errors='replace')
        nodes[rel] = (parent, name)
        off += 5 + slen

    def build_path(node_ref):
        parts = []
        cur = node_ref
        while cur != 0xFFFFFFFF and len(parts) < 64:
            if cur not in nodes:
                break
            p, n = nodes[cur]
            parts.append(n)
            cur = p
        return ''.join(reversed(parts))

    # Folder record section
    folder_count = struct.unpack_from('<I', data, off)[0]; off += 4
    off += folder_count * 16  # skip folder records (16 bytes each)

    # File record section
    file_count = struct.unpack_from('<I', data, off)[0]; off += 4
    entries = []
    import time as _time
    _entry_count = 0
    while off + 20 <= len(data):
        node_ref, paz_offset, comp_size, orig_size, flags = \
            struct.unpack_from('<IIIII', data, off)
        off += 20

        paz_index = flags & 0xFF
        node_path = build_path(node_ref)
        full_path = f"{folder_prefix}/{node_path}" if folder_prefix else node_path

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
