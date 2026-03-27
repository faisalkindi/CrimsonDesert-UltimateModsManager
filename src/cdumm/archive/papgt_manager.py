"""PAPGT authority — single point of control for meta/0.papgt.

The mod manager ALWAYS rebuilds PAPGT from scratch on every apply.
No individual mod ever writes to PAPGT directly.

PAPGT format:
  [0:4]  = entry count or metadata (DO NOT modify)
  [4:8]  = file integrity hash: hashlittle(papgt[12:], 0xC5EDE)
  [8:12] = version/magic
  [12:]  = 33 x 12-byte entries (flags + string_offset + pamt_hash) + string table

Each 12-byte entry:
  [0:4]  = PAMT hash for this directory
  [4:8]  = flags (e.g., 00 FF 3F 00)
  [8:12] = offset into name table
"""
import logging
import struct
from pathlib import Path

from cdumm.archive.hashlittle import compute_pamt_hash, compute_papgt_hash

logger = logging.getLogger(__name__)


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

        # Parse existing entries starting at offset 12
        # Each entry is 12 bytes: [pamt_hash:4][flags:4][name_offset:4]
        entry_start = 12
        entries: list[tuple[int, int, int, int]] = []  # (offset, hash, flags, name_offset)

        pos = entry_start
        while pos + 12 <= len(papgt):
            pamt_hash = struct.unpack_from("<I", papgt, pos)[0]
            flags = struct.unpack_from("<I", papgt, pos + 4)[0]
            name_offset = struct.unpack_from("<I", papgt, pos + 8)[0]

            if flags == 0 and name_offset == 0 and pamt_hash == 0:
                break

            entries.append((pos, pamt_hash, flags, name_offset))
            pos += 12

            if len(entries) > 100:
                break

        logger.info("PAPGT: found %d directory entries", len(entries))

        # Build map of existing directory names
        existing_dirs: set[str] = set()
        for entry_offset, old_hash, flags, name_offset in entries:
            dir_name = self._read_dir_name(papgt, entry_start, len(entries), name_offset)
            if dir_name:
                existing_dirs.add(dir_name)

        # Find new directories from modified_pamts that aren't in PAPGT
        new_dirs: list[str] = []
        if modified_pamts:
            for dir_name in sorted(modified_pamts.keys()):
                if dir_name not in existing_dirs:
                    new_dirs.append(dir_name)

        if new_dirs:
            logger.info("PAPGT: adding %d new directory entries: %s", len(new_dirs), new_dirs)
            papgt = self._add_new_entries(papgt, entries, entry_start, new_dirs, modified_pamts)
            # Re-parse entries after modification
            entries = []
            pos = entry_start
            while pos + 12 <= len(papgt):
                pamt_hash = struct.unpack_from("<I", papgt, pos)[0]
                flags = struct.unpack_from("<I", papgt, pos + 4)[0]
                name_offset = struct.unpack_from("<I", papgt, pos + 8)[0]
                if flags == 0 and name_offset == 0 and pamt_hash == 0:
                    break
                entries.append((pos, pamt_hash, flags, name_offset))
                pos += 12
                if len(entries) > 100:
                    break

        # Update each entry's PAMT hash
        for entry_offset, old_hash, flags, name_offset in entries:
            dir_name = self._read_dir_name(papgt, entry_start, len(entries), name_offset)

            if dir_name is None:
                continue

            if modified_pamts and dir_name in modified_pamts:
                pamt_data = modified_pamts[dir_name]
            else:
                pamt_path = self._game_dir / dir_name / "0.pamt"
                if not pamt_path.exists():
                    continue
                pamt_data = pamt_path.read_bytes()

            new_hash = compute_pamt_hash(pamt_data)
            struct.pack_into("<I", papgt, entry_offset, new_hash)

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
        new_dirs: list[str],
        modified_pamts: dict[str, bytes],
    ) -> bytearray:
        """Add new directory entries to PAPGT for mod-added directories.

        Inserts new 12-byte entries after existing ones and extends the string table.
        All name_offset values are recalculated since the string table shifts.
        """
        old_entry_count = len(entries)
        old_string_table_start = entry_start + old_entry_count * 12

        # Read all existing directory names and their string table positions
        dir_names: list[str] = []
        for _, _, _, name_offset in entries:
            name = self._read_dir_name(papgt, entry_start, old_entry_count, name_offset)
            dir_names.append(name or "")

        # Existing string table content
        old_string_table = papgt[old_string_table_start:]

        # Add new directory names to the string table
        new_string_additions = bytearray()
        new_dir_offsets: list[int] = []
        for dir_name in new_dirs:
            # Offset within string table = existing table size + additions so far
            new_dir_offsets.append(len(old_string_table) + len(new_string_additions))
            new_string_additions += dir_name.encode("ascii") + b"\x00"

        # Build new PAPGT
        new_entry_count = old_entry_count + len(new_dirs)
        new_string_table_start = entry_start + new_entry_count * 12
        shift = len(new_dirs) * 12  # how much the string table shifted

        result = bytearray(papgt[:entry_start])  # header

        # Use the most common flags from existing entries for new ones
        default_flags = 0x003FFF00
        if entries:
            flag_counts: dict[int, int] = {}
            for _, _, flags, _ in entries:
                flag_counts[flags] = flag_counts.get(flags, 0) + 1
            default_flags = max(flag_counts, key=flag_counts.get)

        # Write existing entries with adjusted name_offsets (string table shifted)
        for _, pamt_hash, flags, name_offset in entries:
            result += struct.pack("<III", pamt_hash, flags, name_offset)

        # Write new entries
        for i, dir_name in enumerate(new_dirs):
            pamt_data = modified_pamts.get(dir_name, b"")
            pamt_hash = compute_pamt_hash(pamt_data) if pamt_data else 0
            name_offset = new_dir_offsets[i]
            result += struct.pack("<III", pamt_hash, default_flags, name_offset)
            logger.info("PAPGT: new entry for %s, hash=0x%08X, flags=0x%08X",
                        dir_name, pamt_hash, default_flags)

        # Write string table (existing + new)
        result += old_string_table + new_string_additions

        return result

    def _read_dir_name(self, papgt: bytearray, entry_start: int,
                       entry_count: int, name_offset: int) -> str | None:
        """Read a directory name from the PAPGT string table."""
        # String table starts after all entries
        string_table_start = entry_start + entry_count * 12

        abs_offset = string_table_start + name_offset
        if abs_offset >= len(papgt):
            return None

        # Read null-terminated string
        end = papgt.index(0, abs_offset) if 0 in papgt[abs_offset:] else len(papgt)
        name = papgt[abs_offset:end].decode("ascii", errors="replace")
        return name if name else None
