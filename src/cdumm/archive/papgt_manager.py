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

    def __init__(self, game_dir: Path, vanilla_dir: Path | None = None) -> None:
        self._game_dir = game_dir
        self._papgt_path = game_dir / "meta" / "0.papgt"
        self._vanilla_papgt = vanilla_dir / "meta" / "0.papgt" if vanilla_dir else None

    def rebuild(self, modified_pamts: dict[str, bytes] | None = None) -> bytes:
        """Rebuild PAPGT with correct hashes for all directories.

        Starts from the vanilla PAPGT structure, then:
        1. Removes entries for directories that don't exist on disk
           (cleaned-up mod directories)
        2. Adds entries for new directories in modified_pamts
        3. Updates all PAMT hashes

        Args:
            modified_pamts: dict of {dir_name: pamt_bytes} for directories
                           that have been modified by mods. If None, reads
                           all PAMT files from disk.

        Returns:
            The rebuilt PAPGT bytes.
        """
        # Use vanilla PAPGT as the base structure
        base_path = self._vanilla_papgt if self._vanilla_papgt and self._vanilla_papgt.exists() else self._papgt_path
        if not base_path.exists():
            raise FileNotFoundError(f"PAPGT not found: {base_path}")

        papgt = bytearray(base_path.read_bytes())

        if len(papgt) < 12:
            raise ValueError("PAPGT file too small")

        # Preserve header metadata (bytes 0:4 and 8:12 are NOT hashes)
        header_meta0 = papgt[0:4]
        header_meta8 = papgt[8:12]

        entry_start = 12
        entry_count = _find_entry_count(papgt, entry_start)
        string_table_start = entry_start + entry_count * ENTRY_SIZE + 4

        logger.info("PAPGT base: %d entries from %s", entry_count, base_path.name)

        # Parse existing entries with their directory names
        parsed_entries: list[tuple[str, int, int]] = []  # (dir_name, flags, pamt_hash)
        for i in range(entry_count):
            pos = entry_start + i * ENTRY_SIZE
            flags = struct.unpack_from("<I", papgt, pos)[0]
            name_offset = struct.unpack_from("<I", papgt, pos + 4)[0]
            pamt_hash = struct.unpack_from("<I", papgt, pos + 8)[0]
            dir_name = _read_string(papgt, string_table_start, name_offset)
            if dir_name:
                parsed_entries.append((dir_name, flags, pamt_hash))

        # Determine which directories should be in the PAPGT:
        # - Keep entries where the PAMT exists on disk OR is in modified_pamts
        # - Remove entries for directories that no longer exist (mod cleanup)
        live_entries: list[tuple[str, int, int]] = []
        removed = []
        for dir_name, flags, pamt_hash in parsed_entries:
            pamt_on_disk = (self._game_dir / dir_name / "0.pamt").exists()
            in_modified = modified_pamts and dir_name in modified_pamts
            if pamt_on_disk or in_modified:
                live_entries.append((dir_name, flags, pamt_hash))
            else:
                removed.append(dir_name)

        if removed:
            logger.info("PAPGT: removing %d stale entries: %s", len(removed), removed)

        # Add new directories from modified_pamts not already in PAPGT.
        # Skip directories whose PAMT entries duplicate paths from existing
        # directories — these are overlay mods meant for file-copy installers
        # and adding them to PAPGT causes duplicate entry crashes.
        existing_names = {e[0] for e in live_entries}
        new_dirs = []
        if modified_pamts:
            # Collect all entry paths from existing PAMTs
            existing_entry_paths: set[str] = set()
            for dir_name, _, _ in live_entries:
                try:
                    from cdumm.archive.paz_parse import parse_pamt
                    pamt_path = self._game_dir / dir_name / "0.pamt"
                    if pamt_path.exists():
                        entries = parse_pamt(str(pamt_path), str(self._game_dir / dir_name))
                        for e in entries:
                            existing_entry_paths.add(e.path.lower())
                except Exception:
                    pass

            for dir_name in sorted(modified_pamts.keys()):
                if dir_name in existing_names:
                    continue
                # Check if new dir's PAMT has entries that duplicate existing paths
                try:
                    pamt_data = modified_pamts[dir_name]
                    import tempfile, os
                    # Write temp PAMT to parse it
                    tmp_dir = self._game_dir / dir_name
                    tmp_pamt = tmp_dir / "0.pamt"
                    pamt_existed = tmp_pamt.exists()
                    if not pamt_existed:
                        tmp_dir.mkdir(parents=True, exist_ok=True)
                        tmp_pamt.write_bytes(pamt_data)
                    try:
                        from cdumm.archive.paz_parse import parse_pamt
                        new_entries = parse_pamt(str(tmp_pamt), str(tmp_dir))
                        new_paths = {e.path.lower() for e in new_entries}
                        overlap = new_paths & existing_entry_paths
                        if overlap:
                            logger.info(
                                "PAPGT: skipping %s — %d/%d entries duplicate "
                                "existing paths (e.g. %s)",
                                dir_name, len(overlap), len(new_paths),
                                next(iter(overlap)))
                            continue
                    finally:
                        if not pamt_existed and tmp_pamt.exists():
                            tmp_pamt.unlink()
                            if tmp_dir.exists() and not any(tmp_dir.iterdir()):
                                tmp_dir.rmdir()
                except Exception as e:
                    logger.warning("PAPGT: failed to check %s for duplicates: %s",
                                   dir_name, e)
                new_dirs.append(dir_name)

        # Use the last directory's flag pattern for new entries.
        # High-numbered dirs use power-of-2 flags, not the common 0x003FFF00.
        if live_entries:
            last_flags = live_entries[-1][1]
        else:
            last_flags = 0x003FFF00

        if new_dirs:
            logger.info("PAPGT: adding %d new entries: %s", len(new_dirs), new_dirs)

        # Build the complete entry list: existing first, then new at end
        all_entries: list[tuple[str, int]] = []  # (dir_name, flags)
        for dir_name, flags, _ in live_entries:
            all_entries.append((dir_name, flags))
        for dir_name in new_dirs:
            all_entries.append((dir_name, last_flags))

        # Build string table
        string_table = bytearray()
        name_offsets: dict[str, int] = {}
        for dir_name, _ in all_entries:
            if dir_name not in name_offsets:
                name_offsets[dir_name] = len(string_table)
                string_table += dir_name.encode("ascii") + b"\x00"

        # Construct new PAPGT
        result = bytearray()
        result += header_meta0          # [0:4] metadata
        result += b"\x00\x00\x00\x00"   # [4:8] hash placeholder
        result += header_meta8          # [8:12] metadata

        # Update entry count in header byte 8
        new_count = len(all_entries)
        result[8] = new_count & 0xFF

        # Write entries
        for dir_name, flags in all_entries:
            # Compute PAMT hash
            if modified_pamts and dir_name in modified_pamts:
                pamt_data = modified_pamts[dir_name]
            else:
                pamt_path = self._game_dir / dir_name / "0.pamt"
                if pamt_path.exists():
                    pamt_data = pamt_path.read_bytes()
                else:
                    pamt_data = b""

            pamt_hash = compute_pamt_hash(pamt_data) if len(pamt_data) >= 12 else 0
            result += struct.pack("<III", flags, name_offsets[dir_name], pamt_hash)

        # Write string table size + string table
        result += struct.pack("<I", len(string_table))
        result += string_table

        # Compute and write PAPGT file hash at [4:8]
        papgt_hash = compute_papgt_hash(bytes(result))
        struct.pack_into("<I", result, 4, papgt_hash)

        logger.info("PAPGT rebuilt: %d entries (%d removed, %d added), hash=0x%08X",
                     new_count, len(removed), len(new_dirs), papgt_hash)

        return bytes(result)


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
