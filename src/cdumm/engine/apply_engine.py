"""Apply engine — composes enabled mod deltas into a valid game state.

Pipeline:
  1. Ensure vanilla range backups exist for all mod-affected files
  2. Read game files, restore vanilla at mod byte ranges
  3. Apply each enabled mod's delta in sequence
  4. Rebuild PAPGT from scratch
  5. Stage all modified files
  6. Atomic commit (transactional I/O)

Vanilla backups are byte-range level (not full file copies) for files with
sparse deltas. Only the specific byte ranges that mods modify are backed up.
Bsdiff deltas use full file backups (but those files are always small).
"""
import logging
import struct
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from cdumm.archive.papgt_manager import PapgtManager
from cdumm.archive.transactional_io import TransactionalIO
from cdumm.engine.delta_engine import (
    SPARSE_MAGIC, apply_delta, apply_delta_from_file, load_delta,
)
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)

RANGE_BACKUP_EXT = ".vranges"  # sparse range backup extension


def _backup_copy(src: Path, dst: Path) -> None:
    """Copy a file for vanilla backup. Always a real copy, never a hard link.

    Hard links are unsafe for backups — if a script mod writes directly to
    the game file, it corrupts the backup too (same inode).
    """
    import shutil
    shutil.copy2(src, dst)


def _delta_changes_size(delta_path: Path, vanilla_size: int) -> bool:
    """Check if a delta replaces or resizes the file.

    Returns True for:
    - FULL_COPY deltas (always replace entire file — must be applied first)
    - SPRS deltas that write past vanilla_size
    - bsdiff deltas that produce different size (checked by output size)
    """
    try:
        with open(delta_path, "rb") as f:
            magic = f.read(4)

            if magic == b"FULL":
                # FULL_COPY replaces the entire file — always "changes size"
                # conceptually, even if output happens to be same length.
                # Must be applied before SPRS patches from other mods.
                return True

            if magic == b"BSDI":  # bsdiff4 header "BSDIFF40"
                # bsdiff output size is at offset 16 (8 bytes LE)
                f.seek(16)
                new_size = struct.unpack("<q", f.read(8))[0]
                return new_size != vanilla_size

            if magic == SPARSE_MAGIC:
                count = struct.unpack("<I", f.read(4))[0]
                for _ in range(count):
                    offset = struct.unpack("<Q", f.read(8))[0]
                    length = struct.unpack("<I", f.read(4))[0]
                    if offset + length > vanilla_size:
                        return True
                    f.seek(length, 1)
    except Exception:
        pass
    return False


def _find_insertion_point(delta_path: Path) -> int:
    """Find the first offset in a sparse delta (the insertion/shift point)."""
    try:
        with open(delta_path, "rb") as f:
            f.read(4)  # skip magic
            count = struct.unpack("<I", f.read(4))[0]
            if count > 0:
                offset = struct.unpack("<Q", f.read(8))[0]
                return offset
    except Exception:
        pass
    return 0


def _apply_sparse_shifted(
    buf: bytearray, delta_path: Path, insertion_point: int, shift: int,
) -> None:
    """Apply a sparse delta with offset adjustment for shifted data.

    Entries at or after insertion_point have their offset shifted.
    """
    with open(delta_path, "rb") as f:
        magic = f.read(4)
        if magic != SPARSE_MAGIC:
            return  # can't shift bsdiff
        count = struct.unpack("<I", f.read(4))[0]

        for _ in range(count):
            offset = struct.unpack("<Q", f.read(8))[0]
            length = struct.unpack("<I", f.read(4))[0]
            data = f.read(length)

            # Adjust offset if past the insertion point
            if offset >= insertion_point:
                offset += shift

            end = offset + length
            if end > len(buf):
                buf.extend(b"\x00" * (end - len(buf)))
            buf[offset:end] = data


# ── Range backup helpers ─────────────────────────────────────────────

def _save_range_backup(game_dir: Path, vanilla_dir: Path,
                       file_path: str, byte_ranges: list[tuple[int, int]]) -> None:
    """Save vanilla bytes at specific byte ranges from the game file.

    Stored in sparse format: SPRS + count + (offset, length, data)*
    """
    game_file = game_dir / file_path.replace("/", "\\")
    if not game_file.exists():
        return

    backup_path = vanilla_dir / (file_path.replace("/", "_") + RANGE_BACKUP_EXT)
    if backup_path.exists():
        return  # already backed up

    # Merge overlapping ranges and sort
    merged = _merge_ranges(byte_ranges)

    buf = bytearray(SPARSE_MAGIC)
    buf += struct.pack("<I", len(merged))

    with open(game_file, "rb") as f:
        for start, end in merged:
            length = end - start
            f.seek(start)
            data = f.read(length)
            buf += struct.pack("<QI", start, len(data))
            buf += data

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_bytes(bytes(buf))
    total_bytes = sum(e - s for s, e in merged)
    logger.info("Range backup: %s (%d ranges, %d bytes)",
                file_path, len(merged), total_bytes)


