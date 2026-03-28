"""Main application window — wires all components together."""
import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from cdumm.engine.apply_engine import ApplyWorker, RevertWorker
from cdumm.engine.conflict_detector import ConflictDetector
from cdumm.engine.mod_manager import ModManager
from cdumm.engine.snapshot_manager import SnapshotManager, SnapshotWorker
from cdumm.gui.asi_panel import AsiPanel
from cdumm.gui.conflict_view import ConflictView
from cdumm.gui.import_widget import ImportWidget
from cdumm.gui.mod_list_model import ModListModel
from cdumm.gui.progress_dialog import ProgressDialog
from cdumm.gui.workers import ImportWorker
from cdumm.storage.config import Config
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


def _is_standalone_paz_mod(path: Path) -> bool:
    """Check if path is a standalone PAZ mod (0.paz + 0.pamt, not in a numbered dir).

    These mods add a new PAZ directory and don't need a vanilla snapshot.
    """
    import zipfile
    if path.is_dir():
        # Check folder: has 0.paz + 0.pamt at root or one level deep
        if (path / "0.paz").exists() and (path / "0.pamt").exists():
            return True
        for sub in path.iterdir():
            if sub.is_dir() and (sub / "0.paz").exists() and (sub / "0.pamt").exists():
                # But NOT if it's a numbered directory (those are regular mods)
                if not (sub.name.isdigit() and len(sub.name) == 4):
                    return True
        return False
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                has_paz = any(n.endswith("/0.paz") or n == "0.paz" for n in names)
                has_pamt = any(n.endswith("/0.pamt") or n == "0.pamt" for n in names)
                return has_paz and has_pamt
        except Exception:
            return False
    return False


class MainThreadDispatcher(QObject):
    """Routes callbacks from worker threads to the main thread.

    PySide6 lambdas connected to signals execute on the emitter's thread,
    ignoring QueuedConnection. This QObject lives on the main thread with
    @Slot methods, so Qt's auto-connection correctly queues cross-thread calls.
    """
    _dispatch = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dispatch.connect(self._execute)

    @Slot(object, object)
    def _execute(self, func, args):
        func(*args)

    def call(self, func, *args):
        """Emit from any thread — func will execute on the main thread."""
        self._dispatch.emit(func, args)


