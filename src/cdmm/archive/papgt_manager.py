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

from cdmm.archive.hashlittle import compute_pamt_hash, compute_papgt_hash

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
            The rebuilt PAPGT bytes (also writes to disk).
        """
        if not self._papgt_path.exists():
            raise FileNotFoundError(f"PAPGT not found: {self._papgt_path}")

        papgt = bytearray(self._papgt_path.read_bytes())

        # Parse entries to find where each directory's hash lives
        if len(papgt) < 12:
            raise ValueError("PAPGT file too small")

        # Scan entries starting at offset 12
        # Each entry is 12 bytes: [pamt_hash:4][flags:4][name_offset:4]
        entry_start = 12
        entries: list[tuple[int, int, int, int]] = []  # (offset, hash, flags, name_offset)

        pos = entry_start
        while pos + 12 <= len(papgt):
            pamt_hash = struct.unpack_from("<I", papgt, pos)[0]
            flags = struct.unpack_from("<I", papgt, pos + 4)[0]
            name_offset = struct.unpack_from("<I", papgt, pos + 8)[0]

            # Detect end of entries (heuristic: when we hit the string table)
            # String table entries are ASCII directory names
            if flags == 0 and name_offset == 0 and pamt_hash == 0:
                break

            entries.append((pos, pamt_hash, flags, name_offset))
            pos += 12

            # Safety limit
            if len(entries) > 50:
                break

        logger.info("PAPGT: found %d directory entries", len(entries))

        # Update each entry's PAMT hash
        for entry_offset, old_hash, flags, name_offset in entries:
            # Try to determine directory name from the string table
            dir_name = self._read_dir_name(papgt, entry_start, len(entries), name_offset)

            if dir_name is None:
                continue

            # Get current PAMT data
            if modified_pamts and dir_name in modified_pamts:
                pamt_data = modified_pamts[dir_name]
            else:
                pamt_path = self._game_dir / dir_name / "0.pamt"
                if not pamt_path.exists():
                    continue
                pamt_data = pamt_path.read_bytes()

            # Compute and write new PAMT hash
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