def _load_range_backup(vanilla_dir: Path, file_path: str
                       ) -> list[tuple[int, bytes]] | None:
    """Load a range backup. Returns [(offset, data), ...] or None."""
    backup_path = vanilla_dir / (file_path.replace("/", "_") + RANGE_BACKUP_EXT)
    if not backup_path.exists():
        return None

    raw = backup_path.read_bytes()
    if raw[:4] != SPARSE_MAGIC:
        return None

    entries: list[tuple[int, bytes]] = []
    offset = 4
    count = struct.unpack_from("<I", raw, offset)[0]
    offset += 4

    for _ in range(count):
        file_offset = struct.unpack_from("<Q", raw, offset)[0]
        offset += 8
        length = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        data = raw[offset:offset + length]
        offset += length
        entries.append((file_offset, data))

    return entries


def _apply_ranges_to_buf(buf: bytearray, entries: list[tuple[int, bytes]]) -> None:
    """Overwrite byte ranges in a buffer."""
    for file_offset, data in entries:
        end = file_offset + len(data)
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))
        buf[file_offset:end] = data


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent byte ranges."""
    if not ranges:
        return []
    sorted_r = sorted(ranges)
    merged = [sorted_r[0]]
    for start, end in sorted_r[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _apply_pamt_entry_update(data: bytearray, update: dict) -> None:
    """Update a single PAMT file record based on entry-level delta changes.

    Finds the record by matching vanilla offset/comp_size/orig_size/flags,
    then updates offset, comp_size, and optionally PAZ size in the header.
    """
    entry = update["entry"]  # PazEntry with vanilla values
    new_comp = update["new_comp_size"]
    new_offset = update["new_offset"]
    new_orig = update.get("new_orig_size", entry.orig_size)
    new_paz_size = update.get("new_paz_size")

    # Update PAZ size table in PAMT header if entry was appended
    if new_paz_size is not None:
        paz_count = struct.unpack_from("<I", data, 4)[0]
        paz_index = entry.paz_index
        if paz_index < paz_count:
            table_off = 16
            for i in range(paz_index):
                table_off += 8
                if i < paz_count - 1:
                    table_off += 4
            size_off = table_off + 4  # skip hash, point to size
            old_size = struct.unpack_from("<I", data, size_off)[0]
            # Use the larger of current and new size (multiple entries may append)
            final_size = max(old_size, new_paz_size)
            struct.pack_into("<I", data, size_off, final_size)
            logger.debug("Updated PAMT PAZ[%d] size: %d -> %d",
                         paz_index, old_size, final_size)

    # Find and update the file record (20 bytes: node_ref + offset + comp + orig + flags)
    search = struct.pack("<IIII", entry.offset, entry.comp_size,
                         entry.orig_size, entry.flags)
    pos = data.find(search)
    if pos >= 4:  # at least 4 bytes for node_ref
        struct.pack_into("<I", data, pos, new_offset)
        struct.pack_into("<I", data, pos + 4, new_comp)
        if new_orig != entry.orig_size:
            struct.pack_into("<I", data, pos + 8, new_orig)
        logger.debug("Patched PAMT record for %s: offset %d->%d, comp %d->%d",
                     entry.path, entry.offset, new_offset,
                     entry.comp_size, new_comp)
    else:
        logger.warning("Could not find PAMT record for %s (offset=0x%X, comp=%d)",
                       entry.path, entry.offset, entry.comp_size)


# ── Workers ──────────────────────────────────────────────────────────

class ApplyWorker(QObject):
    """Background worker for apply operation."""

    progress_updated = Signal(int, str)
    finished = Signal()
    error_occurred = Signal(str)

    def __init__(self, game_dir: Path, vanilla_dir: Path, db_path: Path) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._vanilla_dir = vanilla_dir
        self._db_path = db_path

    def run(self) -> None:
        try:
            self._db = Database(self._db_path)
            self._db.initialize()
            self._apply()
            self._db.close()
        except Exception as e:
            logger.error("Apply failed: %s", e, exc_info=True)
            self.error_occurred.emit(f"Apply failed: {e}")

    def _apply(self) -> None:
        file_deltas = self._get_file_deltas()
        revert_files = self._get_files_to_revert(set(file_deltas.keys()))

        if not file_deltas and not revert_files:
            self.error_occurred.emit("No mod changes to apply or revert.")
            return

        # Entry-level deltas (from script mods) require updating the PAMT
        # after PAZ composition. Track updates here for Phase 2.
        self._pamt_entry_updates: dict[str, list[dict]] = {}

        # Also ensure PAMTs are backed up for directories with entry deltas
        entry_pamt_dirs = set()
        for file_path, deltas in file_deltas.items():
            if any(d.get("entry_path") for d in deltas):
                entry_pamt_dirs.add(file_path.split("/")[0])

        all_files = set(file_deltas.keys()) | set(revert_files)
        total_files = len(all_files) + len(entry_pamt_dirs)
        self.progress_updated.emit(0, f"Applying {total_files} file(s)...")

        # Ensure vanilla backups exist BEFORE any modifications.
        self.progress_updated.emit(2, "Backing up vanilla byte ranges...")
        self._ensure_backups(file_deltas, revert_files)
        # Ensure PAMT backups for directories with entry-level deltas
        for pamt_dir in entry_pamt_dirs:
            pamt_path = f"{pamt_dir}/0.pamt"
            full_path = self._vanilla_dir / pamt_path.replace("/", "\\")
            if not full_path.exists():
                game_pamt = self._game_dir / pamt_path.replace("/", "\\")
                if game_pamt.exists():
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    _backup_copy(game_pamt, full_path)
                    logger.info("Full vanilla backup (PAMT for entries): %s", pamt_path)

        staging_dir = self._game_dir / ".cdumm_staging"
        staging_dir.mkdir(exist_ok=True)
        txn = TransactionalIO(self._game_dir, staging_dir)
        modified_pamts: dict[str, bytes] = {}

        try:
            file_idx = 0

            # ── Phase 1: Compose PAZ and other non-PAMT files ──────────
            for file_path, deltas in file_deltas.items():
                pct = int((file_idx / total_files) * 60)
                self.progress_updated.emit(pct, f"Processing {file_path}...")
                file_idx += 1

                # Skip PAPGT (rebuilt at end) and PAMT (Phase 2)
                if file_path == "meta/0.papgt":
                    continue
                if file_path.endswith(".pamt"):
                    continue

                # New files: copy from stored full file (last mod wins)
                new_deltas = [d for d in deltas if d.get("is_new")]
                mod_deltas = [d for d in deltas if not d.get("is_new")]

                if new_deltas and not mod_deltas:
                    src = Path(new_deltas[-1]["delta_path"])
                    if src.exists():
                        result_bytes = src.read_bytes()
                        txn.stage_file(file_path, result_bytes)
                        logger.info("Applying new file: %s from %s",
                                    file_path, new_deltas[-1]["mod_name"])
                    continue

                result_bytes = self._compose_file(file_path, mod_deltas)
                if result_bytes is None:
                    continue

                txn.stage_file(file_path, result_bytes)

            # ── Phase 2: Compose PAMT files (entry updates + byte deltas) ──
            # Collect all PAMTs that need processing
            pamt_paths = set()
            for fp in file_deltas:
                if fp.endswith(".pamt"):
                    pamt_paths.add(fp)
            for pamt_dir in self._pamt_entry_updates:
                pamt_paths.add(f"{pamt_dir}/0.pamt")

            for pamt_path in sorted(pamt_paths):
                pct = int((file_idx / total_files) * 80)
                self.progress_updated.emit(pct, f"Processing {pamt_path}...")
                file_idx += 1

                pamt_dir = pamt_path.split("/")[0]
                byte_deltas = file_deltas.get(pamt_path, [])
                # Filter out entry_path deltas (shouldn't be on PAMT, but be safe)
                byte_deltas = [d for d in byte_deltas
                               if not d.get("entry_path") and not d.get("is_new")]

                new_pamt_deltas = [d for d in file_deltas.get(pamt_path, [])
                                   if d.get("is_new")]

                entry_updates = self._pamt_entry_updates.get(pamt_dir, [])

                if new_pamt_deltas and not byte_deltas and not entry_updates:
                    # Purely new PAMT — use last copy
                    src = Path(new_pamt_deltas[-1]["delta_path"])
                    if src.exists():
                        result_bytes = src.read_bytes()
                        txn.stage_file(pamt_path, result_bytes)
                        modified_pamts[pamt_dir] = result_bytes
                    continue

                result_bytes = self._compose_pamt(
                    pamt_path, pamt_dir, byte_deltas, entry_updates)
                if result_bytes is None:
                    continue

                txn.stage_file(pamt_path, result_bytes)
                modified_pamts[pamt_dir] = result_bytes

            # ── Phase 3: Revert files from disabled mods ───────────────
            new_files_to_delete = self._get_new_files_to_delete(set(file_deltas.keys()))
            for file_path in revert_files:
                pct = int((file_idx / total_files) * 80)
                self.progress_updated.emit(pct, f"Reverting {file_path}...")
                file_idx += 1

                if file_path in new_files_to_delete:
                    game_path = self._game_dir / file_path.replace("/", "\\")
                    if game_path.exists():
                        game_path.unlink()
                        logger.info("Deleted new file from disabled mod: %s", file_path)
                    parent = game_path.parent
                    if parent != self._game_dir and parent.exists():
                        remaining = list(parent.iterdir())
                        if not remaining:
                            parent.rmdir()
                            logger.info("Removed empty mod directory: %s", parent.name)
                    continue

                vanilla_bytes = self._get_vanilla_bytes(file_path)
                if vanilla_bytes is None:
                    logger.warning("Cannot revert %s — no backup", file_path)
                    continue

                txn.stage_file(file_path, vanilla_bytes)
                if file_path.endswith(".pamt"):
                    modified_pamts[file_path.split("/")[0]] = vanilla_bytes

            # ── Phase 4: Rebuild PAPGT ─────────────────────────────────
            self.progress_updated.emit(85, "Rebuilding PAPGT integrity chain...")
            papgt_mgr = PapgtManager(self._game_dir, self._vanilla_dir)
            try:
                papgt_bytes = papgt_mgr.rebuild(modified_pamts)
                txn.stage_file("meta/0.papgt", papgt_bytes)
            except FileNotFoundError:
                logger.warning("PAPGT not found, skipping rebuild")

            self.progress_updated.emit(95, "Committing changes...")
            txn.commit()

            self.progress_updated.emit(100, "Apply complete!")
            self.finished.emit()

        except Exception:
            txn.cleanup_staging()
            raise
        finally:
            txn.cleanup_staging()

    def _ensure_backups(self, file_deltas: dict, revert_files: list[str]) -> None:
        """Create vanilla backups for all files about to be modified.

        Validates each backup against the snapshot hash to ensure we're
        backing up actual vanilla files, not modded ones. A dirty backup
        poisons the entire restore chain.
        """
        self._vanilla_dir.mkdir(parents=True, exist_ok=True)

        # Load snapshot hashes for validation
        snap_hashes: dict[str, tuple[str, int]] = {}
        try:
            cursor = self._db.connection.execute(
                "SELECT file_path, file_hash, file_size FROM snapshots")
            for rel, h, s in cursor.fetchall():
                snap_hashes[rel] = (h, s)
        except Exception:
            pass

        all_files = set(file_deltas.keys()) | set(revert_files)
        for file_path in all_files:
            delta_infos = file_deltas.get(file_path, [])

            # Skip new files — no vanilla version to back up
            if all(d.get("is_new") for d in delta_infos) and delta_infos:
                continue

            has_bsdiff = self._has_bsdiff_delta(file_path)

            if has_bsdiff:
                full_path = self._vanilla_dir / file_path.replace("/", "\\")
                if not full_path.exists():
                    game_path = self._game_dir / file_path.replace("/", "\\")
                    if game_path.exists():
                        # Validate: game file must match snapshot before backing up
                        if not self._verify_is_vanilla(game_path, file_path, snap_hashes):
                            logger.warning(
                                "Skipping backup of %s — file doesn't match snapshot "
                                "(may be modded). Revert will use range backup or "
                                "require Steam verify.", file_path)
                            continue
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        _backup_copy(game_path, full_path)
                        logger.info("Full vanilla backup: %s", file_path)
            else:
                # Byte-range backup — only the positions mods touch
                ranges = self._get_all_byte_ranges(file_path)
                if ranges:
                    _save_range_backup(
                        self._game_dir, self._vanilla_dir, file_path, ranges)

    def _verify_is_vanilla(self, game_path: Path, file_path: str,
                           snap_hashes: dict[str, tuple[str, int]]) -> bool:
        """Check if a game file matches its snapshot hash (is truly vanilla)."""
        snap = snap_hashes.get(file_path)
        if snap is None:
            return False  # not in snapshot = not a vanilla file

        snap_hash, snap_size = snap
        # Quick size check first
        try:
            if game_path.stat().st_size != snap_size:
                return False
        except OSError:
            return False

        # Full hash check for small files (<50MB). For large files, trust
        # the size match — hashing 900MB PAZ on every apply is too slow.
        if snap_size < 50 * 1024 * 1024:
            from cdumm.engine.snapshot_manager import hash_file
            try:
                current_hash, _ = hash_file(game_path)
                return current_hash == snap_hash
            except Exception:
                return False

        return True  # large file, size matches

    def _has_bsdiff_delta(self, file_path: str) -> bool:
        """Check if any mod delta for this file is bsdiff format."""
        cursor = self._db.connection.execute(
            "SELECT md.delta_path FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE md.file_path = ? AND m.mod_type = 'paz'",
            (file_path,),
        )
        for (delta_path,) in cursor.fetchall():
            try:
                with open(delta_path, "rb") as f:
                    magic = f.read(4)
                if magic != SPARSE_MAGIC:
                    return True
            except OSError:
                continue
        return False

    def _get_all_byte_ranges(self, file_path: str) -> list[tuple[int, int]]:
        """Get union of all mod byte ranges for a file."""
        cursor = self._db.connection.execute(
            "SELECT byte_start, byte_end FROM mod_deltas "
            "WHERE file_path = ? AND byte_start IS NOT NULL",
            (file_path,),
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]

    def _compose_file(self, file_path: str, deltas: list[dict]) -> bytes | None:
        """Compose a file by starting from vanilla and applying deltas.

        Handles three delta types:
        - ENTR (entry-level): decompressed PAMT entry content, repacked per-entry
        - FULL_COPY/bsdiff: replace entire file
        - SPRS: sparse byte-level patches

        ENTR deltas are applied first (different entries compose perfectly),
        then byte-level deltas on top for backward compatibility.
        """
        from cdumm.engine.delta_engine import ENTRY_MAGIC, load_entry_delta

        # Separate entry-level and byte-level deltas
        entry_deltas = [d for d in deltas if d.get("entry_path")]
        byte_deltas = [d for d in deltas if not d.get("entry_path")]

        # Get vanilla content
        full_vanilla = self._vanilla_dir / file_path.replace("/", "\\")
        if full_vanilla.exists():
            current = full_vanilla.read_bytes()
        else:
            game_path = self._game_dir / file_path.replace("/", "\\")
            if not game_path.exists():
                logger.warning("Game file not found: %s", file_path)
                return None

            current_buf = bytearray(game_path.read_bytes())
            range_entries = _load_range_backup(self._vanilla_dir, file_path)
            if range_entries:
                _apply_ranges_to_buf(current_buf, range_entries)
            current = bytes(current_buf)

        vanilla_size = len(current)

        # ── Entry-level deltas (script mods) ───────────────────────
        if entry_deltas:
            current = self._apply_entry_deltas(
                file_path, bytearray(current), entry_deltas)

        # ── Byte-level deltas (zip/JSON/legacy mods) ───────────────
        if not byte_deltas:
            return current

        # Classify byte deltas by type
        full_replace = []
        sprs_shifted = []
        size_preserving = []

        for d in byte_deltas:
            dp = Path(d["delta_path"])
            try:
                with open(dp, "rb") as f:
                    magic = f.read(4)
            except OSError:
                continue

            if magic == b"FULL" or (magic == b"BSDI"):
                full_replace.append(d)
            elif _delta_changes_size(dp, vanilla_size):
                sprs_shifted.append(d)
            else:
                size_preserving.append(d)

        # Step 1: Apply full-replace deltas (last one wins if multiple)
        for d in full_replace:
            current = apply_delta_from_file(current, Path(d["delta_path"]))
            logger.info("Applied full-replace delta for %s from %s",
                        file_path, d.get("mod_name", "?"))

        # Step 2: Apply SPRS deltas that shift file size
        for d in sprs_shifted:
            current = apply_delta_from_file(current, Path(d["delta_path"]))

        if not size_preserving:
            return current

        # Step 3: Apply same-size SPRS patches on top
        shift = len(current) - vanilla_size
        if shift != 0 and (full_replace or sprs_shifted):
            if sprs_shifted:
                insertion_point = _find_insertion_point(
                    Path(sprs_shifted[0]["delta_path"]))
            else:
                insertion_point = vanilla_size

            if insertion_point < vanilla_size:
                logger.info(
                    "PAZ shift detected: %+d bytes at offset %d, "
                    "adjusting %d remaining delta(s)",
                    shift, insertion_point, len(size_preserving))
                result = bytearray(current)
                for d in size_preserving:
                    _apply_sparse_shifted(
                        result, Path(d["delta_path"]), insertion_point, shift)
                return bytes(result)

        for d in size_preserving:
            current = apply_delta_from_file(current, Path(d["delta_path"]))
        return current

    def _apply_entry_deltas(self, file_path: str, buf: bytearray,
                            entry_deltas: list[dict]) -> bytes:
        """Apply entry-level deltas to a PAZ file buffer.

        Each entry delta stores decompressed file content + PAMT entry metadata.
        The content is recompressed and written at the entry's offset in the PAZ.
        If the recompressed data doesn't fit, it's appended to the end.

        PAMT updates are tracked in self._pamt_entry_updates for Phase 2.
        """
        from cdumm.archive.paz_parse import PazEntry
        from cdumm.archive.paz_repack import repack_entry_bytes
        from cdumm.engine.delta_engine import load_entry_delta

        pamt_dir = file_path.split("/")[0]

        # Group by entry_path — last mod (highest priority in sorted order) wins
        by_entry: dict[str, dict] = {}
        for d in entry_deltas:
            by_entry[d["entry_path"]] = d

        for entry_path, d in by_entry.items():
            try:
                content, metadata = load_entry_delta(Path(d["delta_path"]))
            except Exception as e:
                logger.warning("Failed to load entry delta %s: %s",
                               d["delta_path"], e)
                continue

            entry = PazEntry(
                path=metadata["entry_path"],
                paz_file="",
                offset=metadata["vanilla_offset"],
                comp_size=metadata["vanilla_comp_size"],
                orig_size=metadata["vanilla_orig_size"],
                flags=metadata["flags"],
                paz_index=metadata["paz_index"],
            )

            try:
                payload, actual_comp, actual_orig = repack_entry_bytes(
                    content, entry, allow_size_change=True)
            except Exception as e:
                logger.warning("Failed to repack entry %s: %s", entry_path, e)
                continue

            new_offset = entry.offset
            new_paz_size = None

            if actual_comp > entry.comp_size:
                # Doesn't fit — append to end of PAZ
                new_offset = len(buf)
                buf.extend(payload)
                new_paz_size = len(buf)
                logger.info("Entry %s appended at offset %d (grew %d->%d)",
                            entry_path, new_offset, entry.comp_size, actual_comp)
            else:
                # Fits in original slot
                buf[entry.offset:entry.offset + len(payload)] = payload

            # Track PAMT update for Phase 2
            self._pamt_entry_updates.setdefault(pamt_dir, []).append({
                "entry": entry,
                "new_comp_size": actual_comp,
                "new_offset": new_offset,
                "new_orig_size": actual_orig,
                "new_paz_size": new_paz_size,
            })

            logger.info("Applied entry delta: %s in %s from %s",
                        entry_path, file_path, d.get("mod_name", "?"))

        return bytes(buf)

    def _compose_pamt(self, pamt_path: str, pamt_dir: str,
                      byte_deltas: list[dict],
                      entry_updates: list[dict]) -> bytes | None:
        """Compose a PAMT file from vanilla + entry updates + byte deltas.

        Entry updates come from PAZ entry-level composition (Phase 1).
        Byte deltas come from non-script mods that modify the PAMT directly.
        """
        vanilla = self._get_vanilla_bytes(pamt_path)
        if vanilla is None:
            game_path = self._game_dir / pamt_path.replace("/", "\\")
            if game_path.exists():
                vanilla = game_path.read_bytes()
            else:
                logger.warning("PAMT not found: %s", pamt_path)
                return None

        buf = bytearray(vanilla)

        # Apply entry-level PAMT updates (from PAZ entry composition)
        for update in entry_updates:
            _apply_pamt_entry_update(buf, update)

        # Apply byte-level PAMT deltas on top (from zip/JSON mods)
        if byte_deltas:
            current = bytes(buf)
            for d in byte_deltas:
                current = apply_delta_from_file(current, Path(d["delta_path"]))
            buf = bytearray(current)

        # Recompute PAMT hash
        from cdumm.archive.hashlittle import compute_pamt_hash
        correct_hash = compute_pamt_hash(bytes(buf))
        stored_hash = struct.unpack_from("<I", buf, 0)[0]
        if stored_hash != correct_hash:
            struct.pack_into("<I", buf, 0, correct_hash)
            logger.info("Recomputed PAMT hash for %s: %08X -> %08X",
                        pamt_path, stored_hash, correct_hash)

        return bytes(buf)

    def _get_vanilla_bytes(self, file_path: str) -> bytes | None:
        """Get vanilla version of a file from backup (range or full)."""
        # Try full backup first
        full_path = self._vanilla_dir / file_path.replace("/", "\\")
        if full_path.exists():
            return full_path.read_bytes()

        # Try range backup — reconstruct vanilla from game file + ranges
        game_path = self._game_dir / file_path.replace("/", "\\")
        if not game_path.exists():
            return None

        range_entries = _load_range_backup(self._vanilla_dir, file_path)
        if range_entries:
            buf = bytearray(game_path.read_bytes())
            _apply_ranges_to_buf(buf, range_entries)
            return bytes(buf)

        return None

    def _verify_vanilla_files(self, txn, active_files: set[str],
                              modified_pamts: dict[str, bytes]) -> None:
        """Safety net: find files that should be vanilla but aren't.

        After a mod is removed, its deltas are deleted from the DB. But the
        game files may still be modded. Two detection methods:
        1. Size mismatch vs snapshot (fast, catches most cases)
        2. Vanilla backup exists but no enabled mod manages the file
           (catches same-size modifications like PAMT byte patches)
        """
        import os

        try:
            cursor = self._db.connection.execute(
                "SELECT file_path, file_hash, file_size FROM snapshots")
            snap_map = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
        except Exception:
            return

        # Method 1: size mismatch
        for file_path, (snap_hash, snap_size) in snap_map.items():
            if file_path in active_files or file_path == "meta/0.papgt":
                continue
            game_file = self._game_dir / file_path.replace("/", os.sep)
            if not game_file.exists():
                continue
            try:
                actual_size = game_file.stat().st_size
            except OSError:
                continue
            if actual_size != snap_size:
                vanilla_bytes = self._get_vanilla_bytes(file_path)
                if vanilla_bytes:
                    txn.stage_file(file_path, vanilla_bytes)
                    if file_path.endswith(".pamt"):
                        modified_pamts[file_path.split("/")[0]] = vanilla_bytes
                    logger.warning("Restored orphaned file to vanilla: %s "
                                   "(size %d != snapshot %d)",
                                   file_path, actual_size, snap_size)

        # Method 2: vanilla backup exists but file isn't actively managed.
        # If we have a backup (range or full) for a file, it was previously
        # modified. If no enabled mod touches it now, restore it.
        if not self._vanilla_dir or not self._vanilla_dir.exists():
            return
        for backup in self._vanilla_dir.rglob("*"):
            if not backup.is_file():
                continue
            # Determine the game file path from backup path
            if backup.name.endswith(".vranges"):
                # Range backup: filename is file_path with / replaced by _
                rel = backup.name[:-len(".vranges")].replace("_", "/")
            else:
                rel = str(backup.relative_to(self._vanilla_dir)).replace("\\", "/")

            if rel in active_files or rel == "meta/0.papgt":
                continue
            if rel not in snap_map:
                continue

            game_file = self._game_dir / rel.replace("/", os.sep)
            if not game_file.exists():
                continue

            # This file has a backup but no enabled mod manages it — restore
            vanilla_bytes = self._get_vanilla_bytes(rel)
            if vanilla_bytes:
                snap_hash, snap_size = snap_map[rel]
                # Only restore if file actually differs from vanilla
                import hashlib
                if len(vanilla_bytes) == snap_size:
                    game_bytes = game_file.read_bytes()
                    if game_bytes != vanilla_bytes:
                        txn.stage_file(rel, vanilla_bytes)
                        if rel.endswith(".pamt"):
                            modified_pamts[rel.split("/")[0]] = vanilla_bytes
                        logger.warning("Restored orphaned file to vanilla: %s "
                                       "(backup exists, no active mod)", rel)

    def _get_files_to_revert(self, enabled_files: set[str]) -> list[str]:
        """Find files modified by disabled mods that no enabled mod covers."""
        cursor = self._db.connection.execute(
            "SELECT DISTINCT md.file_path "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 0 AND m.mod_type = 'paz'"
        )
        disabled_files = {row[0] for row in cursor.fetchall()}
        return sorted(disabled_files - enabled_files)

    def _get_new_files_to_delete(self, enabled_files: set[str]) -> set[str]:
        """Find new files from disabled mods that no enabled mod provides."""
        cursor = self._db.connection.execute(
            "SELECT DISTINCT md.file_path "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 0 AND m.mod_type = 'paz' AND md.is_new = 1"
        )
        disabled_new = {row[0] for row in cursor.fetchall()}
        # Don't delete if an enabled mod also provides this new file
        return disabled_new - enabled_files

    def _get_file_deltas(self) -> dict[str, list[dict]]:
        """Get all deltas for enabled mods, grouped by file path."""
        cursor = self._db.connection.execute(
            "SELECT DISTINCT md.file_path, md.delta_path, m.name, "
            "md.is_new, md.entry_path "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 1 AND m.mod_type = 'paz' "
            "ORDER BY m.priority DESC, md.file_path"
        )

        file_deltas: dict[str, list[dict]] = {}
        seen_deltas: set[str] = set()

        for file_path, delta_path, mod_name, is_new, entry_path in cursor.fetchall():
            if delta_path in seen_deltas:
                continue
            seen_deltas.add(delta_path)
            d = {
                "delta_path": delta_path,
                "mod_name": mod_name,
                "is_new": bool(is_new),
            }
            if entry_path:
                d["entry_path"] = entry_path
            file_deltas.setdefault(file_path, []).append(d)

        return file_deltas


class RevertWorker(QObject):
    """Background worker for revert operation."""

    progress_updated = Signal(int, str)
    finished = Signal()
    error_occurred = Signal(str)

    def __init__(self, game_dir: Path, vanilla_dir: Path, db_path: Path) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._vanilla_dir = vanilla_dir
        self._db_path = db_path

    def run(self) -> None:
        try:
            self._db = Database(self._db_path)
            self._db.initialize()
            self._revert()
            self._db.close()
        except Exception as e:
            logger.error("Revert failed: %s", e, exc_info=True)
            self.error_occurred.emit(f"Revert failed: {e}")

    def _revert(self) -> None:
        """Revert all mod-affected files to vanilla using range or full backups."""
        # Get all files any mod has ever touched
        cursor = self._db.connection.execute(
            "SELECT DISTINCT file_path, is_new FROM mod_deltas")
        rows = cursor.fetchall()
        mod_files = [row[0] for row in rows]
        new_files = {row[0] for row in rows if row[1]}

        if not mod_files:
            self.error_occurred.emit("No mod data found. Nothing to revert.")
            return

        total = len(mod_files)
        self.progress_updated.emit(0, f"Reverting {total} file(s) to vanilla...")

        staging_dir = self._game_dir / ".cdumm_staging"
        staging_dir.mkdir(exist_ok=True)
        txn = TransactionalIO(self._game_dir, staging_dir)

        reverted = 0
        try:
            for i, file_path in enumerate(mod_files):
                pct = int((i / total) * 90)
                self.progress_updated.emit(pct, f"Restoring {file_path}...")

                if file_path in new_files:
                    # New file — delete it (didn't exist in vanilla)
                    game_path = self._game_dir / file_path.replace("/", "\\")
                    if game_path.exists():
                        game_path.unlink()
                        logger.info("Deleted mod-added file: %s", file_path)
                        reverted += 1
                    continue

                vanilla_bytes = self._get_vanilla_bytes(file_path)
                if vanilla_bytes:
                    txn.stage_file(file_path, vanilla_bytes)
                    reverted += 1
                else:
                    logger.warning("Cannot revert %s — no backup found", file_path)

            if reverted == 0:
                self.error_occurred.emit(
                    "No vanilla backups found. Use Steam 'Verify Integrity' to restore.")
                return

            # Clean up orphan mod directories (0036+) that are empty or
            # only existed because of standalone mods
            self.progress_updated.emit(91, "Cleaning orphan directories...")
            for d in sorted(self._game_dir.iterdir()):
                if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                    continue
                if int(d.name) < 36:
                    continue
                # Check if this directory is in the snapshot (vanilla)
                snap_check = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                    (d.name + "/%",),
                ).fetchone()[0]
                if snap_check == 0:
                    # Not in snapshot — orphan from mods, remove it
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                    logger.info("Removed orphan mod directory: %s", d.name)

            # Rebuild PAPGT from scratch (not from backups which may be stale)
            self.progress_updated.emit(92, "Rebuilding PAPGT...")
            papgt_mgr = PapgtManager(self._game_dir, self._vanilla_dir)
            try:
                papgt_bytes = papgt_mgr.rebuild()
                txn.stage_file("meta/0.papgt", papgt_bytes)
            except FileNotFoundError:
                pass

            self.progress_updated.emit(95, "Committing revert...")
            txn.commit()

            self.progress_updated.emit(100, "Revert complete!")
            self.finished.emit()

        except Exception:
            txn.cleanup_staging()
            raise
        finally:
            txn.cleanup_staging()

    def _get_vanilla_bytes(self, file_path: str) -> bytes | None:
        """Get vanilla version from full backup or range backup."""
        full_path = self._vanilla_dir / file_path.replace("/", "\\")
        if full_path.exists():
            return full_path.read_bytes()

        game_path = self._game_dir / file_path.replace("/", "\\")
        if not game_path.exists():
            return None

        range_entries = _load_range_backup(self._vanilla_dir, file_path)
        if range_entries:
            buf = bytearray(game_path.read_bytes())
            _apply_ranges_to_buf(buf, range_entries)
            return bytes(buf)

        return None