class MainWindow(QMainWindow):
    def __init__(self, db: Database | None = None, game_dir: Path | None = None,
                 app_data_dir: Path | None = None) -> None:
        super().__init__()
        from cdumm import __version__
        self.setWindowTitle(f"Crimson Desert Ultimate Mods Manager v{__version__}")
        self.setMinimumSize(1000, 700)

        # Set window icon
        import sys
        from PySide6.QtGui import QIcon
        if getattr(sys, 'frozen', False):
            icon_path = Path(sys._MEIPASS) / "cdumm.ico"
        else:
            icon_path = Path(__file__).resolve().parents[3] / "cdumm.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._db = db
        self._game_dir = game_dir
        self._app_data_dir = app_data_dir or Path.home() / "AppData" / "Local" / "cdumm"
        # Store vanilla backups and deltas on the game drive (CDMods folder)
        # so we can use hard links instead of copies for multi-GB PAZ files.
        self._cdmods_dir = game_dir / "CDMods" if game_dir else self._app_data_dir
        self._cdmods_dir.mkdir(parents=True, exist_ok=True)
        self._deltas_dir = self._cdmods_dir / "deltas"
        self._vanilla_dir = self._cdmods_dir / "vanilla"
        self._migrate_from_appdata()
        self._worker_thread: QThread | None = None
        self._active_progress: ProgressDialog | None = None
        self._needs_apply = False
        self._dispatcher = MainThreadDispatcher(parent=self)

        # Initialize managers if database is available
        if db:
            self._snapshot = SnapshotManager(db)
            self._mod_manager = ModManager(db, self._deltas_dir)
            self._conflict_detector = ConflictDetector(db)
            self._mod_manager.cleanup_orphaned_deltas()
        else:
            self._snapshot = None
            self._mod_manager = None
            self._conflict_detector = None

        self._build_ui()
        self._build_toolbar()
        self._build_status_bar()
        self._refresh_all(update_statuses=False)
        self.setAcceptDrops(True)

        # Crash detection — lock file
        self._lock_file = self._app_data_dir / ".running"
        crashed_last_time = self._lock_file.exists()
        self._lock_file.write_text(str(datetime.now()), encoding="utf-8")

        # Deferred startup tasks (after window is visible)
        QTimer.singleShot(500, self._deferred_startup)

        # Update check (delayed further to not compete with UI loading)
        QTimer.singleShot(5000, self._check_for_updates)

        # Auto-snapshot on first run (after window is shown)
        if self._snapshot and not self._snapshot.has_snapshot() and self._game_dir:
            QTimer.singleShot(500, self._auto_snapshot_first_run)

        # If previous session didn't close cleanly, offer bug report
        if crashed_last_time:
            QTimer.singleShot(1000, self._offer_crash_report)

    def _migrate_from_appdata(self) -> None:
        """One-time migration: move vanilla/deltas from old AppData locations to CDMods on game drive."""
        import shutil
        # Check both old (cdmm) and current (cdumm) AppData paths
        old_appdata = Path.home() / "AppData" / "Local" / "cdmm"
        migrated_deltas_from: list[str] = []

        for appdata in [old_appdata, self._app_data_dir]:
            for sub in ("vanilla", "deltas"):
                old_dir = appdata / sub
                new_dir = self._vanilla_dir if sub == "vanilla" else self._deltas_dir
                if old_dir.exists() and not new_dir.exists() and old_dir != new_dir:
                    try:
                        shutil.move(str(old_dir), str(new_dir))
                        logger.info("Migrated %s -> %s", old_dir, new_dir)
                        if sub == "deltas":
                            migrated_deltas_from.append(str(old_dir))
                    except Exception as e:
                        logger.warning("Migration failed for %s: %s (will copy instead)", old_dir, e)
                        try:
                            shutil.copytree(str(old_dir), str(new_dir))
                            shutil.rmtree(old_dir, ignore_errors=True)
                            if sub == "deltas":
                                migrated_deltas_from.append(str(old_dir))
                        except Exception as e2:
                            logger.error("Copy fallback also failed: %s", e2)

        # Update delta_path references in the database to point to the new location
        if migrated_deltas_from:
            for old_path in migrated_deltas_from:
                new_path = str(self._deltas_dir)
                try:
                    count = self._db.connection.execute(
                        "UPDATE mod_deltas SET delta_path = REPLACE(delta_path, ?, ?)",
                        (old_path, new_path),
                    ).rowcount
                    self._db.connection.commit()
                    logger.info("Updated %d delta paths: %s -> %s", count, old_path, new_path)
                except Exception as e:
                    logger.error("Failed to update delta paths in DB: %s", e)

    def _deferred_startup(self) -> None:
        """Run after window is visible — background status check and backup validation."""
        if hasattr(self, "_mod_list_model"):
            self._mod_list_model.refresh_statuses()
        if self._snapshot and self._snapshot.has_snapshot() and self._game_dir:
            self._purge_corrupted_backups()
            self._check_game_version_mismatches()

    def _check_game_version_mismatches(self) -> None:
        """Warn about mods imported for a different game version."""
        try:
            from cdumm.engine.version_detector import detect_game_version
            current = detect_game_version(self._game_dir)
            if not current:
                return
            cursor = self._db.connection.execute(
                "SELECT name, game_version_hash FROM mods WHERE game_version_hash IS NOT NULL AND enabled = 1")
            mismatched = [name for name, ver in cursor.fetchall() if ver and ver != current]
            if mismatched:
                self.statusBar().showMessage(
                    f"Warning: {len(mismatched)} mod(s) imported for a different game version: "
                    + ", ".join(mismatched[:3])
                    + ("..." if len(mismatched) > 3 else ""), 15000)
        except Exception as e:
            logger.debug("Version mismatch check failed: %s", e)

    def _purge_corrupted_backups(self) -> None:
        """One-time check: run background worker to verify and purge corrupted backups."""
        config = Config(self._db)
        if config.get("backups_verified") == "1":
            return
        if not self._vanilla_dir.exists():
            config.set("backups_verified", "1")
            return

        from cdumm.gui.workers import BackupVerifyWorker
        progress = ProgressDialog("Verifying vanilla backups...", self)
        worker = BackupVerifyWorker(self._vanilla_dir, self._db.db_path)
        thread = QThread()
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_backup_verify_done)

    def _on_backup_verify_done(self, purged_count: int) -> None:
        self._sync_db()
        config = Config(self._db)
        config.set("backups_verified", "1")
        if purged_count and purged_count > 0:
            self.statusBar().showMessage(
                f"Purged {purged_count} corrupted vanilla backup(s)", 10000)
        config.set("backups_verified", "1")

    def _auto_snapshot_first_run(self) -> None:
        reply = QMessageBox.question(
            self, "First Run — Create Snapshot",
            "No vanilla snapshot exists yet. A snapshot is required before you can import mods.\n\n"
            "This will scan all game files and may take a few minutes.\n\n"
            "Create snapshot now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._on_refresh_snapshot()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Import widget at top
        self._import_widget = ImportWidget()
        self._import_widget.file_dropped.connect(self._on_import_dropped)
        layout.addWidget(self._import_widget)

        # Main content: mod list + conflict view
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Tab widget for mods and ASI
        self._tabs = QTabWidget()
        tabs = self._tabs

        # PAZ Mods tab
        mods_widget = QWidget()
        mods_layout = QVBoxLayout(mods_widget)
        if self._mod_manager and self._conflict_detector:
            self._mod_list_model = ModListModel(
                self._mod_manager, self._conflict_detector,
                game_dir=self._game_dir, db_path=self._db.db_path,
                deltas_dir=self._deltas_dir)
            self._mod_list_model.mod_toggled.connect(self._on_mod_toggled_via_checkbox)
            self._mod_table = QTableView()
            self._mod_table.setModel(self._mod_list_model)
            self._mod_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            self._mod_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._mod_table.customContextMenuRequested.connect(self._show_mod_context_menu)
            self._mod_table.setDragEnabled(True)
            self._mod_table.setAcceptDrops(True)
            self._mod_table.setDropIndicatorShown(True)
            self._mod_table.setDragDropMode(QTableView.DragDropMode.InternalMove)
            self._mod_table.setDefaultDropAction(Qt.DropAction.MoveAction)
            # Column sizing: stretch Name, fit others to content
            from PySide6.QtWidgets import QHeaderView
            header = self._mod_table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            from cdumm.gui.mod_list_model import COL_NAME
            header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
            mods_layout.addWidget(self._mod_table)

            btn_row = QHBoxLayout()
            remove_btn = QPushButton("Remove Selected")
            remove_btn.clicked.connect(self._on_remove_mod)
            btn_row.addWidget(remove_btn)
            details_btn = QPushButton("View Details")
            details_btn.clicked.connect(self._on_view_details)
            btn_row.addWidget(details_btn)
            btn_row.addStretch()
            up_btn = QPushButton("Move Up")
            up_btn.setToolTip("Higher priority — wins conflicts")
            up_btn.clicked.connect(self._on_move_up)
            btn_row.addWidget(up_btn)
            down_btn = QPushButton("Move Down")
            down_btn.setToolTip("Lower priority — loses conflicts")
            down_btn.clicked.connect(self._on_move_down)
            btn_row.addWidget(down_btn)
            mods_layout.addLayout(btn_row)
        else:
            mods_layout.addWidget(QLabel("No database connected"))

        tabs.addTab(mods_widget, "PAZ Mods")

        # ASI tab
        if self._game_dir:
            self._asi_panel = AsiPanel(self._game_dir / "bin64")
            tabs.addTab(self._asi_panel, "ASI Plugins")

        splitter.addWidget(tabs)

        # Conflict view
        self._conflict_view = ConflictView()
        self._conflict_view.winner_changed.connect(self._on_set_winner)
        splitter.addWidget(self._conflict_view)

        splitter.setSizes([400, 200])
        layout.addWidget(splitter)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        apply_btn = QPushButton("Apply")
        apply_btn.setStyleSheet("font-weight: bold; color: #4CAF50;")
        apply_btn.clicked.connect(self._on_apply)
        toolbar.addWidget(apply_btn)

        revert_btn = QPushButton("Revert to Vanilla")
        revert_btn.clicked.connect(self._on_revert)
        toolbar.addWidget(revert_btn)

        toolbar.addSeparator()

        test_btn = QPushButton("Test Mod...")
        test_btn.clicked.connect(self._on_test_mod)
        toolbar.addWidget(test_btn)

        toolbar.addSeparator()

        snapshot_btn = QPushButton("Refresh Snapshot")
        snapshot_btn.clicked.connect(self._on_refresh_snapshot)
        toolbar.addWidget(snapshot_btn)

        gamedir_btn = QPushButton("Game Directory...")
        gamedir_btn.clicked.connect(self._on_change_game_dir)
        toolbar.addWidget(gamedir_btn)

        toolbar.addSeparator()

        profiles_btn = QPushButton("Profiles...")
        profiles_btn.clicked.connect(self._on_profiles)
        toolbar.addWidget(profiles_btn)

        export_btn = QPushButton("Export List")
        export_btn.clicked.connect(self._on_export_list)
        toolbar.addWidget(export_btn)

        import_list_btn = QPushButton("Import List")
        import_list_btn.clicked.connect(self._on_import_list)
        toolbar.addWidget(import_list_btn)

        toolbar.addSeparator()

        bug_btn = QPushButton("Report Bug")
        bug_btn.setStyleSheet("color: #FF9800;")
        bug_btn.clicked.connect(self._on_report_bug)
        toolbar.addWidget(bug_btn)

    def _build_status_bar(self) -> None:
        from cdumm import __version__
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        version_label = QLabel(f"v{__version__}")
        version_label.setStyleSheet("color: gray; padding-right: 10px;")
        status_bar.addPermanentWidget(version_label)
        self._snapshot_label = QLabel()
        status_bar.addPermanentWidget(self._snapshot_label)
        self._update_snapshot_status()

    def _update_snapshot_status(self) -> None:
        if not self._snapshot:
            self._snapshot_label.setText("Snapshot: No database")
            self._snapshot_label.setStyleSheet("color: gray;")
        elif self._snapshot.has_snapshot():
            count = self._snapshot.get_snapshot_count()
            self._snapshot_label.setText(f"Snapshot: Valid ({count} files)")
            self._snapshot_label.setStyleSheet("color: green;")
            # Check for game updates in background (deferred)
            QTimer.singleShot(3000, self._check_snapshot_outdated_async)
        else:
            self._snapshot_label.setText("Snapshot: Missing — click Refresh Snapshot")
            self._snapshot_label.setStyleSheet("color: red; font-weight: bold;")

    def _check_snapshot_outdated_async(self) -> None:
        """Check if game was updated, then update the label."""
        if self._check_snapshot_outdated():
            count = self._snapshot.get_snapshot_count() if self._snapshot else 0
            self._snapshot_label.setText(
                f"Snapshot: OUTDATED — game was updated, click Refresh Snapshot ({count} files)")
            self._snapshot_label.setStyleSheet("color: #FF9800; font-weight: bold;")

    def _check_snapshot_outdated(self) -> bool:
        """Check if the game was updated since the snapshot was taken.

        Compares a few files that no mod touches against the snapshot.
        If any differ, the game was updated and the snapshot is stale.
        """
        if not self._snapshot or not self._game_dir or not self._db:
            return False
        import os
        from cdumm.engine.snapshot_manager import hash_file

        # Get files that no mod touches
        modded = self._db.connection.execute(
            "SELECT DISTINCT file_path FROM mod_deltas").fetchall()
        modded_set = {row[0] for row in modded}

        # Check up to 3 unmodded files
        cursor = self._db.connection.execute("SELECT file_path, file_hash FROM snapshots")
        checked = 0
        for file_path, snap_hash in cursor.fetchall():
            if file_path in modded_set:
                continue
            game_file = self._game_dir / file_path.replace("/", os.sep)
            if not game_file.exists():
                return True  # file was removed = game changed
            try:
                current_hash, _ = hash_file(game_file)
                if current_hash != snap_hash:
                    return True
            except Exception:
                continue
            checked += 1
            if checked >= 3:
                break
        return False

    def _sync_db(self) -> None:
        """Sync main DB after a worker writes via WAL checkpoint."""
        if not self._db:
            return
        try:
            self._db.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as e:
            logger.error("WAL checkpoint failed: %s", e)

    def _refresh_all(self, update_statuses: bool = True) -> None:
        logger.debug("_refresh_all: start")
        if hasattr(self, "_mod_list_model"):
            logger.debug("_refresh_all: refreshing mod list model")
            self._mod_list_model.refresh()
            if update_statuses:
                self._mod_list_model.refresh_statuses()
        if self._conflict_detector:
            logger.debug("_refresh_all: detecting conflicts")
            conflicts = self._conflict_detector.detect_all()
            logger.debug("_refresh_all: updating conflict view (%d conflicts)", len(conflicts))
            self._conflict_view.update_conflicts(conflicts)
        logger.debug("_refresh_all: updating snapshot status")
        self._update_snapshot_status()
        logger.debug("_refresh_all: done")

    # --- Helper to run a worker with ProgressDialog ---
    def _run_worker(self, worker, thread: QThread, progress: ProgressDialog,
                    on_finished, on_error=None) -> None:
        """Wire a worker + thread + progress dialog with proper signal connections."""
        # CRITICAL: Keep references alive — without this, Python GC destroys the
        # worker and thread before they finish, causing silent failures.
        self._active_worker = worker
        self._worker_thread = thread
        self._active_progress = progress

        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        # Use proper Slot methods on ProgressDialog — no lambdas for progress
        worker.progress_updated.connect(progress.update_progress)

        # CRITICAL: Use MainThreadDispatcher to route callbacks to the main thread.
        # PySide6 lambdas connected to signals ALWAYS execute on the emitter's
        # thread (ignoring QueuedConnection). The dispatcher is a QObject on the
        # main thread with @Slot methods, so Qt correctly queues the call.
        worker.finished.connect(
            lambda *args: self._dispatcher.call(
                self._worker_done, thread, progress, on_finished, *args)
        )
        worker.error_occurred.connect(
            lambda err: self._dispatcher.call(
                self._worker_error, thread, progress, err, on_error)
        )

        logger.info("Starting worker: %s", type(worker).__name__)
        progress.show()
        thread.start()

    def _worker_done(self, thread: QThread, progress: ProgressDialog, callback, *args) -> None:
        # This method is guaranteed to run on the main thread via MainThreadDispatcher
        logger.info("Worker finished (main thread): %s", type(self._active_worker).__name__)

        progress.hide()

        # Wait for thread to fully stop before cleaning up — prevents Qt
        # segfaults from accessing deleted C++ objects.
        thread.quit()
        thread.wait(5000)

        # Now safe to schedule deletion
        progress.deleteLater()
        thread.deleteLater()

        self._active_progress = None
        self._active_worker = None
        self._worker_thread = None

        logger.info("Calling completion callback")
        try:
            callback(*args)
        except Exception:
            logger.error("Completion callback crashed", exc_info=True)
        logger.info("Completion callback done")

    def _worker_error(self, thread: QThread, progress: ProgressDialog,
                      error: str, callback=None) -> None:
        # This method is guaranteed to run on the main thread via MainThreadDispatcher
        logger.error("Worker error (main thread): %s", error)
        progress.close()

        thread.quit()
        thread.wait(5000)
        thread.deleteLater()

        self._active_progress = None
        self._active_worker = None
        self._worker_thread = None

        QMessageBox.critical(self, "Error", error)
        if callback:
            callback(error)

    # --- Import ---
    def _on_import_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Mod",
            "", "Mod Files (*.zip *.bat *.py *.bsdiff);;All Files (*)",
        )
        if path:
            self._run_import(Path(path))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            logger.info("File dropped on main window: %s", path)
            self._run_import(path)

    def _on_import_dropped(self, path: Path) -> None:
        self._run_import(path)

    def _run_import(self, path: Path) -> None:
        if not self._db or not self._game_dir:
            self.statusBar().showMessage("Error: Database or game directory not configured.", 5000)
            return

        self.statusBar().showMessage(f"Importing {path.name}...")
        logger.info("Starting import: %s", path)

        # Check if this is an ASI mod first (fast, no thread needed)
        from cdumm.asi.asi_manager import AsiManager
        if AsiManager.contains_asi(path):
            self._install_asi_mod(path)
            return

        # Standalone PAZ mods (modinfo.json + 0.paz + 0.pamt) don't need a snapshot
        # since they add new directories rather than modifying existing files.
        # All other PAZ mods require a snapshot for delta generation.
        if not _is_standalone_paz_mod(path) and (not self._snapshot or not self._snapshot.has_snapshot()):
            self.statusBar().showMessage(
                "Snapshot required before importing PAZ mods. Click 'Refresh Snapshot' first.", 10000
            )
            return

        # Check if this is a script-based mod — needs to run on main thread
        # so the user can interact with the cmd window
        from cdumm.engine.import_handler import detect_format, import_script_live
        import zipfile as _zf
        import tempfile

        is_script_mod = False
        if path.suffix.lower() in (".bat", ".py"):
            is_script_mod = True
        elif path.suffix.lower() == ".zip":
            try:
                with _zf.ZipFile(path) as zf:
                    names = zf.namelist()
                    has_scripts = any(n.endswith((".bat", ".py")) for n in names)
                    has_game_files = any(
                        any(n.startswith(f"{i:04d}/") for i in range(33)) or n.startswith("meta/")
                        for n in names
                    )
                    if has_scripts and not has_game_files:
                        is_script_mod = True
            except _zf.BadZipFile:
                pass
        elif path.is_dir():
            scripts = list(path.glob("*.bat")) + list(path.glob("*.py"))
            game_files = any(
                (path / f"{i:04d}").exists() for i in range(33)
            ) or (path / "meta").exists()
            if scripts and not game_files:
                is_script_mod = True

        if is_script_mod:
            self._run_script_mod(path)
            return

        # Regular PAZ mod — run on background thread
        progress = ProgressDialog("Importing Mod", self)
        worker = ImportWorker(path, self._game_dir, self._db.db_path, self._deltas_dir)
        thread = QThread()

        self._run_worker(worker, thread, progress,
                         on_finished=self._on_import_finished)

    def _run_script_mod(self, path: Path) -> None:
        """Handle script-based mods — launch script, poll for completion, capture changes."""
        import tempfile
        import zipfile as _zf
        from cdumm.engine.import_handler import (
            _detect_script_targets, _ensure_vanilla_backup, import_from_game_scan,
        )
        from cdumm.engine.snapshot_manager import hash_file as _hash_file

        logger.info("Script mod detected: %s", path)

        # Extract if zip
        script_path = path
        self._script_tmp_dir = None
        if path.suffix.lower() == ".zip":
            self._script_tmp_dir = tempfile.mkdtemp()
            with _zf.ZipFile(path) as zf:
                zf.extractall(self._script_tmp_dir)
            # Search recursively — scripts may be in subdirectories
            # Prefer .bat (install scripts) over .py (could be library files)
            bat_scripts = list(Path(self._script_tmp_dir).rglob("*.bat"))
            py_scripts = [p for p in Path(self._script_tmp_dir).rglob("*.py")
                          if "lib" not in p.parent.name.lower()
                          and "__pycache__" not in str(p)]
            scripts = bat_scripts + py_scripts
            if not scripts:
                self.statusBar().showMessage("No script found in zip.", 5000)
                return
            script_path = scripts[0]
        elif path.is_dir():
            bat_scripts = list(path.rglob("*.bat"))
            py_scripts = [p for p in path.rglob("*.py")
                          if "lib" not in p.parent.name.lower()
                          and "__pycache__" not in str(p)]
            scripts = bat_scripts + py_scripts
            if not scripts:
                self.statusBar().showMessage("No script found in folder.", 5000)
                return
            script_path = scripts[0]

        # Ask the user to name the mod
        from PySide6.QtWidgets import QInputDialog
        # Use parent folder name for generic script names like install.bat
        default_name = script_path.stem
        if default_name.lower() in ("install", "setup", "patch", "run", "apply", "mod"):
            default_name = script_path.parent.name
        name, ok = QInputDialog.getText(
            self, "Script Mod Name",
            "Enter a name for this mod:",
            text=default_name,
        )
        if not ok or not name.strip():
            return
        self._script_mod_name = name.strip()

        # Phase 1: Restore game files to vanilla so the .bat runs against
        # clean files. This ensures captured deltas are always relative to
        # vanilla, regardless of what mods were previously applied.
        vanilla_dir = self._deltas_dir.parent / "vanilla"
        vanilla_dir.mkdir(parents=True, exist_ok=True)

        # Detect targets from the main script AND any sibling .py files
        targeted = _detect_script_targets(script_path, self._game_dir)
        if not targeted:
            for sibling in script_path.parent.rglob("*.py"):
                if "__pycache__" not in str(sibling):
                    targeted.extend(_detect_script_targets(sibling, self._game_dir))
            targeted = list(dict.fromkeys(targeted))  # dedupe preserving order

        logger.info("Script targets: %s", targeted)

        # Phase 1: Backup, restore, and pre-hash on a background thread
        # to avoid freezing the UI for large directories
        self._pending_script_path = script_path
        self._pending_targeted = targeted

        from cdumm.gui.workers import ScriptPrepWorker
        progress = ProgressDialog("Preparing for script mod...", self)
        worker = ScriptPrepWorker(
            targeted, self._game_dir, vanilla_dir)
        thread = QThread()
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_script_prep_finished)

    def _on_script_prep_finished(self, pre_hashes) -> None:
        """Backup/restore/pre-hash complete — now launch the script."""
        if pre_hashes is None:
            self._script_pre_hashes = None
            logger.info("No targets, launching script directly")
        else:
            self._script_pre_hashes = pre_hashes
            logger.info("Prep done: %d files hashed", len(pre_hashes))
        self._launch_script(self._pending_script_path)

    def _on_prehash_finished(self, pre_hashes) -> None:
        """Pre-hash complete — now launch the script."""
        self._sync_db()
        self._script_pre_hashes = pre_hashes
        logger.info("Pre-hash done: %d files", len(pre_hashes))
        self._launch_script(self._pending_script_path)

    def _launch_script(self, script_path: Path) -> None:
        """Phase 2: Launch the script in a visible cmd window (non-blocking)."""
        import subprocess
        suffix = script_path.suffix.lower()
        if suffix == ".bat":
            cmd = ["cmd", "/c", str(script_path)]
        elif suffix == ".py":
            cmd = ["py", "-3", str(script_path)]
        else:
            self.statusBar().showMessage(f"Unsupported script: {suffix}", 5000)
            return

        import os as _os
        env = _os.environ.copy()
        env["CDUMM_GAME_DIR"] = str(self._game_dir)

        logger.info("Launching script: %s", script_path)
        self._script_proc = subprocess.Popen(
            cmd,
            cwd=str(script_path.parent),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            env=env,
        )

        self.statusBar().showMessage(
            f"Running {script_path.name} — complete the script in its window. "
            "The mod manager will capture changes when it finishes.", 60000
        )

        # Phase 3: Poll for completion with QTimer (non-blocking)
        self._script_poll_timer = QTimer(self)
        self._script_poll_timer.timeout.connect(self._poll_script_done)
        self._script_poll_timer.start(500)  # Check every 500ms

    def _poll_script_done(self) -> None:
        """Check if the script process has finished."""
        if self._script_proc.poll() is None:
            return  # Still running

        # Script finished
        self._script_poll_timer.stop()
        logger.info("Script finished with exit code: %d", self._script_proc.returncode)

        # Script finished — now capture changes on a background thread
        self.statusBar().showMessage("Script finished. Capturing changes...", 30000)
        self._cleanup_script()

        progress = ProgressDialog("Capturing Script Changes", self)
        if self._script_pre_hashes is None:
            # No pre-hash — use scan-based capture (compares vs vanilla
            # snapshot). This handles idempotent scripts that restore+repatch.
            from cdumm.gui.workers import ScanChangesWorker
            worker = ScanChangesWorker(
                self._script_mod_name,
                self._game_dir, self._db.db_path, self._deltas_dir,
            )
        else:
            from cdumm.gui.workers import ScriptCaptureWorker
            worker = ScriptCaptureWorker(
                self._script_mod_name, self._script_pre_hashes,
                self._game_dir, self._db.db_path, self._deltas_dir,
            )
        thread = QThread()
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_script_capture_finished)

    def _on_script_capture_finished(self, result) -> None:
        logger.info("Script capture callback received")
        self._sync_db()

        error = getattr(result, 'error', None) if result else "No result returned"
        if error:
            logger.info("Script capture result: %s", error)
            self.statusBar().showMessage("Script mod not captured", 10000)
            QMessageBox.warning(self, "Script Mod", error)
        else:
            name = getattr(result, 'name', 'Script mod')
            files = getattr(result, 'changed_files', [])
            logger.info("Script mod captured: %s (%d files)", name, len(files))
            self.statusBar().showMessage(
                f"Imported script mod: {name} ({len(files)} files changed). Re-applying mods...", 15000)
            self._refresh_all()

            # Re-apply all enabled mods (we restored vanilla before the .bat ran)
            self._on_apply()

    def _restore_vanilla_for_import(self, targeted: list[str], vanilla_dir: Path) -> None:
        """Restore game files to vanilla before a script import.

        If targeted files are known, only restore those. Otherwise restore
        all files that have vanilla backups (full coverage).
        """
        import os
        import shutil

        if targeted:
            files_to_restore = targeted
        else:
            # Restore all files with full vanilla backups
            files_to_restore = []
            for dirpath, _dirnames, filenames in os.walk(vanilla_dir):
                for fname in filenames:
                    if fname.endswith(".vranges"):
                        continue  # skip range backups
                    full = Path(dirpath) / fname
                    rel = full.relative_to(vanilla_dir)
                    files_to_restore.append(str(rel).replace("\\", "/"))

        restored = 0
        for rel_path in files_to_restore:
            vanilla_file = vanilla_dir / rel_path.replace("/", "\\")
            game_file = self._game_dir / rel_path.replace("/", "\\")
            if vanilla_file.exists() and game_file.exists():
                shutil.copy2(vanilla_file, game_file)
                restored += 1
                logger.info("Restored vanilla: %s", rel_path)

        if restored:
            logger.info("Restored %d files to vanilla for clean import", restored)

    def _cleanup_script(self) -> None:
        """Clean up script temp files."""
        if hasattr(self, "_script_tmp_dir") and self._script_tmp_dir:
            import shutil
            shutil.rmtree(self._script_tmp_dir, ignore_errors=True)
            self._script_tmp_dir = None

    def _on_import_finished(self, result) -> None:
        logger.info("Import callback received, syncing DB...")
        self._sync_db()

        if hasattr(result, 'error') and result.error:
            self.statusBar().showMessage(f"Import error: {result.error}", 10000)
            logger.error("Import error: %s", result.error)
        else:
            # Show health check dialog if issues were found
            health_issues = getattr(result, 'health_issues', [])
            if health_issues:
                from cdumm.gui.health_check_dialog import HealthCheckDialog
                name = getattr(result, 'name', 'Unknown')
                mod_files = {}  # files already imported at this point
                dialog = HealthCheckDialog(health_issues, name, mod_files, self)
                dialog.exec()

            name = getattr(result, 'name', 'Unknown')
            files = getattr(result, 'changed_files', [])
            self.statusBar().showMessage(
                f"Imported PAZ mod: {name} ({len(files)} files changed)", 10000
            )
            logger.info("Import success: %s (%d files)", name, len(files))

            # Stamp mod with current game version
            try:
                from cdumm.engine.version_detector import detect_game_version
                ver = detect_game_version(self._game_dir)
                if ver:
                    # Find the just-imported mod (highest id)
                    row = self._db.connection.execute("SELECT MAX(id) FROM mods").fetchone()
                    if row and row[0]:
                        self._db.connection.execute(
                            "UPDATE mods SET game_version_hash = ? WHERE id = ?",
                            (ver, row[0]))
                        self._db.connection.commit()
            except Exception as e:
                logger.debug("Game version stamp failed (non-fatal): %s", e)

            self._refresh_all()
            self._tabs.setCurrentIndex(0)  # Switch to PAZ Mods tab
            self._on_apply()  # Auto-apply after import

    def _install_asi_mod(self, path: Path) -> None:
        """Install an ASI mod by copying .asi/.ini files to bin64/."""
        from cdumm.asi.asi_manager import AsiManager
        asi_mgr = AsiManager(self._game_dir / "bin64")

        if not asi_mgr.has_loader():
            self.statusBar().showMessage(
                "Warning: ASI Loader (winmm.dll) not found in bin64/. ASI mods won't load without it.", 10000
            )
            logger.warning("ASI Loader not found, installing ASI mod anyway")

        installed = asi_mgr.install(path)
        if installed:
            self.statusBar().showMessage(
                f"Installed ASI mod: {', '.join(installed)} → bin64/", 10000
            )
            logger.info("ASI install success: %s", installed)
            # Refresh ASI panel and switch to ASI tab
            if hasattr(self, "_asi_panel"):
                self._asi_panel.refresh()
                self._tabs.setCurrentWidget(self._asi_panel)
        else:
            self.statusBar().showMessage("No ASI files found to install.", 5000)
            logger.warning("No ASI files found in %s", path)

    # --- Apply ---
    def _on_apply(self) -> None:
        if not self._db or not self._game_dir:
            return

        progress = ProgressDialog("Applying Mods", self)
        worker = ApplyWorker(self._game_dir, self._vanilla_dir, self._db.db_path)
        thread = QThread()

        self._run_worker(worker, thread, progress,
                         on_finished=self._on_apply_finished)

    def _on_apply_finished(self) -> None:
        self._needs_apply = False
        self._refresh_all()
        self.statusBar().showMessage("Mods applied successfully!", 10000)

    # --- Revert ---
    def _on_revert(self) -> None:
        if not self._db or not self._game_dir:
            return

        reply = QMessageBox.question(
            self, "Revert to Vanilla",
            "This will restore all game files to their original state.\n"
            "All applied mod changes will be removed.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        progress = ProgressDialog("Reverting to Vanilla", self)
        worker = RevertWorker(self._game_dir, self._vanilla_dir, self._db.db_path)
        thread = QThread()

        self._run_worker(worker, thread, progress,
                         on_finished=self._on_revert_finished)

    def _on_revert_finished(self) -> None:
        self._refresh_all()
        self.statusBar().showMessage("Reverted to vanilla! Game files restored.", 10000)

    # --- Remove mod ---
    def _on_remove_mod(self, mod_id: int | None = None) -> None:
        if not hasattr(self, "_mod_table") or not self._mod_manager:
            return
        if mod_id is not None:
            # Called from context menu with explicit mod_id
            cursor = self._db.connection.execute("SELECT name FROM mods WHERE id = ?", (mod_id,))
            row = cursor.fetchone()
            mod_name = row[0] if row else f"Mod {mod_id}"
        else:
            # Called from Remove Selected button
            indexes = self._mod_table.selectionModel().selectedRows()
            if not indexes:
                return
            mod = self._mod_list_model.get_mod_at_row(indexes[0].row())
            if not mod:
                return
            mod_id = mod["id"]
            mod_name = mod["name"]
        reply = QMessageBox.question(
            self, "Uninstall Mod",
            f"Uninstall '{mod_name}'?\n"
            "This deletes the mod data. If applied, you should re-apply or revert.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._mod_manager.remove_mod(mod_id)
            self._refresh_all()
            self.statusBar().showMessage(f"Uninstalled: {mod_name}. Applying changes...", 10000)
            self._on_apply()

    # --- View details ---
    def _on_view_details(self) -> None:
        if not hasattr(self, "_mod_table") or not self._mod_manager:
            return
        indexes = self._mod_table.selectionModel().selectedRows()
        if not indexes:
            return
        mod = self._mod_list_model.get_mod_at_row(indexes[0].row())
        if not mod:
            return
        self._show_mod_contents(mod["id"])

    # --- Mod context menu ---
    def _show_mod_context_menu(self, pos) -> None:
        if not hasattr(self, "_mod_table") or not self._mod_manager:
            return
        index = self._mod_table.indexAt(pos)
        if not index.isValid():
            return
        mod = self._mod_list_model.get_mod_at_row(index.row())
        if not mod:
            return

        from PySide6.QtGui import QAction
        menu = QMenu(self)

        # Enable/Disable
        if mod["enabled"]:
            toggle_action = QAction("Disable", self)
        else:
            toggle_action = QAction("Enable", self)
        toggle_action.triggered.connect(lambda: self._on_toggle_mod(mod))
        menu.addAction(toggle_action)

        menu.addSeparator()

        rename_action = QAction("Rename", self)
        rename_action.triggered.connect(lambda: self._on_rename_mod(mod))
        menu.addAction(rename_action)

        update_action = QAction("Update (replace with new version)", self)
        update_action.triggered.connect(lambda: self._on_update_mod(mod))
        menu.addAction(update_action)

        menu.addSeparator()

        remove_action = QAction("Uninstall", self)
        remove_action.triggered.connect(lambda: self._on_remove_mod(mod_id=mod["id"]))
        menu.addAction(remove_action)

        menu.exec(self._mod_table.viewport().mapToGlobal(pos))

    def _on_toggle_mod(self, mod: dict) -> None:
        if not self._mod_manager:
            return
        new_state = not mod["enabled"]
        self._mod_manager.set_enabled(mod["id"], new_state)
        self._needs_apply = True
        self._refresh_all()
        self._show_apply_reminder()

    def _on_mod_toggled_via_checkbox(self) -> None:
        self._needs_apply = True
        self._show_apply_reminder()

    def _show_apply_reminder(self) -> None:
        self.statusBar().showMessage(
            "Mod list changed — click Apply to update game files.", 0)

    def _on_rename_mod(self, mod: dict) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Rename Mod", "New name:", text=mod["name"])
        if ok and name.strip():
            self._mod_manager.rename_mod(mod["id"], name.strip())
            self._refresh_all()
            self.statusBar().showMessage(f"Renamed to: {name.strip()}", 5000)

    # --- Update mod ---
    def _on_update_mod(self, mod: dict) -> None:
        """Show overlay for drag-drop mod update."""
        if not self._db or not self._game_dir or not self._mod_manager:
            return
        from cdumm.gui.update_overlay import UpdateOverlay
        self._update_overlay = UpdateOverlay(mod["name"], parent=self.centralWidget())
        self._update_mod_target = mod
        self._update_overlay.folder_dropped.connect(self._on_update_drop)
        self._update_overlay.cancelled.connect(lambda: self._update_overlay.deleteLater())
        self._update_overlay.show_overlay()

    def _on_update_drop(self, path: Path) -> None:
        """Handle the dropped folder/zip for mod update."""
        mod = self._update_mod_target
        self._update_overlay.deleteLater()

        # Validate: check the dropped content looks like the same mod
        from cdumm.engine.import_handler import _read_modinfo
        modinfo = _read_modinfo(path) if path.is_dir() else None

        # Check by modinfo name match if available
        if modinfo and modinfo.get("name"):
            dropped_name = modinfo["name"].lower().strip()
            existing_name = mod["name"].lower().strip()
            # Allow partial matches (mod names often have version suffixes)
            if dropped_name not in existing_name and existing_name not in dropped_name:
                # Names don't match — warn the user
                reply = QMessageBox.question(
                    self, "Mod Name Mismatch",
                    f"The dropped mod is \"{modinfo['name']}\" but you're updating "
                    f"\"{mod['name']}\".\n\nAre you sure this is the right mod?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

        mod_id = mod["id"]
        logger.info("Updating mod %d (%s) from %s", mod_id, mod["name"], path)

        # Clear old deltas but keep the mod entry
        self._mod_manager.clear_deltas(mod_id)

        # Check for script mods — same flow as import but with existing mod_id
        from cdumm.engine.import_handler import detect_format
        import zipfile as _zf

        is_script_mod = False
        if path.suffix.lower() in (".bat", ".py"):
            is_script_mod = True
        elif path.suffix.lower() == ".zip":
            try:
                with _zf.ZipFile(path) as zf:
                    names = zf.namelist()
                    has_scripts = any(n.endswith((".bat", ".py")) for n in names)
                    has_game_files = any(
                        any(n.startswith(f"{i:04d}/") for i in range(33)) or n.startswith("meta/")
                        for n in names
                    )
                    if has_scripts and not has_game_files:
                        is_script_mod = True
            except _zf.BadZipFile:
                pass
        elif path.is_dir():
            scripts = list(path.glob("*.bat")) + list(path.glob("*.py"))
            game_files = any(
                (path / f"{i:04d}").exists() for i in range(33)
            ) or (path / "meta").exists()
            if scripts and not game_files:
                is_script_mod = True

        if is_script_mod:
            # Script update — use the existing script flow, mod_id will be handled
            # by ScriptCaptureWorker writing to a new mod entry.
            # For simplicity, re-import as new and delete the old one, preserving priority.
            self.statusBar().showMessage(
                "Script mods must be re-imported. Remove the old version and import the new one.", 10000)
            return

        # Regular PAZ mod update — run on background thread with existing_mod_id
        progress = ProgressDialog(f"Updating: {mod['name']}", self)
        worker = ImportWorker(path, self._game_dir, self._db.db_path,
                              self._deltas_dir, existing_mod_id=mod_id)
        thread = QThread()

        self._run_worker(worker, thread, progress,
                         on_finished=self._on_update_finished)

    def _on_update_finished(self, result) -> None:
        self._sync_db()

        error = getattr(result, 'error', None) if result else "No result"
        if error:
            self.statusBar().showMessage(f"Update error: {error}", 10000)
        else:
            name = getattr(result, 'name', 'Mod')
            files = getattr(result, 'changed_files', [])
            self.statusBar().showMessage(
                f"Updated: {name} ({len(files)} files changed)", 10000)
            self._refresh_all()

    # --- Load order ---
    def _on_move_up(self) -> None:
        if not hasattr(self, "_mod_table") or not self._mod_manager:
            return
        indexes = self._mod_table.selectionModel().selectedRows()
        if not indexes:
            return
        mod = self._mod_list_model.get_mod_at_row(indexes[0].row())
        if not mod:
            return
        self._mod_manager.move_up(mod["id"])
        self._refresh_all()
        # Re-select the moved mod
        new_row = max(0, indexes[0].row() - 1)
        self._mod_table.selectRow(new_row)
        self.statusBar().showMessage(f"Moved '{mod['name']}' up in load order", 3000)

    def _on_move_down(self) -> None:
        if not hasattr(self, "_mod_table") or not self._mod_manager:
            return
        indexes = self._mod_table.selectionModel().selectedRows()
        if not indexes:
            return
        mod = self._mod_list_model.get_mod_at_row(indexes[0].row())
        if not mod:
            return
        self._mod_manager.move_down(mod["id"])
        self._refresh_all()
        # Re-select the moved mod
        new_row = min(self._mod_list_model.rowCount() - 1, indexes[0].row() + 1)
        self._mod_table.selectRow(new_row)
        self.statusBar().showMessage(f"Moved '{mod['name']}' down in load order", 3000)

    def _on_set_winner(self, mod_id: int) -> None:
        """Set a mod as the winner (highest priority) from conflict view context menu."""
        if not self._mod_manager:
            return
        self._mod_manager.set_winner(mod_id)
        self._refresh_all()
        self.statusBar().showMessage("Load order updated — conflict resolved", 5000)

    # --- Test Mod ---
    def _on_test_mod(self) -> None:
        if not self._db or not self._snapshot or not self._game_dir:
            QMessageBox.warning(self, "Error", "Database or game directory not configured.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Mod to Test",
            "", "Mod Files (*.zip);;All Files (*)",
        )
        if not path:
            return
        self.statusBar().showMessage(f"Testing mod: {Path(path).name}...")
        from cdumm.engine.test_mod_checker import test_mod
        from cdumm.gui.test_mod_dialog import TestModDialog
        result = test_mod(Path(path), self._game_dir, self._db, self._snapshot)
        dialog = TestModDialog(result, self)
        dialog.exec()
        self.statusBar().showMessage("Test complete", 5000)

    # --- Snapshot ---
    def _on_refresh_snapshot(self) -> None:
        if not self._db or not self._game_dir:
            return

        progress = ProgressDialog("Creating Vanilla Snapshot", self)
        worker = SnapshotWorker(self._game_dir, self._db.db_path)
        thread = QThread()

        self._run_worker(worker, thread, progress,
                         on_finished=self._on_snapshot_finished)

    def _on_snapshot_finished(self, count: int) -> None:
        logger.info("Snapshot callback: %d files", count)
        self._sync_db()

        self._update_snapshot_status()
        self.statusBar().showMessage(f"Snapshot complete: {count} files indexed. You can now import mods.", 10000)
        logger.info("Snapshot finished and UI updated")

    # --- Change Game Directory ---
    def _on_change_game_dir(self) -> None:
        current = str(self._game_dir) if self._game_dir else ""
        new_dir = QFileDialog.getExistingDirectory(
            self, "Select Crimson Desert Game Directory", current)
        if not new_dir:
            return
        new_path = Path(new_dir)
        # Basic validation — check for expected game files
        if not (new_path / "meta" / "0.papgt").exists():
            QMessageBox.warning(
                self, "Invalid Directory",
                "This doesn't look like a Crimson Desert installation.\n"
                "Expected to find meta/0.papgt in the selected folder.")
            return
        from cdumm.storage.config import Config
        config = Config(self._db)
        config.set("game_directory", str(new_path))
        self._game_dir = new_path
        self._cdmods_dir = new_path / "CDMods"
        self._cdmods_dir.mkdir(parents=True, exist_ok=True)
        self._deltas_dir = self._cdmods_dir / "deltas"
        self._vanilla_dir = self._cdmods_dir / "vanilla"
        self.statusBar().showMessage(f"Game directory changed to: {new_path}", 10000)
        QMessageBox.information(
            self, "Game Directory Changed",
            f"Game directory set to:\n{new_path}\n\n"
            "You should click 'Refresh Snapshot' to index the new installation.")

    # --- Bug Report ---
    def _on_report_bug(self) -> None:
        from cdumm.gui.bug_report import generate_bug_report, BugReportDialog
        report = generate_bug_report(self._db, self._game_dir, self._app_data_dir)
        dialog = BugReportDialog(report, self)
        dialog.exec()

    def _offer_crash_report(self) -> None:
        reply = QMessageBox.question(
            self, "Previous Session Crashed",
            "It looks like the app didn't close normally last time.\n"
            "This could indicate a bug.\n\n"
            "Would you like to generate a bug report?\n"
            "(You can attach it to a Nexus Mods bug report)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            from cdumm.gui.bug_report import generate_bug_report, BugReportDialog
            report = generate_bug_report(self._db, self._game_dir, self._app_data_dir)
            dialog = BugReportDialog(report, self, is_crash=True)
            dialog.exec()

    # --- Profiles ---
    def _on_profiles(self) -> None:
        from cdumm.gui.profile_dialog import ProfileDialog
        dialog = ProfileDialog(self._db, self)
        dialog.exec()
        if dialog.was_profile_loaded:
            self._refresh_all()
            self._on_apply()

    # --- Export/Import Mod List ---
    def _on_export_list(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Mod List", "cdumm_modlist.json", "JSON Files (*.json)")
        if not path:
            return
        from cdumm.engine.mod_list_io import export_mod_list
        count = export_mod_list(self._db, Path(path))
        self.statusBar().showMessage(f"Exported {count} mods to {Path(path).name}", 10000)

    def _on_import_list(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Mod List", "", "JSON Files (*.json)")
        if not path:
            return
        from cdumm.engine.mod_list_io import import_mod_list
        mods = import_mod_list(Path(path))
        if not mods:
            QMessageBox.information(self, "Import List", "No mods found in the file.")
            return
        # Show what mods the list contains vs what we have installed
        installed = {m["name"].lower() for m in (self._mod_manager.list_mods() if self._mod_manager else [])}
        lines = []
        missing = 0
        for m in mods:
            status = "installed" if m["name"].lower() in installed else "MISSING"
            if status == "MISSING":
                missing += 1
            lines.append(f"[{status}] {m['name']}" + (f" by {m['author']}" if m.get('author') else ""))
        QMessageBox.information(
            self, "Mod List",
            f"{len(mods)} mods in list, {missing} not installed:\n\n" + "\n".join(lines))

    # --- Update Check ---
    def _check_for_updates(self) -> None:
        from cdumm import __version__
        from cdumm.engine.update_checker import UpdateCheckWorker
        worker = UpdateCheckWorker(__version__)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.update_available.connect(self._on_update_available)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda: setattr(self, '_update_thread', None))
        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    def _on_update_available(self, info: dict) -> None:
        download_url = info.get("download_url", "")
        if download_url:
            reply = QMessageBox.question(
                self, "Update Available",
                f"A new version is available: {info['tag']}\n\n"
                f"{info['body'][:300]}\n\n"
                "Download and install automatically?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._download_and_apply_update(download_url)
        else:
            import webbrowser
            reply = QMessageBox.information(
                self, "Update Available",
                f"A new version is available: {info['tag']}\n\n"
                "Open the download page?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes and info.get("url"):
                webbrowser.open(info["url"])

    def _download_and_apply_update(self, download_url: str) -> None:
        from cdumm.engine.update_checker import UpdateDownloadWorker
        progress = ProgressDialog("Downloading Update", self)
        worker = UpdateDownloadWorker(download_url)
        thread = QThread()
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_update_downloaded)

    def _on_update_downloaded(self, new_exe_path) -> None:
        if not new_exe_path:
            QMessageBox.warning(self, "Update Failed", "Download failed. Try again later.")
            return
        from pathlib import Path
        from cdumm.engine.update_checker import apply_update
        reply = QMessageBox.question(
            self, "Update Ready",
            "Update downloaded. The app will close and install the update.\nPlease relaunch CDUMM after the update window closes.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            apply_update(Path(str(new_exe_path)))

    # --- View Mod Contents ---
    def _show_mod_contents(self, mod_id: int) -> None:
        mod = None
        for m in self._mod_list_model._mods:
            if m["id"] == mod_id:
                mod = m
                break
        if mod:
            from cdumm.gui.mod_contents_dialog import ModContentsDialog
            dialog = ModContentsDialog(mod, self._mod_manager, self)
            dialog.exec()

    def closeEvent(self, event) -> None:
        """Clean shutdown — remove lock file so next startup knows we exited cleanly."""
        if hasattr(self, "_lock_file") and self._lock_file.exists():
            self._lock_file.unlink()
        super().closeEvent(event)
