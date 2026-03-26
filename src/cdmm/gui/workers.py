"""QObject workers for background operations."""
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from cdmm.engine.import_handler import (
    detect_format,
    import_from_bsdiff,
    import_from_folder,
    import_from_script,
    import_from_zip,
)
from cdmm.engine.snapshot_manager import SnapshotManager
from cdmm.storage.database import Database

logger = logging.getLogger(__name__)


class ImportWorker(QObject):
    """Background worker for PAZ mod import. Creates its own DB connection."""

    progress_updated = Signal(int, str)
    finished = Signal(object)  # ModImportResult
    error_occurred = Signal(str)

    def __init__(self, mod_path: Path, game_dir: Path, db_path: Path,
                 deltas_dir: Path, existing_mod_id: int | None = None) -> None:
        super().__init__()
        self._mod_path = mod_path
        self._game_dir = game_dir
        self._db_path = db_path
        self._deltas_dir = deltas_dir
        self._existing_mod_id = existing_mod_id

    def run(self) -> None:
        try:
            # Create thread-local DB connection
            db = Database(self._db_path)
            db.initialize()
            snapshot = SnapshotManager(db)

            fmt = detect_format(self._mod_path)
            self.progress_updated.emit(0, f"Detected format: {fmt}")
            logger.info("ImportWorker: format=%s path=%s", fmt, self._mod_path)

            if fmt == "zip":
                result = import_from_zip(
                    self._mod_path, self._game_dir, db, snapshot, self._deltas_dir,
                    existing_mod_id=self._existing_mod_id)
            elif fmt == "folder":
                result = import_from_folder(
                    self._mod_path, self._game_dir, db, snapshot, self._deltas_dir,
                    existing_mod_id=self._existing_mod_id)
            elif fmt == "script":
                self.progress_updated.emit(10, "Executing script in sandbox...")
                result = import_from_script(
                    self._mod_path, self._game_dir, db, snapshot, self._deltas_dir)
            elif fmt == "bsdiff":
                result = import_from_bsdiff(
                    self._mod_path, self._game_dir, db, snapshot, self._deltas_dir)
            else:
                self.error_occurred.emit(f"Unsupported format: {fmt}")
                db.close()
                return

            db.close()

            if result.error:
                self.error_occurred.emit(result.error)
            else:
                self.finished.emit(result)

        except Exception as e:
            logger.error("Import failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))


class PreHashWorker(QObject):
    """Background worker that hashes all game files before a script runs."""

    progress_updated = Signal(int, str)
    finished = Signal(object)  # dict[str, str] of rel_path -> hash
    error_occurred = Signal(str)

    def __init__(self, game_dir: Path, db_path: Path) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._db_path = db_path

    def run(self) -> None:
        try:
            from cdmm.engine.snapshot_manager import hash_file as _hash_file

            db = Database(self._db_path)
            db.initialize()

            cursor = db.connection.execute("SELECT file_path FROM snapshots")
            all_files = [row[0] for row in cursor.fetchall()]
            db.close()

            total = len(all_files)
            self.progress_updated.emit(0, f"Hashing {total} game files...")
            logger.info("PreHashWorker: hashing %d files", total)

            pre_hashes: dict[str, str] = {}
            for i, rel_path in enumerate(all_files):
                game_file = self._game_dir / rel_path.replace("/", "\\")
                if game_file.exists():
                    h, _ = _hash_file(game_file)
                    pre_hashes[rel_path] = h

                if (i + 1) % 5 == 0 or (i + 1) == total:
                    pct = int((i + 1) / total * 100)
                    self.progress_updated.emit(pct, f"Hashed {i + 1}/{total} files...")

            logger.info("PreHashWorker: done, %d files hashed", len(pre_hashes))
            self.finished.emit(pre_hashes)

        except Exception as e:
            logger.error("Pre-hash failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))


class ScriptCaptureWorker(QObject):
    """Background worker that captures game file changes after a script ran."""

    progress_updated = Signal(int, str)
    finished = Signal(object)  # ModImportResult
    error_occurred = Signal(str)

    def __init__(self, mod_name: str, pre_hashes: dict[str, str],
                 game_dir: Path, db_path: Path, deltas_dir: Path) -> None:
        super().__init__()
        self._mod_name = mod_name
        self._pre_hashes = pre_hashes
        self._game_dir = game_dir
        self._db_path = db_path
        self._deltas_dir = deltas_dir

    def run(self) -> None:
        try:
            from cdmm.engine.snapshot_manager import hash_file as _hash_file
            from cdmm.engine.delta_engine import generate_delta, get_changed_byte_ranges, save_delta
            from cdmm.engine.import_handler import ModImportResult

            db = Database(self._db_path)
            db.initialize()

            self.progress_updated.emit(0, "Detecting changed files...")

            # Find which files changed
            changed: list[str] = []
            for rel_path, old_hash in self._pre_hashes.items():
                game_file = self._game_dir / rel_path.replace("/", "\\")
                if game_file.exists():
                    new_hash, _ = _hash_file(game_file)
                    if new_hash != old_hash:
                        changed.append(rel_path)

            if not changed:
                result = ModImportResult(self._mod_name)
                result.error = (
                    "No new changes detected. This mod may already be applied.\n\n"
                    "To install it fresh:\n"
                    "1. Click 'Revert to Vanilla' to restore original game files\n"
                    "2. Then re-import all your mods through the app"
                )
                self.finished.emit(result)
                db.close()
                return

            logger.info("Script changed %d files: %s", len(changed), changed)
            self.progress_updated.emit(20, f"Found {len(changed)} changed file(s). Generating deltas...")

            # Generate deltas
            vanilla_dir = self._deltas_dir.parent / "vanilla"
            priority_cursor = db.connection.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM mods")
            next_priority = priority_cursor.fetchone()[0]
            cursor = db.connection.execute(
                "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
                (self._mod_name, "paz", next_priority),
            )
            mod_id = cursor.lastrowid
            result = ModImportResult(self._mod_name)

            for idx, rel_path in enumerate(changed):
                pct = 20 + int((idx + 1) / len(changed) * 70)
                self.progress_updated.emit(pct, f"Generating delta for {rel_path}...")

                vanilla_path = vanilla_dir / rel_path.replace("/", "\\")
                current_path = self._game_dir / rel_path.replace("/", "\\")

                if not vanilla_path.exists():
                    logger.warning("No vanilla backup for %s", rel_path)
                    continue

                vanilla_bytes = vanilla_path.read_bytes()
                modified_bytes = current_path.read_bytes()

                delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
                byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

                safe_name = rel_path.replace("/", "_") + ".bsdiff"
                delta_path = self._deltas_dir / str(mod_id) / safe_name
                save_delta(delta_bytes, delta_path)

                for bs, be in byte_ranges:
                    db.connection.execute(
                        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (mod_id, rel_path, str(delta_path), bs, be),
                    )

                result.changed_files.append({
                    "file_path": rel_path,
                    "delta_path": str(delta_path),
                    "byte_ranges": byte_ranges,
                })

            db.connection.commit()
            db.close()

            self.progress_updated.emit(100, "Done!")
            self.finished.emit(result)

        except Exception as e:
            logger.error("Script capture failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))


class ScanChangesWorker(QObject):
    """Background worker that scans game files vs snapshot and captures changes."""

    progress_updated = Signal(int, str)
    finished = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, mod_name: str, game_dir: Path, db_path: Path,
                 deltas_dir: Path) -> None:
        super().__init__()
        self._mod_name = mod_name
        self._game_dir = game_dir
        self._db_path = db_path
        self._deltas_dir = deltas_dir

    @staticmethod
    def _get_existing_deltas(db: "Database") -> dict[str, list[dict]]:
        """Return deltas from all enabled paz mods, grouped by file path.

        Result: ``{file_path: [{delta_path, mod_name}, ...]}`` in priority
        order (same order the apply engine uses).
        """
        cursor = db.connection.execute(
            "SELECT DISTINCT md.file_path, md.delta_path, m.name "
            "FROM mod_deltas md "
            "JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 1 AND m.mod_type = 'paz' "
            "ORDER BY m.priority DESC, md.file_path"
        )

        file_deltas: dict[str, list[dict]] = {}
        seen: set[str] = set()
        for file_path, delta_path, mod_name in cursor.fetchall():
            if delta_path in seen:
                continue
            seen.add(delta_path)
            file_deltas.setdefault(file_path, []).append({
                "delta_path": delta_path,
                "mod_name": mod_name,
            })
        return file_deltas

    def run(self) -> None:
        try:
            from cdmm.engine.snapshot_manager import hash_file as _hash_file
            from cdmm.engine.delta_engine import (
                apply_delta, generate_delta, get_changed_byte_ranges,
                load_delta, save_delta,
            )
            from cdmm.engine.import_handler import ModImportResult

            db = Database(self._db_path)
            db.initialize()

            # Get all snapshot hashes
            cursor = db.connection.execute("SELECT file_path, file_hash FROM snapshots")
            snapshot_rows = cursor.fetchall()
            total = len(snapshot_rows)

            self.progress_updated.emit(0, f"Scanning {total} game files...")
            logger.info("ScanChangesWorker: scanning %d files", total)

            # Find changed files
            changed: list[str] = []
            for i, (rel_path, stored_hash) in enumerate(snapshot_rows):
                abs_path = self._game_dir / rel_path.replace("/", "\\")
                if not abs_path.exists():
                    continue

                current_hash, _ = _hash_file(abs_path)
                if current_hash != stored_hash:
                    changed.append(rel_path)

                if (i + 1) % 10 == 0 or (i + 1) == total:
                    pct = int((i + 1) / total * 50)
                    self.progress_updated.emit(pct, f"Scanned {i + 1}/{total} files...")

            if not changed:
                result = ModImportResult(self._mod_name)
                result.error = "No changes detected. Game files match the vanilla snapshot."
                self.finished.emit(result)
                db.close()
                return

            logger.info("Found %d changed files: %s", len(changed), changed)
            self.progress_updated.emit(55, f"Found {len(changed)} changed file(s). Generating deltas...")

            # Generate deltas
            vanilla_dir = self._deltas_dir.parent / "vanilla"
            priority_cursor = db.connection.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM mods")
            next_priority = priority_cursor.fetchone()[0]
            cursor = db.connection.execute(
                "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
                (self._mod_name, "paz", next_priority),
            )
            mod_id = cursor.lastrowid
            result = ModImportResult(self._mod_name)

            # Pre-fetch existing enabled mod deltas so we can compute
            # incremental changes instead of re-capturing everything.
            existing_deltas_by_file = self._get_existing_deltas(db)

            for idx, rel_path in enumerate(changed):
                pct = 55 + int((idx + 1) / len(changed) * 40)
                self.progress_updated.emit(pct, f"Delta: {rel_path}...")

                vanilla_path = vanilla_dir / rel_path.replace("/", "\\")
                current_path = self._game_dir / rel_path.replace("/", "\\")

                if not vanilla_path.exists():
                    logger.warning("No vanilla backup for %s", rel_path)
                    continue

                vanilla_bytes = vanilla_path.read_bytes()
                modified_bytes = current_path.read_bytes()

                # Compute the "expected" state by replaying existing mod
                # deltas on top of vanilla.  The incremental delta is then
                # expected -> current rather than vanilla -> current, so we
                # only capture the NEW mod's changes.
                existing = existing_deltas_by_file.get(rel_path, [])
                if existing:
                    expected_bytes = vanilla_bytes
                    for delta_info in existing:
                        existing_delta = load_delta(Path(delta_info["delta_path"]))
                        expected_bytes = apply_delta(expected_bytes, existing_delta)
                    base_bytes = expected_bytes
                    logger.info(
                        "Incremental delta for %s (applied %d existing mod deltas)",
                        rel_path, len(existing),
                    )
                else:
                    base_bytes = vanilla_bytes

                # If the file matches the expected state, no new changes
                if base_bytes == modified_bytes:
                    logger.info("File %s matches expected state, skipping", rel_path)
                    continue

                delta_bytes = generate_delta(base_bytes, modified_bytes)
                byte_ranges = get_changed_byte_ranges(base_bytes, modified_bytes)

                safe_name = rel_path.replace("/", "_") + ".bsdiff"
                delta_path = self._deltas_dir / str(mod_id) / safe_name
                save_delta(delta_bytes, delta_path)

                for bs, be in byte_ranges:
                    db.connection.execute(
                        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (mod_id, rel_path, str(delta_path), bs, be),
                    )

                result.changed_files.append({
                    "file_path": rel_path,
                    "byte_ranges": byte_ranges,
                })

            db.connection.commit()
            db.close()

            self.progress_updated.emit(100, "Done!")
            self.finished.emit(result)

        except Exception as e:
            logger.error("Scan failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))
