"""QObject workers for background operations."""
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from cdumm.engine.import_handler import (
    detect_format,
    import_from_7z,
    import_from_bsdiff,
    import_from_folder,
    import_from_json_patch,
    import_from_script,
    import_from_zip,
)
from cdumm.engine.snapshot_manager import SnapshotManager
from cdumm.storage.database import Database

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
            elif fmt == "7z":
                result = import_from_7z(
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
            elif fmt == "json_patch":
                result = import_from_json_patch(
                    self._mod_path, self._game_dir, db, snapshot, self._deltas_dir,
                    existing_mod_id=self._existing_mod_id)
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
            from cdumm.engine.snapshot_manager import hash_file as _hash_file

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


class ScriptPrepWorker(QObject):
    """Background worker that backs up vanilla files, restores them, and pre-hashes."""

    progress_updated = Signal(int, str)
    finished = Signal(object)  # dict[str, str] pre_hashes or None
    error_occurred = Signal(str)

    def __init__(self, targeted: list[str], game_dir: Path, vanilla_dir: Path) -> None:
        super().__init__()
        self._targeted = targeted
        self._game_dir = game_dir
        self._vanilla_dir = vanilla_dir

    def run(self) -> None:
        try:
            import os
            import shutil
            from cdumm.engine.import_handler import _ensure_vanilla_backup
            from cdumm.engine.snapshot_manager import hash_file as _hash_file
            from cdumm.storage.database import Database

            total = len(self._targeted) if self._targeted else 0
            if total == 0:
                self.finished.emit(None)
                return

            # Load snapshot hashes to check what actually needs work
            db_path = self._vanilla_dir.parent / ".." / ".." / "AppData" / "Local" / "cdumm" / "cdumm.db"
            # Find the DB by walking up from vanilla_dir (CDMods/vanilla -> game_dir)
            # The DB is at AppData, but we can check snapshot inline
            snap_hashes: dict[str, str] = {}
            try:
                # Try to find DB path from standard location
                db_path = Path.home() / "AppData" / "Local" / "cdumm" / "cdumm.db"
                if db_path.exists():
                    db = Database(db_path)
                    db.initialize()
                    for rel_path in self._targeted:
                        row = db.connection.execute(
                            "SELECT file_hash FROM snapshots WHERE file_path = ?",
                            (rel_path,)).fetchone()
                        if row:
                            snap_hashes[rel_path] = row[0]
                    db.close()
            except Exception:
                pass  # proceed without snapshot optimization

            # Step 1: Back up and restore only files that need it
            backed_up = 0
            restored = 0
            for i, rel_path in enumerate(self._targeted):
                pct = int((i / total) * 50)
                game_file = self._game_dir / rel_path.replace("/", os.sep)
                vanilla_file = self._vanilla_dir / rel_path.replace("/", os.sep)

                if not game_file.exists():
                    continue

                # Check if game file already matches vanilla snapshot
                if rel_path in snap_hashes:
                    current_hash, _ = _hash_file(game_file)
                    if current_hash == snap_hashes[rel_path]:
                        # Already vanilla — just ensure backup exists, no restore needed
                        if not vanilla_file.exists():
                            self.progress_updated.emit(pct, f"Backing up {rel_path}...")
                            _ensure_vanilla_backup(self._game_dir, self._vanilla_dir, rel_path)
                            backed_up += 1
                        continue

                # File is modified — back up and restore
                if not vanilla_file.exists():
                    self.progress_updated.emit(pct, f"Backing up {rel_path}...")
                    _ensure_vanilla_backup(self._game_dir, self._vanilla_dir, rel_path)
                    backed_up += 1

                if vanilla_file.exists():
                    self.progress_updated.emit(pct, f"Restoring {rel_path}...")
                    shutil.copy2(str(vanilla_file), str(game_file))
                    restored += 1

            if backed_up:
                logger.info("Backed up %d vanilla files", backed_up)
            if restored:
                logger.info("Restored %d files to vanilla for clean import", restored)
            else:
                logger.info("All target files already vanilla, no restore needed")

            # Step 2: Pre-hash ALL game files (not just targets)
            # Use snapshot hashes for non-targeted files (instant).
            # Only actually hash targeted files (which were just restored).
            # This way ScriptCaptureWorker detects ALL changes, not just predicted.
            db_path2 = Path.home() / "AppData" / "Local" / "cdumm" / "cdumm.db"
            all_snap: dict[str, str] = {}
            try:
                if db_path2.exists():
                    db2 = Database(db_path2)
                    db2.initialize()
                    for row in db2.connection.execute("SELECT file_path, file_hash FROM snapshots").fetchall():
                        all_snap[row[0]] = row[1]
                    db2.close()
            except Exception:
                pass

            pre_hashes = {}
            all_files = list(all_snap.keys()) if all_snap else self._targeted
            hash_total = len(all_files)
            for i, rel_path in enumerate(all_files):
                if (i + 1) % 20 == 0:
                    pct = 50 + int((i / hash_total) * 50)
                    self.progress_updated.emit(pct, f"Pre-hashing {i+1}/{hash_total}...")

                game_file = self._game_dir / rel_path.replace("/", os.sep)
                if not game_file.exists():
                    continue

                if rel_path in self._targeted:
                    # Targeted file — was just restored, hash it fresh
                    h, _ = _hash_file(game_file)
                    pre_hashes[rel_path] = h
                elif rel_path in all_snap:
                    # Non-targeted — use snapshot hash (file wasn't touched)
                    pre_hashes[rel_path] = all_snap[rel_path]

            self.progress_updated.emit(100, "Ready!")
            self.finished.emit(pre_hashes)

        except Exception as e:
            logger.error("Script prep failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))


class ScriptCaptureWorker(QObject):
    """Background worker that captures game file changes after a script ran.

    Uses entry-level capture for PAZ files: parses PAMT to identify which
    game file entries changed, extracts+decompresses each, and stores the
    decompressed content as ENTR deltas. This allows different entries in
    the same PAZ to compose correctly when multiple mods are applied.
    """

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
            from cdumm.engine.snapshot_manager import hash_file as _hash_file
            from cdumm.engine.import_handler import ModImportResult

            db = Database(self._db_path)
            db.initialize()

            total_files = len(self._pre_hashes)
            self.progress_updated.emit(0, f"Scanning {total_files} game files...")

            # Find which files changed
            changed: list[str] = []
            for file_idx, (rel_path, old_hash) in enumerate(self._pre_hashes.items()):
                if file_idx % 5 == 0 or file_idx == total_files - 1:
                    pct = int((file_idx / max(total_files, 1)) * 18)
                    self.progress_updated.emit(
                        pct, f"Scanning file {file_idx + 1}/{total_files}...")

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
            self.progress_updated.emit(20, f"Found {len(changed)} changed file(s)...")

            vanilla_dir = self._deltas_dir.parent / "vanilla"
            priority_cursor = db.connection.execute(
                "SELECT COALESCE(MAX(priority), 0) + 1 FROM mods")
            next_priority = priority_cursor.fetchone()[0]
            cursor = db.connection.execute(
                "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
                (self._mod_name, "paz", next_priority),
            )
            mod_id = cursor.lastrowid
            result = ModImportResult(self._mod_name)

            # Separate PAZ files from non-PAZ files
            paz_files = [f for f in changed if f.endswith(".paz")]
            pamt_files = [f for f in changed if f.endswith(".pamt")]
            papgt_files = [f for f in changed if f.endswith(".papgt")]
            # Skip PAMT (derived from PAZ entry changes) and PAPGT (rebuilt)
            skipped = set(pamt_files) | set(papgt_files)
            other_files = [f for f in changed if f not in skipped and f not in paz_files]

            # ── Entry-level capture for PAZ files ──────────────────────
            if paz_files:
                self.progress_updated.emit(25, "Analyzing PAZ entries...")
                entry_count = self._capture_paz_entries(
                    paz_files, vanilla_dir, mod_id, db, result)
                logger.info("Captured %d entry-level deltas from %d PAZ files",
                            entry_count, len(paz_files))

            # ── Byte-level capture for non-PAZ files (rare/shouldn't happen) ──
            if other_files:
                from cdumm.engine.delta_engine import (
                    generate_delta, get_changed_byte_ranges, save_delta,
                )
                from cdumm.engine.apply_engine import _save_range_backup
                import hashlib

                for idx, rel_path in enumerate(other_files):
                    pct = 70 + int((idx + 1) / len(other_files) * 20)
                    self.progress_updated.emit(pct, f"Delta: {rel_path}...")

                    vanilla_path = vanilla_dir / rel_path.replace("/", "\\")
                    if not vanilla_path.exists():
                        continue
                    vanilla_bytes = vanilla_path.read_bytes()
                    modified_bytes = (self._game_dir / rel_path.replace("/", "\\")).read_bytes()

                    delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
                    byte_ranges = ([(0, len(modified_bytes))]
                                   if delta_bytes[:4] == b"FULL"
                                   else get_changed_byte_ranges(vanilla_bytes, modified_bytes))

                    safe_name = rel_path.replace("/", "_") + ".bsdiff"
                    delta_path = self._deltas_dir / str(mod_id) / safe_name
                    save_delta(delta_bytes, delta_path)
                    _save_range_backup(self._game_dir, vanilla_dir, rel_path, byte_ranges)

                    for bs, be in byte_ranges:
                        vh = hashlib.sha256(vanilla_bytes[bs:be]).hexdigest()[:16]
                        db.connection.execute(
                            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                            "byte_start, byte_end, vanilla_hash) VALUES (?, ?, ?, ?, ?, ?)",
                            (mod_id, rel_path, str(delta_path), bs, be, vh),
                        )
                    db.connection.execute(
                        "INSERT OR IGNORE INTO mod_vanilla_sizes "
                        "(mod_id, file_path, vanilla_size) VALUES (?, ?, ?)",
                        (mod_id, rel_path, len(vanilla_bytes)),
                    )
                    result.changed_files.append({"file_path": rel_path})

            db.connection.commit()
            db.close()

            self.progress_updated.emit(100, "Done!")
            self.finished.emit(result)

        except Exception as e:
            logger.error("Script capture failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))

    def _capture_paz_entries(
        self,
        paz_files: list[str],
        vanilla_dir: Path,
        mod_id: int,
        db: "Database",
        result,
    ) -> int:
        """Compare PAZ entries between vanilla and modified, store ENTR deltas.

        For each changed PAZ, parses both vanilla and modified PAMT, then
        extracts+decompresses each entry to find which game files changed.
        Stores decompressed content as ENTR deltas.

        Returns count of entry deltas created.
        """
        from cdumm.archive.paz_parse import parse_pamt, PazEntry
        from cdumm.engine.delta_engine import save_entry_delta

        count = 0

        # Group PAZ files by directory
        by_dir: dict[str, list[str]] = {}
        for paz_rel in paz_files:
            pamt_dir = paz_rel.split("/")[0]
            by_dir.setdefault(pamt_dir, []).append(paz_rel)

        total_dirs = len(by_dir)
        for dir_idx, (pamt_dir, paz_rels) in enumerate(sorted(by_dir.items())):
            pct = 25 + int((dir_idx / total_dirs) * 45)
            self.progress_updated.emit(pct, f"Analyzing entries in {pamt_dir}/...")

            # Parse vanilla PAMT
            vanilla_pamt = vanilla_dir / pamt_dir / "0.pamt"
            modified_pamt = self._game_dir / pamt_dir / "0.pamt"

            if not vanilla_pamt.exists() or not modified_pamt.exists():
                logger.warning("PAMT not found for %s, skipping entry capture", pamt_dir)
                continue

            # vanilla_pamt in CDMods/vanilla should already be the pristine copy
            # (backed up during snapshot or first import). Don't overwrite it.

            try:
                vanilla_entries = parse_pamt(
                    str(vanilla_pamt), str(vanilla_dir / pamt_dir))
                modified_entries = parse_pamt(
                    str(modified_pamt), str(self._game_dir / pamt_dir))
            except Exception as e:
                logger.warning("Failed to parse PAMT for %s: %s", pamt_dir, e)
                continue

            # Build lookup: path -> entry (for vanilla)
            vanilla_by_path: dict[str, PazEntry] = {}
            for e in vanilla_entries:
                vanilla_by_path[e.path.lower()] = e

            modified_by_path: dict[str, PazEntry] = {}
            for e in modified_entries:
                modified_by_path[e.path.lower()] = e

            # Which PAZ indices changed?
            changed_paz_indices = set()
            for paz_rel in paz_rels:
                paz_name = paz_rel.split("/")[1]  # e.g. "4.paz"
                paz_idx = int(paz_name.replace(".paz", ""))
                changed_paz_indices.add(paz_idx)

            # Filter to entries in changed PAZ files only
            target_entries = [
                (p, e) for p, e in modified_by_path.items()
                if e.paz_index in changed_paz_indices
            ]
            total_entries = len(target_entries)

            # Compare entries in changed PAZ files
            for entry_idx, (path_lower, mod_entry) in enumerate(target_entries):
                # Per-entry progress within this directory
                if total_entries > 0 and (entry_idx % 20 == 0 or entry_idx == total_entries - 1):
                    dir_pct = entry_idx / total_entries
                    pct = 25 + int(((dir_idx + dir_pct) / total_dirs) * 45)
                    self.progress_updated.emit(
                        pct, f"Comparing {pamt_dir}/ entry {entry_idx + 1}/{total_entries}...")

                van_entry = vanilla_by_path.get(path_lower)

                # Extract decompressed content from both
                try:
                    mod_content = self._extract_entry(mod_entry)
                except Exception as e:
                    logger.debug("Failed to extract modified %s: %s",
                                 mod_entry.path, e)
                    continue

                if van_entry is None:
                    # New entry added by script
                    van_content = b""
                elif van_entry.paz_index != mod_entry.paz_index:
                    # Entry moved between PAZ files — treat as new
                    van_content = b""
                else:
                    try:
                        # Extract from vanilla PAZ (which is in vanilla_dir)
                        van_paz_path = str(vanilla_dir / pamt_dir
                                           / f"{van_entry.paz_index}.paz")
                        van_entry_copy = PazEntry(
                            path=van_entry.path,
                            paz_file=van_paz_path,
                            offset=van_entry.offset,
                            comp_size=van_entry.comp_size,
                            orig_size=van_entry.orig_size,
                            flags=van_entry.flags,
                            paz_index=van_entry.paz_index,
                        )
                        van_content = self._extract_entry(van_entry_copy)
                    except Exception as e:
                        logger.debug("Failed to extract vanilla %s: %s",
                                     van_entry.path, e)
                        continue

                if mod_content == van_content:
                    continue  # Entry unchanged

                # Store ENTR delta
                # Use vanilla entry metadata (for apply to know original slot)
                ref_entry = van_entry if van_entry else mod_entry
                metadata = {
                    "pamt_dir": pamt_dir,
                    "entry_path": mod_entry.path,
                    "paz_index": mod_entry.paz_index,
                    "compression_type": ref_entry.compression_type,
                    "flags": ref_entry.flags,
                    "vanilla_offset": ref_entry.offset if van_entry else 0,
                    "vanilla_comp_size": ref_entry.comp_size if van_entry else 0,
                    "vanilla_orig_size": ref_entry.orig_size if van_entry else 0,
                }

                safe_name = mod_entry.path.replace("/", "_") + ".entr"
                delta_path = self._deltas_dir / str(mod_id) / safe_name
                save_entry_delta(mod_content, metadata, delta_path)

                # DB entry: file_path = PAZ file, entry_path = game file
                paz_file_path = f"{pamt_dir}/{mod_entry.paz_index}.paz"
                db.connection.execute(
                    "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                    "byte_start, byte_end, entry_path) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mod_id, paz_file_path, str(delta_path),
                     ref_entry.offset if van_entry else None,
                     (ref_entry.offset + ref_entry.comp_size) if van_entry else None,
                     mod_entry.path),
                )

                db.connection.execute(
                    "INSERT OR IGNORE INTO mod_vanilla_sizes "
                    "(mod_id, file_path, vanilla_size) VALUES (?, ?, ?)",
                    (mod_id, paz_file_path,
                     ref_entry.comp_size if van_entry else 0),
                )

                result.changed_files.append({
                    "file_path": paz_file_path,
                    "entry_path": mod_entry.path,
                })

                count += 1
                logger.info("Entry delta: %s in %s/%d.paz (vanilla=%d, mod=%d bytes)",
                            mod_entry.path, pamt_dir, mod_entry.paz_index,
                            len(van_content), len(mod_content))

        return count

    @staticmethod
    def _extract_entry(entry) -> bytes:
        """Extract and decompress a single PAMT entry from its PAZ file."""
        import os
        from cdumm.archive.paz_crypto import decrypt, lz4_decompress

        with open(entry.paz_file, "rb") as f:
            f.seek(entry.offset)
            raw = f.read(entry.comp_size)

        basename = os.path.basename(entry.path)

        if entry.compressed and entry.compression_type == 2:
            try:
                return lz4_decompress(raw, entry.orig_size)
            except Exception:
                decrypted = decrypt(raw, basename)
                return lz4_decompress(decrypted, entry.orig_size)

        if entry.encrypted:
            raw = decrypt(raw, basename)

        return raw


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
        """Scan game files vs snapshot and capture changes at entry level.

        Uses the same entry-level capture as ScriptCaptureWorker: PAZ files
        are compared at the PAMT entry level (decompressed content), so
        different entries compose correctly across mods.
        """
        try:
            from cdumm.engine.snapshot_manager import hash_file as _hash_file
            from cdumm.engine.import_handler import ModImportResult

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
            self.progress_updated.emit(55, f"Found {len(changed)} changed file(s)...")

            vanilla_dir = self._deltas_dir.parent / "vanilla"
            priority_cursor = db.connection.execute(
                "SELECT COALESCE(MAX(priority), 0) + 1 FROM mods")
            next_priority = priority_cursor.fetchone()[0]
            cursor = db.connection.execute(
                "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
                (self._mod_name, "paz", next_priority),
            )
            mod_id = cursor.lastrowid
            result = ModImportResult(self._mod_name)

            # Separate PAZ from other files
            paz_files = [f for f in changed if f.endswith(".paz")]
            skipped = {f for f in changed if f.endswith((".pamt", ".papgt"))}
            other_files = [f for f in changed if f not in skipped and f not in paz_files]

            # Entry-level capture for PAZ files (reuse ScriptCaptureWorker logic)
            if paz_files:
                self.progress_updated.emit(60, "Analyzing PAZ entries...")
                # Create a temporary ScriptCaptureWorker to use its methods
                cap = ScriptCaptureWorker.__new__(ScriptCaptureWorker)
                cap._game_dir = self._game_dir
                cap._deltas_dir = self._deltas_dir
                entry_count = cap._capture_paz_entries(
                    paz_files, vanilla_dir, mod_id, db, result)
                logger.info("Captured %d entry-level deltas", entry_count)

            # Byte-level capture for non-PAZ files (rare)
            if other_files:
                from cdumm.engine.delta_engine import (
                    generate_delta, get_changed_byte_ranges, save_delta,
                )
                from cdumm.engine.apply_engine import _save_range_backup
                import hashlib

                for idx, rel_path in enumerate(other_files):
                    pct = 80 + int((idx + 1) / len(other_files) * 15)
                    self.progress_updated.emit(pct, f"Delta: {rel_path}...")

                    vanilla_path = vanilla_dir / rel_path.replace("/", "\\")
                    if not vanilla_path.exists():
                        continue
                    vanilla_bytes = vanilla_path.read_bytes()
                    modified_bytes = (self._game_dir / rel_path.replace("/", "\\")).read_bytes()

                    delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
                    byte_ranges = ([(0, len(modified_bytes))]
                                   if delta_bytes[:4] == b"FULL"
                                   else get_changed_byte_ranges(vanilla_bytes, modified_bytes))

                    safe_name = rel_path.replace("/", "_") + ".bsdiff"
                    delta_path = self._deltas_dir / str(mod_id) / safe_name
                    save_delta(delta_bytes, delta_path)
                    _save_range_backup(self._game_dir, vanilla_dir, rel_path, byte_ranges)

                    for bs, be in byte_ranges:
                        vh = hashlib.sha256(
                            vanilla_bytes[bs:be] if bs < len(vanilla_bytes) else b""
                        ).hexdigest()[:16]
                        db.connection.execute(
                            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                            "byte_start, byte_end, vanilla_hash) VALUES (?, ?, ?, ?, ?, ?)",
                            (mod_id, rel_path, str(delta_path), bs, be, vh),
                        )
                    db.connection.execute(
                        "INSERT OR IGNORE INTO mod_vanilla_sizes "
                        "(mod_id, file_path, vanilla_size) VALUES (?, ?, ?)",
                        (mod_id, rel_path, len(vanilla_bytes)),
                    )
                    result.changed_files.append({"file_path": rel_path})

            db.connection.commit()
            db.close()

            self.progress_updated.emit(100, "Done!")
            self.finished.emit(result)

        except Exception as e:
            logger.error("Scan failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))


class BackupVerifyWorker(QObject):
    """Background worker that verifies vanilla backups against snapshot hashes."""

    progress_updated = Signal(int, str)
    finished = Signal(object)  # int: count of purged files
    error_occurred = Signal(str)

    def __init__(self, vanilla_dir: Path, db_path: Path) -> None:
        super().__init__()
        self._vanilla_dir = vanilla_dir
        self._db_path = db_path

    def run(self) -> None:
        try:
            import os
            from cdumm.engine.snapshot_manager import hash_file

            db = Database(self._db_path)
            db.initialize()

            # Collect all backup files
            backup_files = []
            for dirpath, _, filenames in os.walk(self._vanilla_dir):
                for fname in filenames:
                    if fname.endswith(".vranges"):
                        continue
                    backup_files.append(Path(dirpath) / fname)

            total = len(backup_files)
            if total == 0:
                self.finished.emit(0)
                db.close()
                return

            purged = 0
            for i, full in enumerate(backup_files):
                pct = int((i / total) * 100)
                rel = str(full.relative_to(self._vanilla_dir)).replace("\\", "/")
                self.progress_updated.emit(pct, f"Verifying {rel}...")

                snap = db.connection.execute(
                    "SELECT file_hash FROM snapshots WHERE file_path = ?", (rel,)
                ).fetchone()
                if snap is None:
                    continue
                try:
                    backup_hash, _ = hash_file(full)
                    if backup_hash != snap[0]:
                        full.unlink()
                        purged += 1
                        logger.warning("Purged corrupted backup: %s", rel)
                except Exception as e:
                    logger.warning("Could not verify backup %s: %s", rel, e)

            if purged:
                logger.info("Purged %d corrupted vanilla backup(s)", purged)

            db.close()
            self.progress_updated.emit(100, "Done!")
            self.finished.emit(purged)

        except Exception as e:
            logger.error("Backup verify failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))


class ModCheckWorker(QObject):
    """Background worker that checks enabled mods for issues."""

    progress_updated = Signal(int, str)
    finished = Signal(list)  # list of (source, detail) tuples
    error_occurred = Signal(str)

    def __init__(self, game_dir: Path, db_path: Path):
        super().__init__()
        self._game_dir = game_dir
        self._db_path = db_path

    def run(self):
        try:
            import os, hashlib
            from cdumm.archive.paz_parse import parse_pamt

            db = Database(self._db_path)
            db.initialize()
            issues = []

            # All checks work against VANILLA files + stored mod data.
            # No need to apply first — checks if mods WILL work, not if they DID work.

            # 1. Check if vanilla file sizes changed since import
            self.progress_updated.emit(10, "Checking vanilla file sizes...")
            try:
                size_rows = db.connection.execute(
                    "SELECT m.name, vs.file_path, vs.vanilla_size "
                    "FROM mod_vanilla_sizes vs JOIN mods m ON vs.mod_id = m.id "
                    "WHERE m.enabled = 1"
                ).fetchall()
                for mod_name, fp, expected_size in size_rows:
                    vanilla_path = self._game_dir / "CDMods" / "vanilla" / fp.replace("/", os.sep)
                    game_path = self._game_dir / fp.replace("/", os.sep)
                    src = vanilla_path if vanilla_path.exists() else game_path
                    if src.exists():
                        actual_size = src.stat().st_size
                        if actual_size != expected_size:
                            issues.append((mod_name,
                                f"{fp} size changed ({expected_size} -> {actual_size}) — "
                                f"game updated, mod needs re-importing"))
            except Exception:
                pass

            # (Byte-level vanilla hash check removed — too many false positives
            # from PAMT hash field changes and Steam verify refreshes.
            # File size check above is the reliable game update indicator.)

            # 3. Check mods have valid delta files
            self.progress_updated.emit(90, "Checking delta files...")
            delta_rows = db.connection.execute(
                "SELECT m.name, md.delta_path, md.file_path "
                "FROM mod_deltas md JOIN mods m ON md.mod_id = m.id "
                "WHERE m.enabled = 1"
            ).fetchall()
            checked_paths = set()
            for mod_name, dp, fp in delta_rows:
                if dp in checked_paths:
                    continue
                checked_paths.add(dp)
                if not Path(dp).exists():
                    issues.append((mod_name, f"Missing delta file for {fp}"))

            db.close()
            self.progress_updated.emit(100, "Done")
            self.finished.emit(issues)

        except Exception as e:
            logger.error("Mod check failed: %s", e, exc_info=True)
            self.error_occurred.emit(str(e))
