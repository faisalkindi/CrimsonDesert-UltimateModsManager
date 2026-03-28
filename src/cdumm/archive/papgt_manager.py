"""PAPGT authority — single point of control for meta/0.papgt.

The mod manager ALWAYS rebuilds PAPGT from scratch on every apply.
No individual mod ever writes to PAPGT directly.

PAPGT format:
  [0:4]  = metadata (DO NOT modify)
  [4:8]  = file integrity hash: hashlittle(papgt[12:], 0xC5EDE)
  [8:12] = metadata (DO NOT modify)
  [12:]  = N x 12-byte entries + 4-byte string table size + string table

Each 12-byte entry:
  [0:4]  = flags (e.g., 00 FF 3F 00)
  [4:8]  = offset into string table (null-terminated ASCII dir name)
  [8:12] = PAMT hash for this directory: hashlittle(pamt[12:], 0xC5EDE)

After entries: 4-byte LE uint = string table size in bytes
Then: string table (null-terminated ASCII strings)
"""
import logging
import struct
from pathlib import Path

from cdumm.archive.hashlittle import compute_pamt_hash, compute_papgt_hash

logger = logging.getLogger(__name__)

ENTRY_SIZE = 12  # bytes per directory entry


class PapgtManager:
    """Manages PAPGT rebuild from scratch."""

    def __init__(self, game_dir: Path) -> None:
        self._game_dir = game_dir
        self._papgt_path = game_dir / "meta" / "0.papgt"

    def rebuild(self, modified_pamts: dict[str, bytes] | None = None) -> bytes:
        """Rebuild PAPGT with correct hashes for all directories.

        Args:
            modified_pamts: dict of {dir_name: pamt_bytes} for directories
                           that have been modified by mods. If None, reads
                           all PAMT files from disk.

        Returns:
            The rebuilt PAPGT bytes.
        """
        if not self._papgt_path.exists():
            raise FileNotFoundError(f"PAPGT not found: {self._papgt_path}")

        papgt = bytearray(self._papgt_path.read_bytes())

        if len(papgt) < 12:
            raise ValueError("PAPGT file too small")

        entry_start = 12
        entry_count = _find_entry_count(papgt, entry_start)
        string_table_start = entry_start + entry_count * ENTRY_SIZE + 4  # +4 for size field

        logger.info("PAPGT: %d directory entries, string table at %d",
                     entry_count, string_table_start)

        # Parse existing entries
        entries: list[tuple[int, int, int, int]] = []  # (offset, flags, name_offset, pamt_hash)
        for i in range(entry_count):
            pos = entry_start + i * ENTRY_SIZE
            flags = struct.unpack_from("<I", papgt, pos)[0]
            name_offset = struct.unpack_from("<I", papgt, pos + 4)[0]
            pamt_hash = struct.unpack_from("<I", papgt, pos + 8)[0]
            entries.append((pos, flags, name_offset, pamt_hash))

        # Build map of existing directory names
        existing_dirs: dict[str, int] = {}  # dir_name -> entry index
        for i, (_, _, name_offset, _) in enumerate(entries):
            dir_name = _read_string(papgt, string_table_start, name_offset)
            if dir_name:
                existing_dirs[dir_name] = i

        # Find new directories from modified_pamts that aren't in PAPGT
        new_dirs: list[str] = []
        if modified_pamts:
            for dir_name in sorted(modified_pamts.keys()):
                if dir_name not in existing_dirs:
                    new_dirs.append(dir_name)

        if new_dirs:
            logger.info("PAPGT: adding %d new directory entries: %s",
                         len(new_dirs), new_dirs)
            papgt = self._add_new_entries(
                papgt, entries, entry_start, entry_count,
                string_table_start, new_dirs, modified_pamts)
            # Re-parse after modification
            entry_count = _find_entry_count(papgt, entry_start)
            string_table_start = entry_start + entry_count * ENTRY_SIZE + 4
            entries = []
            for i in range(entry_count):
                pos = entry_start + i * ENTRY_SIZE
                flags = struct.unpack_from("<I", papgt, pos)[0]
                name_offset = struct.unpack_from("<I", papgt, pos + 4)[0]
                pamt_hash = struct.unpack_from("<I", papgt, pos + 8)[0]
                entries.append((pos, flags, name_offset, pamt_hash))
            existing_dirs = {}
            for i, (_, _, name_offset, _) in enumerate(entries):
                dir_name = _read_string(papgt, string_table_start, name_offset)
                if dir_name:
                    existing_dirs[dir_name] = i

        # Update each entry's PAMT hash
        for dir_name, idx in existing_dirs.items():
            entry_offset, flags, name_offset, old_hash = entries[idx]

            if modified_pamts and dir_name in modified_pamts:
                pamt_data = modified_pamts[dir_name]
            else:
                pamt_path = self._game_dir / dir_name / "0.pamt"
                if not pamt_path.exists():
                    continue
                pamt_data = pamt_path.read_bytes()

            new_hash = compute_pamt_hash(pamt_data)
            # Hash is at entry_offset + 8 (third field)
            struct.pack_into("<I", papgt, entry_offset + 8, new_hash)

            if new_hash != old_hash:
                logger.info("PAPGT: updated %s hash 0x%08X -> 0x%08X",
                           dir_name, old_hash, new_hash)

        # Recompute PAPGT file hash at [4:8]
        papgt_hash = compute_papgt_hash(bytes(papgt))
        struct.pack_into("<I", papgt, 4, papgt_hash)
        logger.info("PAPGT: file hash updated to 0x%08X", papgt_hash)

        return bytes(papgt)

    def _add_new_entries(
        self, papgt: bytearray,
        entries: list[tuple[int, int, int, int]],
        entry_start: int,
        entry_count: int,
        string_table_start: int,
        new_dirs: list[str],
        modified_pamts: dict[str, bytes],
    ) -> bytearray:
        """Add new directory entries to PAPGT for mod-added directories.

        Inserts new 12-byte entries after existing ones, updates the string
        table size field, and extends the string table.
        """
        # Read existing string table (after the 4-byte size field)
        old_string_table = papgt[string_table_start:]

        # Build new string additions
        new_string_additions = bytearray()
        new_dir_offsets: list[int] = []
        for dir_name in new_dirs:
            new_dir_offsets.append(len(old_string_table) + len(new_string_additions))
            new_string_additions += dir_name.encode("ascii") + b"\x00"

        # Use the most common flags from existing entries for new ones
        default_flags = 0x003FFF00
        if entries:
            flag_counts: dict[int, int] = {}
            for _, flags, _, _ in entries:
                flag_counts[flags] = flag_counts.get(flags, 0) + 1
            default_flags = max(flag_counts, key=flag_counts.get)

        # Build new PAPGT
        result = bytearray(papgt[:entry_start])  # header (12 bytes)

        # Write existing entries (unchanged)
        for _, flags, name_offset, pamt_hash in entries:
            result += struct.pack("<III", flags, name_offset, pamt_hash)

        # Write new entries
        for i, dir_name in enumerate(new_dirs):
            pamt_data = modified_pamts.get(dir_name, b"")
            pamt_hash = compute_pamt_hash(pamt_data) if pamt_data else 0
            result += struct.pack("<III", default_flags, new_dir_offsets[i], pamt_hash)
            logger.info("PAPGT: new entry for %s, hash=0x%08X, flags=0x%08X",
                        dir_name, pamt_hash, default_flags)

        # Write string table size field
        new_string_table_size = len(old_string_table) + len(new_string_additions)
        result += struct.pack("<I", new_string_table_size)

        # Write string table (existing + new)
        result += old_string_table + new_string_additions

        return result


def _find_entry_count(papgt: bytearray, entry_start: int) -> int:
    """Determine entry count by finding the string table size field.

    The string table size field is at entry_start + N*12, and its value
    satisfies: entry_start + N*12 + 4 + string_size == len(papgt).
    """
    file_size = len(papgt)
    for n in range(1, 100):
        size_field_pos = entry_start + n * ENTRY_SIZE
        if size_field_pos + 4 > file_size:
            break
        string_size = struct.unpack_from("<I", papgt, size_field_pos)[0]
        if size_field_pos + 4 + string_size == file_size:
            return n
    # Fallback: estimate from file size
    logger.warning("PAPGT: could not determine entry count, estimating")
    return (file_size - entry_start - 4) // ENTRY_SIZE


def _read_string(papgt: bytearray, string_table_start: int,
                 name_offset: int) -> str | None:
    """Read a null-terminated string from the PAPGT string table."""
    abs_offset = string_table_start + name_offset
    if abs_offset >= len(papgt):
        return None
    end = papgt.index(0, abs_offset) if 0 in papgt[abs_offset:] else len(papgt)
    name = papgt[abs_offset:end].decode("ascii", errors="replace")
    return name if name else None
