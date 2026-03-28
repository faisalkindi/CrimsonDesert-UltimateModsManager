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
    """Check if a sparse delta produces a file of different size than vanilla."""
    try:
        with open(delta_path, "rb") as f:
            magic = f.read(4)
            if magic != SPARSE_MAGIC:
                return False  # bsdiff — can't easily tell without applying
            count = struct.unpack("<I", f.read(4))[0]
            # Check if any patch entry writes past vanilla_size
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

        all_files = set(file_deltas.keys()) | set(revert_files)
        total_files = len(all_files)
        self.progress_updated.emit(0, f"Applying {total_files} file(s)...")

        # Ensure vanilla backups exist BEFORE any modifications.
        self.progress_updated.emit(2, "Backing up vanilla byte ranges...")
        self._ensure_backups(file_deltas, revert_files)

        staging_dir = self._game_dir / ".cdumm_staging"
        staging_dir.mkdir(exist_ok=True)
        txn = TransactionalIO(self._game_dir, staging_dir)
        modified_pamts: dict[str, bytes] = {}

        try:
            file_idx = 0

            # Apply enabled mod deltas
            for file_path, deltas in file_deltas.items():
                pct = int((file_idx / total_files) * 80)
                self.progress_updated.emit(pct, f"Processing {file_path}...")
                file_idx += 1

                # Skip PAPGT — it's always rebuilt from scratch at the end
                if file_path == "meta/0.papgt":
                    continue

                # New files: copy from stored full file (last mod wins)
                new_deltas = [d for d in deltas if d.get("is_new")]
                mod_deltas = [d for d in deltas if not d.get("is_new")]

                if new_deltas and not mod_deltas:
                    # Purely new file — use the last (highest priority) copy
                    src = Path(new_deltas[-1]["delta_path"])
                    if src.exists():
                        result_bytes = src.read_bytes()
                        txn.stage_file(file_path, result_bytes)
                        if file_path.endswith(".pamt"):
                            modified_pamts[file_path.split("/")[0]] = result_bytes
                        logger.info("Applying new file: %s from %s",
                                    file_path, new_deltas[-1]["mod_name"])
                    continue

                result_bytes = self._compose_file(file_path, mod_deltas)
                if result_bytes is None:
                    continue

                txn.stage_file(file_path, result_bytes)
                if file_path.endswith(".pamt"):
                    modified_pamts[file_path.split("/")[0]] = result_bytes

            # Revert files from disabled mods
            new_files_to_delete = self._get_new_files_to_delete(set(file_deltas.keys()))
            for file_path in revert_files:
                pct = int((file_idx / total_files) * 80)
                self.progress_updated.emit(pct, f"Reverting {file_path}...")
                file_idx += 1

                if file_path in new_files_to_delete:
                    # New file from a disabled mod — delete it from game dir
                    game_path = self._game_dir / file_path.replace("/", "\\")
                    if game_path.exists():
                        game_path.unlink()
                        logger.info("Deleted new file from disabled mod: %s", file_path)
                    continue

                vanilla_bytes = self._get_vanilla_bytes(file_path)
                if vanilla_bytes is None:
                    logger.warning("Cannot revert %s — no backup", file_path)
                    continue

                txn.stage_file(file_path, vanilla_bytes)
                if file_path.endswith(".pamt"):
                    modified_pamts[file_path.split("/")[0]] = vanilla_bytes

            # Rebuild PAPGT
            self.progress_updated.emit(85, "Rebuilding PAPGT integrity chain...")
            papgt_mgr = PapgtManager(self._game_dir)
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

        - Sparse-only files: byte-range backup (tiny — just the modified bytes)
        - Bsdiff files: full file backup (file is small anyway)
        """
        self._vanilla_dir.mkdir(parents=True, exist_ok=True)

        all_files = set(file_deltas.keys()) | set(revert_files)
        for file_path in all_files:
            delta_infos = file_deltas.get(file_path, [])

            # Skip new files — no vanilla version to back up
            if all(d.get("is_new") for d in delta_infos) and delta_infos:
                continue

            has_bsdiff = self._has_bsdiff_delta(file_path)

            if has_bsdiff:
                # Full file backup — use hard link (instant, zero extra space)
                # with copy fallback for cross-drive or filesystem issues.
                full_path = self._vanilla_dir / file_path.replace("/", "\\")
                if not full_path.exists():
                    game_path = self._game_dir / file_path.replace("/", "\\")
                    if game_path.exists():
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        _backup_copy(game_path, full_path)
                        logger.info("Full vanilla backup: %s", file_path)
            else:
                # Byte-range backup — only the positions mods touch
                ranges = self._get_all_byte_ranges(file_path)
                if ranges:
                    _save_range_backup(
                        self._game_dir, self._vanilla_dir, file_path, ranges)

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

        Handles PAZ-shift conflicts: if one delta changes the file size
        (e.g., LootMultiplier's PAZ shift), it's applied first, then
        remaining deltas have their offsets adjusted to account for the shift.
        """
        # Always prefer full vanilla backup (most reliable restoration)
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

        # Separate deltas into size-changing (PAZ shift) and size-preserving
        size_changing = []
        size_preserving = []
        for d in deltas:
            if _delta_changes_size(Path(d["delta_path"]), vanilla_size):
                size_changing.append(d)
            else:
                size_preserving.append(d)

        # Apply size-changing deltas first
        for d in size_changing:
            current = apply_delta_from_file(current, Path(d["delta_path"]))

        if not size_preserving:
            return current

        # If file size changed, adjust offsets for remaining deltas
        shift = len(current) - vanilla_size
        if shift != 0 and size_changing:
            insertion_point = _find_insertion_point(
                Path(size_changing[0]["delta_path"]))
            logger.info(
                "PAZ shift detected: %+d bytes at offset %d, "
                "adjusting %d remaining delta(s)",
                shift, insertion_point, len(size_preserving))

            # Apply remaining deltas with offset adjustment
            result = bytearray(current)
            for d in size_preserving:
                _apply_sparse_shifted(
                    result, Path(d["delta_path"]), insertion_point, shift)
            return bytes(result)

        # No shift — apply normally
        for d in size_preserving:
            current = apply_delta_from_file(current, Path(d["delta_path"]))
        return current

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
            "SELECT DISTINCT md.file_path, md.delta_path, m.name, md.is_new "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 1 AND m.mod_type = 'paz' "
            "ORDER BY m.priority DESC, md.file_path"
        )

        file_deltas: dict[str, list[dict]] = {}
        seen_deltas: set[str] = set()

        for file_path, delta_path, mod_name, is_new in cursor.fetchall():
            if delta_path in seen_deltas:
                continue
            seen_deltas.add(delta_path)
            file_deltas.setdefault(file_path, []).append({
                "delta_path": delta_path,
                "mod_name": mod_name,
                "is_new": bool(is_new),
            })

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

            # Rebuild PAPGT from vanilla PAMTs
            self.progress_updated.emit(92, "Rebuilding PAPGT...")
            modified_pamts: dict[str, bytes] = {}
            for file_path in mod_files:
                if file_path.endswith(".pamt"):
                    vanilla_path = self._vanilla_dir / file_path.replace("/", "\\")
                    if vanilla_path.exists():
                        modified_pamts[file_path.split("/")[0]] = vanilla_path.read_bytes()

            papgt_mgr = PapgtManager(self._game_dir)
            try:
                papgt_bytes = papgt_mgr.rebuild(modified_pamts)
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
