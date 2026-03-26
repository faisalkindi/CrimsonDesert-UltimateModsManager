"""Apply engine — composes enabled mod deltas into a valid game state.

Pipeline:
  1. Read vanilla files from backup
  2. Apply each enabled mod's bsdiff4 delta in sequence
  3. Rebuild PAPGT from scratch
  4. Stage all modified files
  5. Atomic commit (transactional I/O)
"""
import logging
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from cdmm.archive.papgt_manager import PapgtManager
from cdmm.archive.transactional_io import TransactionalIO
from cdmm.engine.delta_engine import apply_delta, load_delta
from cdmm.storage.database import Database

logger = logging.getLogger(__name__)


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
        # Get all enabled mods with their deltas, grouped by file
        file_deltas = self._get_file_deltas()
        if not file_deltas:
            self.error_occurred.emit("No enabled mods with changes to apply.")
            return

        total_files = len(file_deltas)
        self.progress_updated.emit(0, f"Applying {total_files} file(s)...")

        # Create staging directory on same filesystem as game
        staging_dir = self._game_dir / ".cdmm_staging"
        staging_dir.mkdir(exist_ok=True)

        txn = TransactionalIO(self._game_dir, staging_dir)
        modified_pamts: dict[str, bytes] = {}

        try:
            # For each file, start from vanilla and apply all deltas
            for i, (file_path, deltas) in enumerate(file_deltas.items()):
                pct = int((i / total_files) * 80)
                self.progress_updated.emit(pct, f"Processing {file_path}...")

                # Read vanilla version
                vanilla_path = self._vanilla_dir / file_path.replace("/", "\\")
                if not vanilla_path.exists():
                    # Fallback: read from game directory (might be unmodified)
                    vanilla_path = self._game_dir / file_path.replace("/", "\\")

                if not vanilla_path.exists():
                    logger.warning("Vanilla file not found: %s, skipping", file_path)
                    continue

                current_bytes = vanilla_path.read_bytes()

                # Apply each delta in sequence
                for delta_info in deltas:
                    delta_bytes = load_delta(Path(delta_info["delta_path"]))
                    current_bytes = apply_delta(current_bytes, delta_bytes)

                # Stage the result
                txn.stage_file(file_path, current_bytes)

                # Track PAMT files for PAPGT rebuild
                if file_path.endswith(".pamt"):
                    dir_name = file_path.split("/")[0]
                    modified_pamts[dir_name] = current_bytes

            # Rebuild PAPGT
            self.progress_updated.emit(85, "Rebuilding PAPGT integrity chain...")
            papgt_mgr = PapgtManager(self._game_dir)
            try:
                papgt_bytes = papgt_mgr.rebuild(modified_pamts)
                txn.stage_file("meta/0.papgt", papgt_bytes)
            except FileNotFoundError:
                logger.warning("PAPGT not found, skipping integrity chain rebuild")

            # Commit atomically
            self.progress_updated.emit(95, "Committing changes...")
            txn.commit()

            self.progress_updated.emit(100, "Apply complete!")
            self.finished.emit()

        except Exception:
            txn.cleanup_staging()
            raise
        finally:
            txn.cleanup_staging()

    def _get_file_deltas(self) -> dict[str, list[dict]]:
        """Get all deltas for enabled mods, grouped by file path.

        Returns {file_path: [{delta_path, mod_name}, ...]} in mod order.
        """
        cursor = self._db.connection.execute(
            "SELECT DISTINCT md.file_path, md.delta_path, m.name "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 1 AND m.mod_type = 'paz' "
            "ORDER BY m.priority DESC, md.file_path"
        )

        file_deltas: dict[str, list[dict]] = {}
        seen_deltas: set[str] = set()  # deduplicate by delta_path

        for file_path, delta_path, mod_name in cursor.fetchall():
            if delta_path in seen_deltas:
                continue
            seen_deltas.add(delta_path)
            file_deltas.setdefault(file_path, []).append({
                "delta_path": delta_path,
                "mod_name": mod_name,
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
            self._revert()
        except Exception as e:
            logger.error("Revert failed: %s", e, exc_info=True)
            self.error_occurred.emit(f"Revert failed: {e}")

    def _revert(self) -> None:
        # Find all files that have vanilla backups
        if not self._vanilla_dir.exists():
            self.error_occurred.emit("No vanilla backups found. Nothing to revert.")
            return

        backup_files: list[tuple[str, Path]] = []
        for f in self._vanilla_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(self._vanilla_dir).as_posix()
                backup_files.append((rel, f))

        if not backup_files:
            self.error_occurred.emit("No vanilla backup files found.")
            return

        total = len(backup_files)
        self.progress_updated.emit(0, f"Reverting {total} file(s) to vanilla...")

        staging_dir = self._game_dir / ".cdmm_staging"
        staging_dir.mkdir(exist_ok=True)
        txn = TransactionalIO(self._game_dir, staging_dir)

        try:
            for i, (rel_path, backup_path) in enumerate(backup_files):
                pct = int((i / total) * 90)
                self.progress_updated.emit(pct, f"Restoring {rel_path}...")
                txn.stage_file(rel_path, backup_path.read_bytes())

            self.progress_updated.emit(95, "Committing revert...")
            txn.commit()

            self.progress_updated.emit(100, "Revert complete!")
            self.finished.emit()

        except Exception:
            txn.cleanup_staging()
            raise
        finally:
            txn.cleanup_staging()
