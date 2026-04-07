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

import os
import struct
import fnmatch
from dataclasses import dataclass, field


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


if __name__ == "__main__":
    main()
