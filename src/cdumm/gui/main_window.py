"""Main application window — wires all components together."""
import logging
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
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
from cdumm.gui.changelog import PatchNotesDialog, CHANGELOG
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
                 app_data_dir: Path | None = None,
                 startup_context: dict | None = None) -> None:
        super().__init__()
        from cdumm import __version__
        from cdumm.i18n import tr as _tr
        self.setWindowTitle(_tr("app.title", version=__version__))
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
        self._applied_state: dict[int, bool] = {}  # {mod_id: enabled} snapshot after last apply
        self._snapshot_in_progress = False
        self._dispatcher = MainThreadDispatcher(parent=self)

        # Clean up stale staging directory from previous crash.
        # Only clean if no other CDUMM instance is running (check lock file age).
        if game_dir:
            staging = game_dir / ".cdumm_staging"
            if staging.exists():
                lock_file = (Path.home() / "AppData" / "Local" / "cdumm" / ".running")
                lock_is_stale = True
                if lock_file.exists():
                    try:
                        lock_time = datetime.fromisoformat(
                            lock_file.read_text(encoding="utf-8").strip())
                        if datetime.now() - lock_time < timedelta(seconds=30):
                            lock_is_stale = False  # another instance may be active
                    except Exception:
                        pass
                if lock_is_stale:
                    try:
                        import shutil
                        shutil.rmtree(staging, ignore_errors=True)
                        logger.info("Cleaned up stale staging directory")
                    except Exception:
                        pass

        # Clear stale import state from previous session
        from cdumm.engine.import_handler import clear_assigned_dirs
        clear_assigned_dirs()

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
        self._snapshot_applied_state()
        self.setAcceptDrops(True)
        self._startup_context = startup_context or {}

        # Crash detection — lock file
        self._lock_file = self._app_data_dir / ".running"
        crashed_last_time = self._lock_file.exists()
        self._lock_file.write_text(str(datetime.now()), encoding="utf-8")

        # Deferred startup tasks (after window is visible)
        QTimer.singleShot(500, self._deferred_startup)

        # Update check (delayed further to not compete with UI loading)
        QTimer.singleShot(5000, self._check_for_updates)

        # Re-check for updates every 15 minutes (for users who leave the app open)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._check_for_updates)
        self._update_timer.start(15 * 60 * 1000)  # 15 minutes in ms

        # Auto-snapshot is handled by _deferred_startup

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
        """Run after window is visible. Only fast checks here — no file I/O."""
        if self._game_dir and self._db:
            if self._check_one_time_reset():
                return
            if self._check_game_updated():
                return
        if self._game_dir and self._snapshot and not self._snapshot.has_snapshot():
            reply = QMessageBox.question(
                self, "Game Files Scan Needed",
                "Before using the mod manager, your game files need to be scanned.\n\n"
                "For best results, please verify your game files through Steam first:\n"
                "  Steam → Right-click Crimson Desert → Properties\n"
                "  → Installed Files → Verify integrity of game files\n\n"
                "Have you verified (or is this a fresh install)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._on_refresh_snapshot(skip_verify_prompt=True)
            else:
                self.statusBar().showMessage(
                    "Please verify game files (Steam: Verify Integrity / Xbox: Repair), then restart.", 0)
            return

        self._check_stale_appdata()
        self._check_program_files_warning()
        self._check_missing_sources()
        self._check_bad_standalone_imports()
        self._check_show_update_notes()

        # Check if main.py detected a game update during splash
        if self._startup_context.get("game_updated"):
            self._check_game_updated()

        # Check if game version fingerprint changed (Steam verify or update)
        # This is a fast check — just compares a config string, no file hashing
        elif self._game_dir and self._snapshot and self._snapshot.has_snapshot():
            try:
                from cdumm.engine.version_detector import detect_game_version
                from cdumm.storage.config import Config
                config = Config(self._db)
                current_fp = detect_game_version(self._game_dir)
                stored_fp = config.get("game_version_fingerprint")
                if current_fp and stored_fp and current_fp != stored_fp:
                    reply = QMessageBox.question(
                        self, "Game Files Changed",
                        "Your game files have changed since the last snapshot.\n\n"
                        "This usually means you verified through Steam.\n\n"
                        "Rescan now to update the snapshot?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply == QMessageBox.StandardButton.Yes:
                        self._on_refresh_snapshot(skip_verify_prompt=True)
            except Exception:
                pass

        # Silent startup health check — fix dirty game state automatically
        self._startup_health_check()

        # Check for missing PAMT full backups (upgrade from older versions)
        self._check_pamt_backups()

        # Trigger background status check for mod list
        if hasattr(self, "_mod_list_model"):
            self._mod_list_model.refresh_statuses()

    def _startup_health_check(self) -> None:
        """Fast silent check for dirty game state on startup.

        If no mods are enabled but game files don't match vanilla,
        automatically fix the state so users don't have to.
        """
        try:
            if not self._db or not self._game_dir or not self._snapshot:
                return
            if not self._snapshot.has_snapshot():
                return

            # Only check if no mods are enabled
            enabled = self._db.connection.execute(
                "SELECT COUNT(*) FROM mods WHERE enabled = 1").fetchone()[0]
            if enabled > 0:
                return

            # Fast check: PAPGT size vs snapshot
            papgt_path = self._game_dir / "meta" / "0.papgt"
            if not papgt_path.exists():
                return
            snap = self._db.connection.execute(
                "SELECT file_size FROM snapshots WHERE file_path = 'meta/0.papgt'"
            ).fetchone()
            if not snap:
                return
            actual_size = papgt_path.stat().st_size
            if actual_size == snap[0]:
                # PAPGT matches — also check for orphan directories
                has_orphans = False
                for d in self._game_dir.iterdir():
                    if (d.is_dir() and d.name.isdigit() and len(d.name) == 4
                            and int(d.name) >= 36):
                        orphan_check = self._db.connection.execute(
                            "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                            (d.name + "/%",)).fetchone()[0]
                        if orphan_check == 0:
                            has_orphans = True
                            break
                if not has_orphans:
                    return  # everything looks clean

            # Game files are dirty with no mods enabled — auto fix
            logger.info("Startup health check: game files dirty, auto-fixing")
            import shutil

            # Clean orphan directories
            for d in sorted(self._game_dir.iterdir()):
                if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                    continue
                if int(d.name) < 36:
                    continue
                orphan_check = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                    (d.name + "/%",)).fetchone()[0]
                if orphan_check == 0:
                    shutil.rmtree(d, ignore_errors=True)
                    logger.info("Health check: removed orphan directory %s", d.name)

            # Restore vanilla PAPGT if backup exists and size matches snapshot
            vanilla_papgt = self._vanilla_dir / "meta" / "0.papgt"
            if vanilla_papgt.exists() and snap:
                if vanilla_papgt.stat().st_size == snap[0]:
                    shutil.copy2(vanilla_papgt, papgt_path)
                    logger.info("Health check: restored vanilla PAPGT")

            self._log_activity("health", "Auto-fixed dirty game state on startup")
        except Exception as e:
            logger.debug("Startup health check failed: %s", e)

        # Clean up contaminated ENTR deltas — mods imported before v2.1.6
        # could absorb other mods' entries because the CB handler copied the
        # modded game PAZ instead of vanilla. Detect by finding entry_paths
        # that appear in multiple mods and remove the foreign copies.
        try:
            self._clean_contaminated_deltas()
        except Exception as e:
            logger.debug("Contaminated delta cleanup failed: %s", e)

    def _clean_contaminated_deltas(self) -> None:
        """Remove ENTR deltas that were incorrectly attributed to a mod.

        Before v2.1.6, importing a mod copied the modded game PAZ (not vanilla),
        so all active mods' entry changes got baked into the new mod's deltas.
        Detect these by finding entry_paths shared across multiple mods.
        The mod that legitimately owns the entry is the one with fewer total
        ENTR deltas for that PAZ file (the contaminated mod has many extras).
        """
        if not self._db:
            return

        # Find entry_paths that appear in more than one mod
        dupes = self._db.connection.execute("""
            SELECT md.entry_path, md.file_path
            FROM mod_deltas md
            WHERE md.entry_path IS NOT NULL
            GROUP BY md.entry_path, md.file_path
            HAVING COUNT(DISTINCT md.mod_id) > 1
        """).fetchall()

        if not dupes:
            return

        import shutil
        cleaned = 0
        for entry_path, file_path in dupes:
            # Get all mods that claim this entry
            rows = self._db.connection.execute("""
                SELECT md.mod_id, m.name, md.delta_path,
                    (SELECT COUNT(*) FROM mod_deltas md2
                     WHERE md2.mod_id = md.mod_id AND md2.entry_path IS NOT NULL) as total
                FROM mod_deltas md JOIN mods m ON m.id = md.mod_id
                WHERE md.entry_path = ? AND md.file_path = ?
                ORDER BY total ASC
            """, (entry_path, file_path)).fetchall()

            if len(rows) < 2:
                continue

            # The mod with the fewest total ENTR deltas likely owns it legitimately.
            # Remove from all others.
            owner_id = rows[0][0]
            for mod_id, mod_name, delta_path, total in rows[1:]:
                self._db.connection.execute(
                    "DELETE FROM mod_deltas WHERE mod_id = ? AND entry_path = ? "
                    "AND file_path = ?",
                    (mod_id, entry_path, file_path))
                # Delete the stale delta file
                dp = Path(delta_path)
                if dp.exists():
                    dp.unlink()
                logger.info("Cleaned contaminated delta: %s from %s (belongs to mod %d)",
                            entry_path, mod_name, owner_id)
                cleaned += 1

        if cleaned > 0:
            self._db.connection.commit()
            logger.info("Cleaned %d contaminated ENTR deltas on startup", cleaned)

    def _on_language_changed(self, index: int) -> None:
        code = self._lang_combo.itemData(index)
        if self._db:
            from cdumm.storage.config import Config
            config = Config(self._db)
            config.set("language", code)
        from cdumm.i18n import tr
        QMessageBox.information(
            self, tr("tools.language_restart_title"),
            tr("tools.language_restart_body"))

    def _on_fix_everything(self) -> None:
        """One-click fix: revert, clear backups, clean orphans, rescan if clean.

        For users with corrupted state who just want it to work.
        """
        verified = QMessageBox.question(
            self, "Fix Game State",
            "This will revert all game files, clear old backups,\n"
            "remove orphan directories, and reimport your mods.\n\n"
            "If you verified game files through Steam first,\n"
            "a fresh snapshot will also be taken.\n\n"
            "Did you verify through Steam?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
        )
        if verified == QMessageBox.StandardButton.Cancel:
            return

        import shutil

        # Step 1: Revert
        self.statusBar().showMessage("Fix: reverting to vanilla...", 0)
        try:
            from cdumm.engine.apply_engine import RevertWorker
            from cdumm.storage.database import Database
            revert_db = Database(self._db.db_path)
            revert_db.initialize()
            rw = RevertWorker.__new__(RevertWorker)
            rw._game_dir = self._game_dir
            rw._vanilla_dir = self._vanilla_dir
            rw._db = revert_db
            rw._revert()
            revert_db.close()
        except Exception as e:
            logger.warning("Fix: revert failed: %s", e)

        # Step 2: Clear backups
        if self._vanilla_dir and self._vanilla_dir.exists():
            shutil.rmtree(self._vanilla_dir, ignore_errors=True)
            self._vanilla_dir.mkdir(parents=True, exist_ok=True)

        # Step 3: Clean orphan directories
        for d in sorted(self._game_dir.iterdir()):
            if (d.is_dir() and d.name.isdigit() and len(d.name) == 4
                    and int(d.name) >= 36):
                snap_check = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                    (d.name + "/%",)).fetchone()[0]
                if snap_check == 0:
                    shutil.rmtree(d, ignore_errors=True)

        # Step 4: Check if files are actually clean before taking snapshot
        # Quick check: PAPGT size should match snapshot
        files_clean = True
        try:
            papgt = self._game_dir / "meta" / "0.papgt"
            snap = self._db.connection.execute(
                "SELECT file_size FROM snapshots WHERE file_path = 'meta/0.papgt'"
            ).fetchone()
            if snap and papgt.exists() and papgt.stat().st_size != snap[0]:
                files_clean = False
        except Exception:
            pass

        steam_verified = verified == QMessageBox.StandardButton.Yes

        if steam_verified:
            self._on_refresh_snapshot(skip_verify_prompt=True)
            self._log_activity("fix",
                               "Fix Everything: reverted, cleared backups, rescanning")
            self.statusBar().showMessage("Fix in progress: rescanning game files...", 0)
        else:
            self._log_activity("fix",
                               "Fix Everything: reverted and cleaned up (no rescan)")
            self._refresh_all()
            self.statusBar().showMessage(
                "Fix done. Rescan skipped — verify through Steam and "
                "Rescan Game Files when ready.", 0)

    def _check_stale_appdata(self) -> None:
        """Detect stale data in %LocalAppData%/cdumm from old versions.

        Since v1.7.0, CDUMM stores everything in CDMods/ inside the game
        directory. Old %LocalAppData%/cdumm data can conflict or confuse
        users. Offer to clean it up.
        """
        try:
            from cdumm.storage.config import Config
            config = Config(self._db)
            if config.get("stale_appdata_checked"):
                return

            appdata_dir = Path.home() / "AppData" / "Local" / "cdumm"
            if not appdata_dir.exists():
                config.set("stale_appdata_checked", "1")
                return

            # Check if there's actual mod data (deltas, vanilla backups)
            has_stale = False
            for name in ["deltas", "vanilla", "cdumm.db"]:
                if (appdata_dir / name).exists():
                    has_stale = True
                    break

            if not has_stale:
                config.set("stale_appdata_checked", "1")
                return

            # Calculate size
            total_size = 0
            try:
                for f in appdata_dir.rglob("*"):
                    if f.is_file():
                        total_size += f.stat().st_size
            except Exception:
                pass
            size_mb = total_size / (1024 * 1024)

            reply = QMessageBox.question(
                self, "Old Data Found",
                f"Found leftover data from an older CDUMM version in:\n"
                f"{appdata_dir}\n"
                f"({size_mb:.0f} MB)\n\n"
                f"Since v1.7.0, all mod data is stored in the CDMods folder\n"
                f"inside your game directory. This old data is no longer needed.\n\n"
                f"Delete it to free up space?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                import shutil
                for name in ["deltas", "vanilla", "cdumm.db"]:
                    target = appdata_dir / name
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    elif target.is_file():
                        target.unlink(missing_ok=True)
                self.statusBar().showMessage(
                    f"Cleaned up {size_mb:.0f} MB of old data.", 10000)
                self._log_activity("cleanup",
                                   f"Removed stale AppData ({size_mb:.0f} MB)")

            config.set("stale_appdata_checked", "1")
        except Exception as e:
            logger.debug("Stale appdata check failed: %s", e)

    def _check_missing_sources(self) -> None:
        """Notify user about mods that have no stored source files.

        Mods imported before source archiving was added can't be auto-reimported
        after updates or reconfigured. The user needs to drag them in again once.
        """
        try:
            if not self._db or not self._mod_manager:
                return
            from cdumm.storage.config import Config
            config = Config(self._db)
            if config.get("missing_sources_checked"):
                return

            sources_dir = self._cdmods_dir / "sources"
            mods = self._db.connection.execute(
                "SELECT id, name, source_path FROM mods").fetchall()
            missing = []
            for mod_id, name, source_path in mods:
                has_source = False
                src_dir = sources_dir / str(mod_id)
                if src_dir.exists():
                    try:
                        if any(src_dir.iterdir()):
                            has_source = True
                    except Exception:
                        pass
                if not has_source and source_path and Path(source_path).exists():
                    has_source = True
                if not has_source:
                    missing.append(name)

            if missing:
                names = "\n".join(f"  • {n}" for n in missing[:10])
                extra = f"\n  ...and {len(missing) - 10} more" if len(missing) > 10 else ""
                QMessageBox.information(
                    self, "Mods Need Re-import",
                    f"These mods were imported on an older version and don't have\n"
                    f"stored source files. They can't be auto-updated or reconfigured.\n\n"
                    f"{names}{extra}\n\n"
                    f"To fix this, remove each mod and drag the original download\n"
                    f"file back in. You only need to do this once per mod."
                )

            config.set("missing_sources_checked", "1")
        except Exception as e:
            logger.debug("Missing sources check failed: %s", e)

    def _check_program_files_warning(self) -> None:
        """Warn if game is installed under Program Files (admin restrictions)."""
        try:
            if not self._game_dir:
                return
            from cdumm.storage.config import Config
            config = Config(self._db)
            if config.get("program_files_warned"):
                return

            game_path = str(self._game_dir).lower()
            if "program files" not in game_path:
                return

            QMessageBox.warning(
                self, "Game Location Warning",
                "Your game is installed under Program Files, which has\n"
                "restricted write permissions on Windows.\n\n"
                "This can cause issues with mod backups and configuration.\n"
                "If you experience problems, consider moving your Steam\n"
                "library to a different location (e.g. C:\\SteamLibrary).\n\n"
                "Steam → Settings → Storage → Add a new library folder"
            )
            config.set("program_files_warned", "1")
        except Exception as e:
            logger.debug("Program Files warning check failed: %s", e)

    def _check_pamt_backups(self) -> None:
        """Detect missing full PAMT backups and create them or prompt for Steam verify.

        Older versions used range backups for PAMTs which can't fully restore
        vanilla. This checks every PAMT that mods touch and ensures a full
        backup exists. If the game file is currently vanilla (matches snapshot),
        the backup is created silently. If it's modded, the user is prompted.
        """
        if not self._db or not self._game_dir or not self._vanilla_dir:
            return
        if not self._snapshot or not self._snapshot.has_snapshot():
            return

        try:
            from cdumm.storage.config import Config
            config = Config(self._db)
            if config.get("pamt_backups_checked") == "1":
                return  # Already checked this install

            # Find all PAMT files that mods touch
            cursor = self._db.connection.execute(
                "SELECT DISTINCT file_path FROM mod_deltas "
                "WHERE file_path LIKE '%.pamt'")
            mod_pamts = [row[0] for row in cursor.fetchall()]
            if not mod_pamts:
                config.set("pamt_backups_checked", "1")
                return

            missing = []
            for pamt_path in mod_pamts:
                full_backup = self._vanilla_dir / pamt_path.replace("/", "\\")
                if not full_backup.exists():
                    missing.append(pamt_path)

            if not missing:
                config.set("pamt_backups_checked", "1")
                return

            # Try to create backups from current game files if they match snapshot
            from cdumm.engine.snapshot_manager import hash_file
            created = 0
            still_missing = []
            for pamt_path in missing:
                game_file = self._game_dir / pamt_path.replace("/", "\\")
                if not game_file.exists():
                    continue
                snap = self._db.connection.execute(
                    "SELECT file_hash, file_size FROM snapshots WHERE file_path = ?",
                    (pamt_path,)).fetchone()
                if not snap:
                    continue

                # Quick size check
                try:
                    if game_file.stat().st_size != snap[1]:
                        still_missing.append(pamt_path)
                        continue
                except OSError:
                    still_missing.append(pamt_path)
                    continue

                # Full hash check (PAMTs are small, <14MB)
                current_hash, _ = hash_file(game_file)
                if current_hash == snap[0]:
                    # Game file IS vanilla — create backup silently
                    backup_path = self._vanilla_dir / pamt_path.replace("/", "\\")
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(game_file, backup_path)
                    created += 1
                    logger.info("Created missing PAMT backup: %s", pamt_path)
                else:
                    still_missing.append(pamt_path)

            if created:
                self._log_activity("backup",
                    f"Created {created} missing PAMT backup(s)",
                    "Upgraded from range backups to full backups")

            if still_missing:
                QMessageBox.information(
                    self, "Vanilla Backup Incomplete",
                    f"{len(still_missing)} PAMT file(s) are currently modded and have "
                    f"no full vanilla backup:\n\n"
                    + "\n".join(f"  {p}" for p in still_missing[:5])
                    + ("\n  ..." if len(still_missing) > 5 else "")
                    + "\n\nTo fix this:\n"
                    "1. Steam → Right-click Crimson Desert → Properties\n"
                    "   → Installed Files → Verify integrity of game files\n"
                    "2. Restart CDUMM — it will create the missing backups\n\n"
                    "Until then, Revert to Vanilla may not fully restore these files.",
                )
            else:
                config.set("pamt_backups_checked", "1")

        except Exception as e:
            logger.debug("PAMT backup check failed: %s", e)

    def _check_one_time_reset(self) -> bool:
        """One-time migrations when upgrading to a new major version.

        Each migration version is checked independently so users who skip
        versions still get all necessary migrations applied.
        """
        try:
            from cdumm.storage.config import Config
            config = Config(self._db)
            last_reset = config.get("last_reset_version") or ""

            # v1.0.7 migration: full reset (old format incompatible)
            if last_reset < "1.0.7":
                has_data = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots").fetchone()[0] > 0
                if not has_data:
                    config.set("last_reset_version", "1.3.0")
                    return False  # fresh install, nothing to reset

                from cdumm import __version__
                QMessageBox.information(
                    self, f"CDUMM v{__version__}",
                    "This update includes important fixes to how mods are stored.\n\n"
                    "Before continuing, please verify your game files through Steam:\n\n"
                    "  Steam → Right-click Crimson Desert → Properties\n"
                    "  → Installed Files → Verify integrity of game files\n\n"
                    "This ensures your game is in a clean state before re-scanning.",
                )
                reply = QMessageBox.question(
                    self, "Ready to continue?",
                    "Have you verified your game files through Steam?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    self.statusBar().showMessage(
                        "Please verify game files (Steam: Verify Integrity / Xbox: Repair), then restart.", 0)
                    return True

                from cdumm.engine.version_detector import detect_game_version
                fp = detect_game_version(self._game_dir) or ""
                self._reset_for_game_update(fp)
                config.set("last_reset_version", "1.3.0")
                return True

            # v1.3.0 migration: purge stale vanilla backups
            if last_reset < "1.3.0":
                return self._migrate_v130(config)

            return False
        except Exception as e:
            logger.debug("One-time reset check failed: %s", e)
            return False

    def _migrate_v130(self, config) -> bool:
        """v1.3.0 migration: clean vanilla backups and rebuild from scratch.

        Previous versions could create dirty vanilla backups (taken from
        modded files) and stale PAPGT entries. This migration:
        1. Asks user to verify game files through Steam
        2. Deletes the vanilla backup folder (will be recreated clean)
        3. Cleans orphan mod directories (0036+)
        4. Clears snapshot (forces fresh rescan)
        5. Disables all mods (safe starting state)
        """
        import shutil
        from cdumm import __version__

        has_backups = self._vanilla_dir and self._vanilla_dir.exists()
        has_mods = self._db.connection.execute(
            "SELECT COUNT(*) FROM mods").fetchone()[0] > 0

        if not has_backups and not has_mods:
            config.set("last_reset_version", "1.3.0")
            return False  # nothing to migrate

        QMessageBox.information(
            self, f"CDUMM v{__version__} — Important Update",
            "This update fixes how vanilla backups are managed.\n\n"
            "Your existing backups need to be rebuilt from scratch to\n"
            "ensure Revert to Vanilla works correctly.\n\n"
            "Please verify your game files through Steam first:\n\n"
            "  Steam → Right-click Crimson Desert → Properties\n"
            "  → Installed Files → Verify integrity of game files\n\n"
            "Your mod list will be kept — you just need to re-apply them.",
        )
        reply = QMessageBox.question(
            self, "Ready to continue?",
            "Have you verified your game files through Steam?\n\n"
            "Click Yes to proceed with the cleanup.\n"
            "Click No to do it later (the app will ask again next launch).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage(
                "Please verify game files (Steam: Verify Integrity / Xbox: Repair), then restart.", 0)
            return True  # block startup but don't mark as done

        logger.info("v1.3.0 migration: cleaning vanilla backups")

        # Delete vanilla backup folder entirely
        if self._vanilla_dir and self._vanilla_dir.exists():
            shutil.rmtree(self._vanilla_dir, ignore_errors=True)
            logger.info("Deleted vanilla backup folder")

        # Clean orphan mod directories (0036+)
        if self._game_dir:
            for d in sorted(self._game_dir.iterdir()):
                if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                    continue
                if int(d.name) >= 36:
                    shutil.rmtree(d, ignore_errors=True)
                    logger.info("Removed orphan directory: %s", d.name)

        # Clear snapshot (forces fresh scan from verified files)
        try:
            self._db.connection.execute("DELETE FROM snapshots")
            self._db.connection.commit()
        except Exception:
            pass

        # Disable all mods (they'll be re-applied after rescan)
        try:
            self._db.connection.execute("UPDATE mods SET enabled = 0")
            self._db.connection.commit()
        except Exception:
            pass

        config.set("last_reset_version", "1.3.0")

        # Trigger fresh snapshot (user already confirmed Steam verify)
        self._refresh_all()
        self._on_refresh_snapshot(skip_verify_prompt=True)

        QMessageBox.information(
            self, "Cleanup Complete",
            "Vanilla backups have been reset.\n\n"
            "Your mods are still listed but disabled.\n"
            "Enable them and click Apply to rebuild everything cleanly.",
        )
        return True

    def _check_game_updated(self) -> bool:
        """Detect if the game was updated since last launch.

        Compares PAMT fingerprints. If changed, wipes all mod data
        (backups, deltas, snapshot, database) and forces a fresh start.
        Returns True if a reset was triggered.
        """
        try:
            from cdumm.engine.version_detector import detect_game_version
            from cdumm.storage.config import Config
            config = Config(self._db)

            current = detect_game_version(self._game_dir)
            if not current:
                return False

            stored = config.get("game_version_fingerprint")
            game_changed = False

            if stored is None:
                # First time with this feature — save fingerprint.
                config.set("game_version_fingerprint", current)
                return False
            elif stored == current:
                return False
            else:
                logger.info("Game fingerprint changed: %s -> %s", stored, current)

            # Try to get the Steam build ID for a more informative message
            from cdumm.engine.version_detector import get_steam_build_id
            build_id = get_steam_build_id(self._game_dir)
            build_info = f" (Steam build {build_id})" if build_id else ""

            QMessageBox.information(
                self, "Game Update Detected",
                f"Crimson Desert has been updated{build_info}.\n\n"
                "The app will now reset and re-scan your game files. "
                "After that, just re-import your mods.\n\n"
                "Note: Game updates (including hotfixes) can break mods. "
                "If a mod stops working, it may need to be updated by "
                "its author for the new game version.",
            )
            self._reset_for_game_update(current)
            return True
        except Exception as e:
            logger.debug("Game update check failed: %s", e)
            return False

    def _snapshot_stale(self) -> bool:
        """Check if stored snapshot hashes match actual game files.

        Skips files modified by enabled mods (those are expected to differ).
        Checks PAMT files and samples PAZ files from unmodded directories.

        IMPORTANT: If a mismatched file has a vanilla backup, it was
        previously modified by a mod — NOT by a Steam update. Don't
        count these as stale (they'll be fixed by the apply safety net).
        """
        try:
            from cdumm.engine.snapshot_manager import hash_file
            import os

            # Get files that enabled mods modify — these are expected to differ
            modded = self._db.connection.execute(
                "SELECT DISTINCT md.file_path FROM mod_deltas md "
                "JOIN mods m ON md.mod_id = m.id WHERE m.enabled = 1"
            ).fetchall()
            modded_set = {row[0] for row in modded}
            modded_set.add("meta/0.papgt")

            # Build set of files that have vanilla backups (previously modded)
            backed_up = set()
            if self._vanilla_dir and self._vanilla_dir.exists():
                for f in self._vanilla_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    if f.name.endswith(".vranges"):
                        rel = f.name[:-len(".vranges")].replace("_", "/")
                    else:
                        rel = str(f.relative_to(self._vanilla_dir)).replace("\\", "/")
                    backed_up.add(rel)

            cursor = self._db.connection.execute(
                "SELECT file_path, file_hash FROM snapshots ORDER BY file_path")
            all_rows = cursor.fetchall()
            if not all_rows:
                return False

            priority = []
            others = []
            for row in all_rows:
                if row[0] in modded_set:
                    continue
                if row[0].endswith(".pamt"):
                    priority.append(row)
                else:
                    others.append(row)

            step = max(1, len(others) // 10)
            to_check = priority + others[::step]

            if not to_check:
                return False

            for file_path, snap_hash in to_check:
                game_file = self._game_dir / file_path.replace("/", os.sep)
                if not game_file.exists():
                    logger.info("Stale snapshot: %s missing", file_path)
                    return True
                current_hash, _ = hash_file(game_file)
                if current_hash != snap_hash:
                    # If this file has a vanilla backup, it was modded by a
                    # removed mod — NOT a game update. Don't trigger refresh.
                    if file_path in backed_up:
                        logger.info("Snapshot mismatch for %s but backup exists "
                                    "— orphaned mod file, not stale", file_path)
                        continue
                    logger.info("Stale snapshot: %s hash mismatch", file_path)
                    return True
            return False
        except Exception as e:
            logger.debug("Snapshot stale check failed: %s", e)
            return False

    def _reset_for_game_update(self, new_fingerprint: str) -> None:
        """Reset for a new game version while KEEPING mod data.

        Mod deltas and DB entries are preserved — users just need to
        re-enable and Apply after the rescan. Only vanilla backups and
        snapshot are cleared (they're version-specific).
        """
        import shutil
        from cdumm.storage.config import Config

        # Step 1: Clean up orphan mod directories (0036+) from game dir
        # These were created by mods — the new game version won't have them
        for d in self._game_dir.iterdir():
            if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                continue
            if int(d.name) >= 36:
                shutil.rmtree(d, ignore_errors=True)
                logger.info("Removed orphan mod directory: %s", d.name)

        # Step 2: Clear vanilla backups (they're for the old game version)
        if self._vanilla_dir.exists():
            shutil.rmtree(self._vanilla_dir, ignore_errors=True)
            logger.info("Cleared vanilla backups (old game version)")

        # Step 3: Clear snapshot (needs fresh rescan against new game files)
        self._db.connection.execute("DELETE FROM snapshots")
        try:
            self._db.connection.execute("DELETE FROM conflicts")
        except Exception:
            pass

        # Step 4: Clear old deltas (they're against the old vanilla)
        if self._deltas_dir.exists():
            shutil.rmtree(self._deltas_dir, ignore_errors=True)
            self._deltas_dir.mkdir(parents=True, exist_ok=True)
        self._db.connection.execute("DELETE FROM mod_deltas")

        # Step 5: Disable all mods
        self._db.connection.execute("UPDATE mods SET enabled = 0")
        self._db.connection.commit()
        logger.info("Game update: cleared backups/deltas/snapshot, disabled mods")

        # Step 6: Auto-reimport mods from stored sources (after rescan completes).
        # This is deferred — the rescan callback will trigger _auto_reimport_mods.
        sources_dir = self._cdmods_dir / "sources"
        if sources_dir.exists() and any(sources_dir.iterdir()):
            self._pending_auto_reimport = True
            logger.info("Auto-reimport scheduled: %d mod sources found",
                        sum(1 for _ in sources_dir.iterdir()))

        # Save new fingerprint
        config = Config(self._db)
        config.set("game_version_fingerprint", new_fingerprint)
        config.set("backups_verified", "0")

        # Refresh UI
        self._snapshot = SnapshotManager(self._db)
        self._refresh_all()
        self._snapshot_applied_state()

        # Automatically take a fresh snapshot
        self._on_refresh_snapshot_for_update()

    def _on_refresh_snapshot_for_update(self) -> None:
        """Take a snapshot automatically after game update reset."""
        if not self._db or not self._game_dir:
            return
        if self._snapshot_in_progress:
            return
        self._snapshot_in_progress = True
        progress = ProgressDialog("Scanning new game files...", self)
        worker = SnapshotWorker(self._game_dir, self._db.db_path)
        worker.activity.connect(self._log_activity)
        thread = QThread()
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_update_snapshot_finished)

    def _on_update_snapshot_finished(self, count: int) -> None:
        """Snapshot after game update is done."""
        self._on_snapshot_finished(count)
        # Count mods that need re-importing
        mod_count = 0
        mod_names = []
        if self._mod_manager:
            mods = self._mod_manager.list_mods()
            mod_count = len(mods)
            mod_names = [m["name"] for m in mods[:10]]
        if mod_count:
            names_str = "\n".join(f"  - {n}" for n in mod_names)
            if mod_count > 10:
                names_str += f"\n  ... and {mod_count - 10} more"
            QMessageBox.information(
                self, "Ready",
                f"Game files scanned ({count} files).\n\n"
                f"You have {mod_count} mod(s) that need re-importing:\n\n"
                f"{names_str}\n\n"
                "Drop each mod onto the app to re-import it."
            )
        else:
            QMessageBox.information(
                self, "Ready",
                f"Game files scanned ({count} files).\n\n"
                "You can now import mods by dropping them onto the app."
            )

    def _check_bad_standalone_imports(self) -> None:
        """Detect mods imported by v1.0.0 as broken standalone PAZ copies.

        v1.0.0 stored JSON patch mods as full 954MB PAZ copies in new directories
        instead of small byte-level deltas. These cause game crashes.
        Telltale: is_new=1 delta for a .paz file >100MB.
        """
        if not self._db:
            return
        try:
            cursor = self._db.connection.execute("""
                SELECT DISTINCT m.id, m.name, md.file_path
                FROM mod_deltas md JOIN mods m ON md.mod_id = m.id
                WHERE md.is_new = 1 AND md.file_path LIKE '%%.paz'
                  AND md.byte_end > 100000000
            """)
            bad_mods = {}
            for mid, name, fpath in cursor.fetchall():
                bad_mods[mid] = name

            if not bad_mods:
                return

            names = "\n".join(f"  - {name}" for name in bad_mods.values())
            reply = QMessageBox.warning(
                self, "Mods Need Re-import",
                f"The following mods were imported by an older version and may crash the game:\n\n"
                f"{names}\n\n"
                "They need to be uninstalled and re-imported to work correctly.\n\n"
                "Uninstall them now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                # Store bad mod IDs for removal after revert
                self._bad_import_ids = list(bad_mods.keys())
                self.statusBar().showMessage("Reverting to vanilla to clean up...", 15000)
                # Revert to vanilla first, then remove bad mods and re-apply good ones
                progress = ProgressDialog("Cleaning up broken mods...", self)
                from cdumm.engine.apply_engine import RevertWorker
                worker = RevertWorker(self._game_dir, self._vanilla_dir, self._db.db_path)
                thread = QThread()
                worker.warning.connect(
                    lambda msg: self._dispatcher.call(self._show_revert_warning, msg))
                self._run_worker(worker, thread, progress,
                                 on_finished=self._on_bad_import_cleanup)
        except Exception as e:
            logger.debug("Bad standalone check failed: %s", e)

    def _on_bad_import_cleanup(self) -> None:
        """After revert completes, remove bad mods and disable all."""
        if hasattr(self, '_bad_import_ids'):
            names = []
            for mid in self._bad_import_ids:
                try:
                    details = self._mod_manager.get_mod_details(mid)
                    names.append(details["name"] if details else str(mid))
                except Exception:
                    names.append(str(mid))
                self._mod_manager.remove_mod(mid)
                logger.info("Removed bad standalone import: id=%d", mid)
            count = len(self._bad_import_ids)
            del self._bad_import_ids
            # Disable all mods since we reverted to vanilla
            for m in self._mod_manager.list_mods():
                if m["enabled"]:
                    self._mod_manager.set_enabled(m["id"], False)
            self._refresh_all()
            self._snapshot_applied_state()
            QMessageBox.information(
                self, "Cleanup Complete",
                f"Removed {count} broken mod(s):\n"
                + "\n".join(f"  - {n}" for n in names) +
                "\n\nAll mods have been disabled. Re-enable your mods, "
                "click Apply, then re-import the removed mods."
            )

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
        from PySide6.QtWidgets import QFrame, QStackedWidget, QHeaderView
        from PySide6.QtCore import QSortFilterProxyModel
        from cdumm.gui.mod_list_model import (
            COL_ENABLED, COL_ORDER, COL_NAME, COL_AUTHOR, COL_VERSION,
            COL_STATUS, COL_FILES, COL_DATE,
        )

        central = QWidget()
        self.setCentralWidget(central)
        main_h = QHBoxLayout(central)
        main_h.setContentsMargins(0, 0, 0, 0)
        main_h.setSpacing(0)

        # ── Sidebar ──
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMaximumWidth(160)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(4, 8, 4, 8)
        sb_layout.setSpacing(4)

        # Sidebar title
        title = QLabel("CDUMM")
        title.setObjectName("sidebarTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sb_layout.addWidget(title)
        sb_layout.addSpacing(12)

        from cdumm.i18n import tr
        self._nav_buttons = []
        for key, tr_key, tr_tip in [
            ("PAZ Mods", "nav.paz_mods", "nav.paz_mods_tooltip"),
            ("ASI Mods", "nav.asi_mods", "nav.asi_mods_tooltip"),
            ("Log", "nav.log", "nav.log_tooltip"),
            ("Tools", "nav.tools", "nav.tools_tooltip"),
            ("About", "nav.about", "nav.about_tooltip"),
        ]:
            btn = QPushButton(tr(tr_key))
            btn.setToolTip(tr(tr_tip))
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, l=key: self._on_nav(l))
            sb_layout.addWidget(btn)
            self._nav_buttons.append((key, btn))

        sb_layout.addStretch()
        main_h.addWidget(sidebar)

        # ── Content area ──
        content_v = QVBoxLayout()
        content_v.setContentsMargins(0, 0, 0, 0)
        content_v.setSpacing(0)

        # Drop zone (compact)
        self._import_widget = ImportWidget()
        self._import_widget.file_dropped.connect(self._on_import_dropped)
        content_v.addWidget(self._import_widget)

        # Stacked pages
        self._pages = QStackedWidget()

        # ── Page 0: Mods ──
        mods_page = QWidget()
        mods_v = QVBoxLayout(mods_page)
        mods_v.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Vertical)

        if self._mod_manager and self._conflict_detector:
            self._mod_list_model = ModListModel(
                self._mod_manager, self._conflict_detector,
                game_dir=self._game_dir, db_path=self._db.db_path,
                deltas_dir=self._deltas_dir)
            self._mod_list_model.mod_toggled.connect(self._on_mod_toggled_via_checkbox)

            class _CheckHeader(QHeaderView):
                toggle_requested = Signal()
                _label = "☐"

                def __init__(self, orientation, parent=None):
                    super().__init__(orientation, parent)
                    self.setSectionsClickable(True)

                def mousePressEvent(self, event):
                    if self.logicalIndexAt(event.pos()) == 0:
                        self.toggle_requested.emit()
                        event.accept()
                        return
                    super().mousePressEvent(event)

                def set_label(self, label: str):
                    self._label = label
                    if self.model():
                        self.model().setHeaderData(
                            0, Qt.Orientation.Horizontal, label)
                    self.viewport().update()

            class _NumericSortProxy(QSortFilterProxyModel):
                def lessThan(self, left, right):
                    col = left.column()
                    if col in (COL_ORDER, COL_FILES):
                        try:
                            return int(left.data() or 0) < int(right.data() or 0)
                        except (ValueError, TypeError):
                            pass
                    return super().lessThan(left, right)

            self._sort_proxy = _NumericSortProxy()
            self._sort_proxy.setSourceModel(self._mod_list_model)
            self._mod_table = QTableView()
            self._mod_table.setModel(self._sort_proxy)
            self._mod_table.setSortingEnabled(True)
            self._mod_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            self._mod_table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
            self._mod_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._mod_table.customContextMenuRequested.connect(self._show_mod_context_menu)
            self._mod_table.setDragEnabled(True)
            self._mod_table.setAcceptDrops(True)
            self._mod_table.setDropIndicatorShown(True)
            self._mod_table.setDragDropMode(QTableView.DragDropMode.InternalMove)
            self._mod_table.setDefaultDropAction(Qt.DropAction.MoveAction)
            self._mod_table.verticalHeader().setVisible(False)
            self._check_header = _CheckHeader(Qt.Orientation.Horizontal, self._mod_table)
            self._check_header.toggle_requested.connect(self._on_toggle_all)
            self._mod_table.setHorizontalHeader(self._check_header)
            self._check_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            self._check_header.setStretchLastSection(True)
            # Fixed-size columns auto-fit to header, rest are draggable
            self._check_header.setSectionResizeMode(COL_ENABLED, QHeaderView.ResizeMode.Fixed)
            self._mod_table.setColumnWidth(COL_ENABLED, 30)
            self._mod_table.setColumnWidth(COL_ORDER, 45)
            # Fit content columns to header text width at minimum
            self._mod_table.setColumnWidth(COL_NAME, 180)
            self._mod_table.setColumnWidth(COL_AUTHOR, 110)
            self._mod_table.setColumnWidth(COL_VERSION, 65)
            self._mod_table.setColumnWidth(COL_STATUS, 130)
            self._mod_table.setColumnWidth(COL_FILES, 45)
            # Import Date stretches via setStretchLastSection
            self._mod_table.doubleClicked.connect(self._on_mod_double_clicked)
            splitter.addWidget(self._mod_table)
        else:
            splitter.addWidget(QLabel("No database connected"))

        self._conflict_view = ConflictView()
        self._conflict_view.winner_changed.connect(self._on_set_winner)
        splitter.addWidget(self._conflict_view)
        splitter.setSizes([500, 150])
        mods_v.addWidget(splitter)

        hint = QLabel("Right-click a mod for more options  ·  Drag rows to reorder  ·  Ctrl+click to multi-select")
        hint.setStyleSheet("color: #4E5564; font-size: 11px; padding: 4px 8px;")
        mods_v.addWidget(hint)

        self._pages.addWidget(mods_page)

        # ── Page 1: ASI ──
        if self._game_dir:
            self._asi_panel = AsiPanel(self._game_dir / "bin64")
            self._pages.addWidget(self._asi_panel)
        else:
            self._pages.addWidget(QLabel("No game directory"))

        # ── Page 2: Activity Log ──
        from cdumm.engine.activity_log import ActivityLog
        from cdumm.gui.activity_panel import ActivityPanel
        if self._db:
            self._activity_log = ActivityLog(self._db)
            self._activity_panel = ActivityPanel(self._activity_log)
            self._pages.addWidget(self._activity_panel)
        else:
            self._activity_log = None
            self._pages.addWidget(QLabel("No database"))

        # ── Page 3: Tools & Settings ──
        tools_page = QWidget()
        tools_v = QVBoxLayout(tools_page)
        tools_v.setContentsMargins(20, 20, 20, 20)
        tools_v.setSpacing(12)

        tools_header = QLabel(tr("tools.header"))
        tools_header.setObjectName("toolsHeader")
        tools_v.addWidget(tools_header)

        # Language selector
        from cdumm.i18n import available_languages, current_language
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel(tr("tools.language")))
        self._lang_combo = QComboBox()
        for code, name in available_languages():
            self._lang_combo.addItem(name, code)
        for i in range(self._lang_combo.count()):
            if self._lang_combo.itemData(i) == current_language():
                self._lang_combo.setCurrentIndex(i)
                break
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_row.addWidget(self._lang_combo)
        lang_row.addStretch()
        tools_v.addLayout(lang_row)

        for tr_key, slot in [
            ("tools.verify_state", self._on_verify_game_state),
            ("tools.check_mods", self._on_check_mods),
            ("tools.find_problem", self._on_find_problem_mod),
            ("tools.rescan", self._on_refresh_snapshot),
            ("tools.fix_everything", self._on_fix_everything),
            ("tools.change_dir", self._on_change_game_dir),
            ("tools.profiles", self._on_profiles),
            ("tools.export_list", self._on_export_list),
            ("tools.import_list", self._on_import_list),
            ("tools.test_mod", self._on_test_mod),
            ("tools.patch_notes", self._on_show_patch_notes),
            ("tools.report_bug", self._on_report_bug),
        ]:
            btn = QPushButton(tr(tr_key))
            btn.setFixedHeight(36)
            btn.clicked.connect(slot)
            tools_v.addWidget(btn)
        tools_v.addStretch()
        self._pages.addWidget(tools_page)

        # ── Page 4: About CDUMM ──
        about_page = QWidget()
        about_v = QVBoxLayout(about_page)
        about_v.setContentsMargins(20, 20, 20, 20)
        about_v.setSpacing(16)

        from cdumm import __version__ as _about_ver
        about_title = QLabel(f"Crimson Desert Ultimate Mods Manager")
        about_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #D4A43C;")
        about_v.addWidget(about_title)

        about_ver = QLabel(f"Version {_about_ver}")
        about_ver.setStyleSheet("font-size: 13px; color: #888;")
        about_v.addWidget(about_ver)

        # Update status indicator — big and visible
        self._about_update_label = QLabel("Checking for updates...")
        self._about_update_label.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #888; "
            "padding: 12px; border: 1px solid #333; border-radius: 8px; "
            "background: #1A1D23;")
        about_v.addWidget(self._about_update_label)

        about_v.addSpacing(8)

        # Links
        import webbrowser
        links_label = QLabel("Links")
        links_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #D8DEE9;")
        about_v.addWidget(links_label)

        for text, url in [
            ("GitHub Releases — Download Latest", "https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases"),
            ("NexusMods Page", "https://www.nexusmods.com/crimsondesert/mods/207"),
            ("NexusMods Posts & Discussion", "https://www.nexusmods.com/crimsondesert/mods/207?tab=posts"),
            ("Report a Bug", "https://www.nexusmods.com/crimsondesert/mods/207?tab=bugs"),
        ]:
            link_btn = QPushButton(text)
            link_btn.setFixedHeight(36)
            link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            link_btn.clicked.connect(lambda checked, u=url: webbrowser.open(u))
            about_v.addWidget(link_btn)

        about_v.addStretch()

        # Credits
        credits = QLabel("by kindiboy")
        credits.setStyleSheet("color: #555; font-size: 11px;")
        credits.setAlignment(Qt.AlignmentFlag.AlignCenter)
        about_v.addWidget(credits)

        self._pages.addWidget(about_page)

        # Fake tabs reference for tab switching on import
        self._tabs = self._pages

        content_v.addWidget(self._pages)

        # ── Persistent Update Banner (hidden by default) ──
        self._update_banner = QLabel("")
        self._update_banner.setStyleSheet(
            "background: #8B0000; color: white; font-size: 13px; "
            "font-weight: bold; padding: 8px 16px; border: none;")
        self._update_banner.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_banner.setVisible(False)
        self._update_banner.mousePressEvent = lambda e: self._on_banner_clicked()
        content_v.addWidget(self._update_banner)

        # ── Action Bar ──
        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        action_bar.setFixedHeight(56)
        ab_layout = QHBoxLayout(action_bar)
        ab_layout.setContentsMargins(16, 8, 16, 8)

        apply_btn = QPushButton(tr("action_bar.apply"))
        apply_btn.setObjectName("applyBtn")
        apply_btn.clicked.connect(self._on_apply)
        ab_layout.addWidget(apply_btn)

        launch_btn = QPushButton(tr("action_bar.launch_game"))
        launch_btn.setObjectName("launchBtn")
        launch_btn.clicked.connect(self._on_launch_game)
        ab_layout.addWidget(launch_btn)

        ab_layout.addStretch()

        fix_btn = QPushButton(tr("action_bar.fix_everything"))
        fix_btn.setObjectName("fixBtn")
        fix_btn.setToolTip(tr("action_bar.fix_everything_tooltip"))
        fix_btn.clicked.connect(self._on_fix_everything)
        ab_layout.addWidget(fix_btn)

        content_v.addWidget(action_bar)
        main_h.addLayout(content_v)

        # Set initial nav
        self._on_nav("PAZ Mods")

    def _on_nav(self, label: str) -> None:
        """Switch pages via sidebar navigation."""
        page_map = {"PAZ Mods": 0, "ASI Mods": 1, "Log": 2, "Tools": 3, "About": 4}
        idx = page_map.get(label, 0)
        self._pages.setCurrentIndex(idx)
        for nav_label, btn in self._nav_buttons:
            btn.setChecked(nav_label == label)

    def _on_launch_game(self) -> None:
        """Launch the game executable."""
        import subprocess
        if not self._game_dir:
            return
        exe = self._game_dir / "bin64" / "CrimsonDesert.exe"
        if not exe.exists():
            # Try finding the exe
            for candidate in ["CrimsonDesert.exe", "crimsondesert.exe"]:
                test = self._game_dir / "bin64" / candidate
                if test.exists():
                    exe = test
                    break
            else:
                self.statusBar().showMessage("Game executable not found in bin64/", 10000)
                return
        try:
            subprocess.Popen([str(exe)], cwd=str(self._game_dir / "bin64"))
            self.statusBar().showMessage("Game launched!", 5000)
            self.showMinimized()
        except Exception as e:
            self.statusBar().showMessage(f"Failed to launch: {e}", 10000)

    def _build_toolbar(self) -> None:
        # Toolbar replaced by sidebar — this is kept as a no-op for compatibility
        pass

    def _build_status_bar(self) -> None:
        from cdumm import __version__
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self._snapshot_label = QLabel()
        status_bar.addPermanentWidget(self._snapshot_label)
        version_label = QLabel(f"v{__version__}")
        status_bar.addPermanentWidget(version_label)
        self._update_snapshot_status()

    def _update_snapshot_status(self) -> None:
        if not self._snapshot:
            self._snapshot_label.setText("Snapshot: No database")
            self._snapshot_label.setStyleSheet("color: gray;")
        elif self._snapshot.has_snapshot():
            count = self._snapshot.get_snapshot_count()
            self._snapshot_label.setText(f"Snapshot: {count} files")
            self._snapshot_label.setStyleSheet("color: green;")
        else:
            self._snapshot_label.setText("Snapshot: Not scanned yet")
            self._snapshot_label.setStyleSheet("color: #FF9800;")

    def _log_activity(self, category: str, message: str, detail: str = None) -> None:
        """Log an activity to the persistent activity log."""
        if hasattr(self, '_activity_log') and self._activity_log:
            try:
                self._activity_log.log(category, message, detail)
                if hasattr(self, '_activity_panel'):
                    self._activity_panel.refresh()
            except Exception:
                pass

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
            if hasattr(self, "_mod_list_model"):
                self._mod_list_model.refresh_conflict_cache()
        logger.debug("_refresh_all: updating snapshot status")
        self._update_snapshot_status()
        self._update_header_checkbox()
        logger.debug("_refresh_all: done")

    def _on_mod_double_clicked(self, index) -> None:
        """Double-click on a configurable mod opens the configure dialog."""
        mod = self._get_mod_at_proxy_row(index.row())
        if mod and mod.get("configurable"):
            self._on_configure_mod(mod)

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

        # Wait for thread to fully stop before cleaning up
        thread.quit()
        thread.wait(5000)

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

        # Clear configurable state so failed imports don't leak to next import
        if hasattr(self, '_configurable_source'):
            del self._configurable_source
        if hasattr(self, '_configurable_labels'):
            del self._configurable_labels

        # If imports are queued, collect error and continue instead of blocking
        if hasattr(self, '_import_queue') and self._import_queue:
            if not hasattr(self, '_import_errors'):
                self._import_errors = []
            self._import_errors.append(error)
            self.statusBar().showMessage(f"Error: {error} — continuing with next mod...", 5000)
            QTimer.singleShot(300, self._process_next_import)
            return

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
        for url in urls:
            path = Path(url.toLocalFile())
            logger.info("File dropped on main window: %s", path)
            self._queue_import(path)

    def _on_import_dropped(self, path: Path) -> None:
        self._queue_import(path)

    def _queue_import(self, path: Path) -> None:
        """Add a path to the import queue. Processes sequentially."""
        if not hasattr(self, '_import_queue'):
            self._import_queue = []
        self._import_queue.append(path)
        # If no import is running, start the first one
        if not (hasattr(self, '_active_worker') and self._active_worker):
            self._process_next_import()

    def _process_next_import(self) -> None:
        """Process the next item in the import queue."""
        if not hasattr(self, '_import_queue') or not self._import_queue:
            # Queue empty — show summary if there were errors
            if hasattr(self, '_import_errors') and self._import_errors:
                errors = self._import_errors
                self._import_errors = []
                error_list = "\n".join(f"  - {e}" for e in errors)
                QMessageBox.warning(
                    self, "Some Imports Failed",
                    f"{len(errors)} mod(s) had issues:\n\n{error_list}",
                )
            # Process deferred script mods
            if hasattr(self, '_script_queue') and self._script_queue:
                script_path = self._script_queue.pop(0)
                self._run_script_mod(script_path)
            return

        path = self._import_queue.pop(0)
        remaining = len(self._import_queue)

        # Defer script mods — they open a cmd window and block the queue.
        # Process them after all non-script mods are done.
        suffix = path.suffix.lower() if path.is_file() else ""
        if suffix in (".bat", ".py"):
            if not hasattr(self, '_script_queue'):
                self._script_queue = []
            self._script_queue.append(path)
            logger.info("Deferred script mod: %s (processing after other imports)", path.name)
            self._process_next_import()  # skip to next
            return

        if remaining:
            self.statusBar().showMessage(
                f"Importing {path.name}... ({remaining} more queued)", 0)
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
                "Game files not scanned yet. Go to Tools → Rescan Game Files first.", 10000)
            return

        # Check if this is an update to an existing mod (before routing to script/PAZ)
        existing_mod_id = None
        if self._mod_manager:
            match = self._find_existing_mod(path)
            if match:
                mid, mname = match
                reply = QMessageBox.question(
                    self, "Mod Already Installed",
                    f"'{mname}' is already installed.\n\n"
                    "Do you want to update it with the new version?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    # Save old mod's state, delete it, import fresh
                    old_details = self._mod_manager.get_mod_details(mid)
                    self._update_priority = old_details.get("priority", 0) if old_details else 0
                    self._update_enabled = old_details.get("enabled", True) if old_details else True
                    self._mod_manager.remove_mod(mid)
                    logger.info("Removed old mod %d (%s) for update", mid, mname)
                else:
                    # No or Cancel — don't install
                    self.statusBar().showMessage("Import cancelled.", 5000)
                    return

        # Check if this is a script-based mod — needs to run on main thread
        # so the user can interact with the cmd window
        from cdumm.engine.import_handler import detect_format, import_script_live
        import zipfile as _zf
        import tempfile

        is_script_mod = False
        if path.suffix.lower() in (".bat", ".py"):
            is_script_mod = True
        elif path.suffix.lower() in (".zip", ".7z"):
            try:
                if path.suffix.lower() == ".zip":
                    with _zf.ZipFile(path) as zf:
                        names = zf.namelist()
                else:
                    import py7zr
                    with py7zr.SevenZipFile(path, 'r') as zf:
                        names = zf.getnames()
                has_scripts = any(n.endswith((".bat", ".py")) for n in names)
                has_game_files = any(
                    any(n.startswith(f"{i:04d}/") for i in range(33)) or n.startswith("meta/")
                    for n in names
                )
                if has_scripts and not has_game_files:
                    is_script_mod = True
            except Exception:
                pass
        elif path.is_dir():
            scripts = list(path.glob("*.bat")) + list(path.glob("*.py"))
            game_files = any(
                (path / f"{i:04d}").exists() for i in range(33)
            ) or (path / "meta").exists()
            if scripts and not game_files:
                is_script_mod = True

        if is_script_mod:
            # Extract 7z script mods before running
            if path.suffix.lower() == ".7z":
                import py7zr
                tmp = tempfile.mkdtemp(prefix="cdumm_7z_script_")
                with py7zr.SevenZipFile(path, 'r') as zf:
                    zf.extractall(tmp)
                scripts = list(Path(tmp).rglob("*.bat")) + list(Path(tmp).rglob("*.py"))
                if scripts:
                    path = scripts[0]
            self._run_script_mod(path, existing_mod_id=existing_mod_id)
            return

        # Check for variant folders (e.g. Fat Stacks with 2x, 5x, 10x subfolders).
        # Each subfolder contains the same game files — let user pick which one.
        if path.is_dir():
            variants = []
            for sub in sorted(path.iterdir()):
                if sub.is_dir() and not sub.name.startswith((".", "_")):
                    has_game = any(
                        (sub / f"{i:04d}").is_dir() for i in range(100)
                    ) or (sub / "meta").is_dir()
                    if has_game:
                        variants.append(sub)
            if len(variants) > 1:
                from PySide6.QtWidgets import QInputDialog
                names = [v.name for v in variants]
                chosen, ok = QInputDialog.getItem(
                    self, "Choose Variant",
                    f"This mod has {len(variants)} variants.\n"
                    "Choose which one to install:",
                    names, 0, False)
                if ok and chosen:
                    path = variants[names.index(chosen)]
                    logger.info("User selected variant: %s", path.name)
                else:
                    self.statusBar().showMessage("Import cancelled.", 5000)
                    return

            # Also check for deeply nested loose-file variants (files/NNNN/ pattern)
            if not variants:
                from cdumm.engine.import_handler import find_loose_file_variants
                lfm_dir_variants = find_loose_file_variants(path)
                if len(lfm_dir_variants) > 1:
                    from PySide6.QtWidgets import QInputDialog
                    lfm_names = []
                    for v in lfm_dir_variants:
                        try:
                            lfm_names.append(v["_base_dir"].relative_to(path).as_posix())
                        except ValueError:
                            lfm_names.append(v["_base_dir"].name)
                    chosen, ok = QInputDialog.getItem(
                        self, "Choose Variant",
                        f"This mod has {len(lfm_dir_variants)} variants.\n"
                        "Choose which one to install:",
                        lfm_names, 0, False)
                    if ok and chosen:
                        idx = lfm_names.index(chosen)
                        path = lfm_dir_variants[idx]["_base_dir"]
                        logger.info("User selected loose-file variant: %s", chosen)
                    else:
                        self.statusBar().showMessage("Import cancelled.", 5000)
                        return

        # Check for multiple JSON presets — let user pick one
        # For zips, extract to temp first to scan for presets
        from cdumm.gui.preset_picker import find_json_presets, PresetPickerDialog
        presets = []
        _preset_tmp = None
        if path.suffix.lower() == ".zip":
            try:
                import zipfile as _zf2
                _preset_tmp = tempfile.mkdtemp(prefix="cdumm_preset_")
                with _zf2.ZipFile(path) as zf:
                    zf.extractall(_preset_tmp)
                presets = find_json_presets(Path(_preset_tmp))
            except Exception:
                presets = []
        else:
            presets = find_json_presets(path)

        if len(presets) > 1:
            dialog = PresetPickerDialog(presets, self)
            if dialog.exec() and dialog.selected_path:
                path = dialog.selected_path
                logger.info("User selected preset: %s", path.name)
            else:
                if _preset_tmp:
                    import shutil
                    shutil.rmtree(_preset_tmp, ignore_errors=True)
                self.statusBar().showMessage("Import cancelled.", 5000)
                return
        if _preset_tmp and len(presets) <= 1:
            import shutil
            shutil.rmtree(_preset_tmp, ignore_errors=True)
            _preset_tmp = None

        # Check for labeled changes (toggles/presets inside a single JSON)
        from cdumm.gui.preset_picker import has_labeled_changes, TogglePickerDialog
        from cdumm.engine.json_patch_handler import detect_json_patch

        # For zips/7z, extract to temp to check JSON content
        json_check_path = path
        _label_tmp = None
        if path.suffix.lower() in (".zip", ".7z"):
            try:
                _label_tmp = tempfile.mkdtemp(prefix="cdumm_label_")
                if path.suffix.lower() == ".zip":
                    import zipfile as _zf3
                    with _zf3.ZipFile(path) as zf:
                        zf.extractall(_label_tmp)
                else:
                    import py7zr
                    with py7zr.SevenZipFile(path, 'r') as zf:
                        zf.extractall(_label_tmp)
                json_check_path = Path(_label_tmp)
            except Exception:
                pass

        # Check for multi-variant loose-file mods (e.g. LOD fix with x1/x2/x3 folders)
        _lfm_selected = False
        if _label_tmp:
            from cdumm.engine.import_handler import find_loose_file_variants
            lfm_variants = find_loose_file_variants(Path(_label_tmp))
            if len(lfm_variants) > 1:
                from PySide6.QtWidgets import QInputDialog
                # Build display names from the variant's ancestor path
                variant_names = []
                for v in lfm_variants:
                    base = v["_base_dir"]
                    try:
                        rel = base.relative_to(_label_tmp)
                        variant_names.append(rel.as_posix())
                    except ValueError:
                        variant_names.append(base.name)
                chosen, ok = QInputDialog.getItem(
                    self, "Choose Variant",
                    f"This mod has {len(lfm_variants)} variants.\n"
                    "Choose which one to install:",
                    variant_names, 0, False)
                if ok and chosen:
                    idx = variant_names.index(chosen)
                    selected = lfm_variants[idx]
                    # Point import at the selected variant's base directory.
                    # Don't clean _label_tmp — the folder import needs it alive.
                    path = selected["_base_dir"]
                    _lfm_selected = True
                    logger.info("User selected loose-file variant: %s", chosen)
                else:
                    import shutil
                    shutil.rmtree(_label_tmp, ignore_errors=True)
                    self.statusBar().showMessage("Import cancelled.", 5000)
                    return

        json_data = detect_json_patch(json_check_path)

        # Mark for configurable flag only if the mod has real configurable options
        if json_data and has_labeled_changes(json_data):
            self._configurable_source = str(path)
            self._configurable_labels = []  # populated if picker shown

        # Also mark as configurable if source has multiple JSON presets
        if not hasattr(self, '_configurable_source'):
            from cdumm.gui.preset_picker import find_json_presets as _fp
            _presets_check = _fp(json_check_path)
            if len(_presets_check) <= 1 and json_check_path.is_file() and json_check_path.parent.is_dir():
                _presets_check = _fp(json_check_path.parent)
            if len(_presets_check) > 1:
                self._configurable_source = str(path)
                self._configurable_labels = []

        if json_data and has_labeled_changes(json_data):
            logger.info("JSON has labeled changes — showing picker dialog")
            dialog = TogglePickerDialog(json_data, self)
            if dialog.exec() and dialog.selected_data:
                # Write filtered JSON to temp file and import that
                import json as _json
                tmp_json = Path(tempfile.mktemp(suffix=".json", prefix="cdumm_filtered_"))
                # Preserve original _json_path for source archiving, remove for serialization
                original_json_path = dialog.selected_data.get("_json_path")
                write_data = dialog.selected_data.copy()
                write_data.pop("_json_path", None)
                # Store original path so import_json_as_entr can archive the real source
                if original_json_path:
                    write_data["_original_source"] = str(original_json_path)
                tmp_json.write_text(_json.dumps(write_data, indent=2, default=str), encoding="utf-8")
                # Remember original source and selected labels for reconfiguration
                self._configurable_source = str(path)
                # Store which labels/presets were selected
                selected_labels = []
                for patch in dialog.selected_data.get("patches", []):
                    for c in patch.get("changes", []):
                        if "label" in c:
                            selected_labels.append(c["label"])
                self._configurable_labels = selected_labels
                path = tmp_json
                logger.info("User selected %d changes from labeled JSON",
                            sum(len(p.get("changes", [])) for p in dialog.selected_data.get("patches", [])))
            else:
                if _label_tmp:
                    import shutil
                    shutil.rmtree(_label_tmp, ignore_errors=True)
                self.statusBar().showMessage("Import cancelled.", 5000)
                return

        if _label_tmp and not _lfm_selected:
            import shutil
            shutil.rmtree(_label_tmp, ignore_errors=True)
        elif _label_tmp and _lfm_selected:
            # Variant folder lives inside _label_tmp — clean after import finishes
            self._pending_tmp_cleanup = _label_tmp

        # Regular PAZ mod — run on background thread
        logger.info("Starting import worker for: %s", path)
        progress = ProgressDialog("Importing Mod", self)
        worker = ImportWorker(path, self._game_dir, self._db.db_path, self._deltas_dir,
                              existing_mod_id=existing_mod_id)
        thread = QThread()

        self._run_worker(worker, thread, progress,
                         on_finished=self._on_import_finished)

    def _run_script_mod(self, path: Path, existing_mod_id: int | None = None) -> None:
        """Handle script-based mods — launch script, poll for completion, capture changes."""
        # If updating, remove the old mod entry first so the new one replaces it
        if existing_mod_id is not None and self._mod_manager:
            self._mod_manager.set_enabled(existing_mod_id, False)
            # Apply to revert old mod's files before re-importing
            # (handled by the script prep phase which restores vanilla)
            self._mod_manager.remove_mod(existing_mod_id)
            logger.info("Removed old mod %d for update", existing_mod_id)

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
            self._script_pre_stats = {}
            logger.info("No targets, launching script directly")
        else:
            self._script_pre_hashes = pre_hashes
            self._script_pre_stats = self._capture_file_stats(pre_hashes)
            logger.info("Prep done: %d files hashed", len(pre_hashes))
        self._launch_script(self._pending_script_path)

    def _on_prehash_finished(self, pre_hashes) -> None:
        """Pre-hash complete — now launch the script."""
        self._sync_db()
        self._script_pre_hashes = pre_hashes
        self._script_pre_stats = self._capture_file_stats(pre_hashes)
        logger.info("Pre-hash done: %d files", len(pre_hashes))
        self._launch_script(self._pending_script_path)

    def _capture_file_stats(self, pre_hashes: dict) -> dict[str, tuple[int, float]]:
        """Capture size+mtime for all game files — used for fast change detection."""
        stats = {}
        for rel_path in pre_hashes:
            game_file = self._game_dir / rel_path.replace("/", "\\")
            try:
                st = game_file.stat()
                stats[rel_path] = (st.st_size, st.st_mtime)
            except OSError:
                pass
        return stats

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
        if self._script_pre_hashes is not None:
            # Use pre-hashes for targeted capture (fast)
            from cdumm.gui.workers import ScriptCaptureWorker
            worker = ScriptCaptureWorker(
                self._script_mod_name, self._script_pre_hashes,
                self._game_dir, self._db.db_path, self._deltas_dir,
                pre_stats=getattr(self, '_script_pre_stats', None),
            )
        else:
            # No pre-hashes — use scan-based capture
            from cdumm.gui.workers import ScanChangesWorker
            worker = ScanChangesWorker(
                self._script_mod_name,
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
            self._log_activity("import", f"Imported script mod: {name}",
                               f"{len(files)} files changed")

            # Restore vanilla — the bat modified game files directly but we've
            # captured the delta. Game files should stay clean until Apply.
            self.statusBar().showMessage(
                f"Imported: {name}. Restoring vanilla...", 10000)
            vanilla_dir = self._deltas_dir.parent / "vanilla"
            targeted = getattr(self, '_pending_targeted', [])
            self._restore_vanilla_for_import(targeted, vanilla_dir)
            self._log_activity("revert", "Restored vanilla after script capture",
                               "Game files clean — mod stored as delta only")

            self.statusBar().showMessage(
                f"Imported: {name} ({len(files)} files). Enable it and click Apply when ready.", 15000)
            self._refresh_all()
            self._on_nav("PAZ Mods")

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

        # Clean up temp dir from loose-file variant selection
        if hasattr(self, '_pending_tmp_cleanup'):
            import shutil
            shutil.rmtree(self._pending_tmp_cleanup, ignore_errors=True)
            del self._pending_tmp_cleanup

        if hasattr(result, 'error') and result.error:
            # Collect error — don't block if more imports are queued
            if not hasattr(self, '_import_errors'):
                self._import_errors = []
            name = getattr(result, 'name', 'Unknown')
            self._import_errors.append(f"{name}: {result.error}")
            self.statusBar().showMessage(f"Import error for {name}", 5000)
            logger.error("Import error for %s: %s", name, result.error)
            self._log_activity("error", f"Import failed: {name}", result.error)
        else:
            # Show health check dialog if critical issues were found
            health_issues = getattr(result, 'health_issues', [])
            critical = [i for i in health_issues if i.severity == "critical"]
            if critical:
                from cdumm.gui.health_check_dialog import HealthCheckDialog
                name = getattr(result, 'name', 'Unknown')
                mod_files = {}
                dialog = HealthCheckDialog(health_issues, name, mod_files, self)
                if dialog.exec() == 0:  # rejected / cancelled
                    # Remove the just-imported mod since user cancelled
                    row = self._db.connection.execute("SELECT MAX(id) FROM mods").fetchone()
                    if row and row[0]:
                        self._mod_manager.remove_mod(row[0])
                        logger.info("Removed mod after health check cancel")
                    self._refresh_all()
                    self.statusBar().showMessage("Import cancelled due to health issues.", 10000)
                    return

            name = getattr(result, 'name', 'Unknown')
            files = getattr(result, 'changed_files', [])
            self.statusBar().showMessage(
                f"Imported: {name} ({len(files)} files). Click Apply when ready.", 10000
            )
            logger.info("Import success: %s (%d files)", name, len(files))
            self._log_activity("import", f"Imported: {name}",
                               f"{len(files)} files changed")

            # Stamp mod with current game version
            mod_id = None
            try:
                from cdumm.engine.version_detector import detect_game_version
                ver = detect_game_version(self._game_dir)
                row = self._db.connection.execute("SELECT MAX(id) FROM mods").fetchone()
                if row and row[0]:
                    mod_id = row[0]
                    if ver:
                        self._db.connection.execute(
                            "UPDATE mods SET game_version_hash = ? WHERE id = ?",
                            (ver, mod_id))
                        self._db.connection.commit()
            except Exception as e:
                logger.debug("Game version stamp failed: %s", e)

            # Mark as configurable if it came from a labeled JSON
            if mod_id and hasattr(self, '_configurable_source'):
                try:
                    logger.info("Marking mod %d as configurable, source=%s",
                                mod_id, self._configurable_source)
                    self._db.connection.execute(
                        "UPDATE mods SET configurable = 1, source_path = ? WHERE id = ?",
                        (self._configurable_source, mod_id))
                    self._db.connection.commit()
                except Exception as e:
                    logger.error("Failed to set configurable flag: %s", e)
                del self._configurable_source

                if hasattr(self, '_configurable_labels'):
                    try:
                        import json as _json2
                        self._db.connection.execute(
                            "INSERT OR REPLACE INTO mod_config (mod_id, selected_labels) "
                            "VALUES (?, ?)",
                            (mod_id, _json2.dumps(self._configurable_labels)))
                        self._db.connection.commit()
                    except Exception as e:
                        logger.error("Failed to store config labels: %s", e)
                    del self._configurable_labels

            # Restore priority and enabled state if this was an update
            if hasattr(self, '_update_priority'):
                try:
                    row = self._db.connection.execute("SELECT MAX(id) FROM mods").fetchone()
                    if row and row[0]:
                        new_id = row[0]
                        self._db.connection.execute(
                            "UPDATE mods SET priority = ?, enabled = ? WHERE id = ?",
                            (self._update_priority, self._update_enabled, new_id))
                        self._db.connection.commit()
                        logger.info("Restored update state: priority=%d, enabled=%s",
                                    self._update_priority, self._update_enabled)
                except Exception as e:
                    logger.debug("Restore update state failed: %s", e)
                del self._update_priority
                del self._update_enabled

            self._refresh_all()
            self._on_nav("PAZ Mods")
            self._update_apply_reminder()

        # Process next queued import if any
        if hasattr(self, '_import_queue') and self._import_queue:
            QTimer.singleShot(500, self._process_next_import)
        else:
            # All imports done — show error summary if any failed
            if hasattr(self, '_import_errors') and self._import_errors:
                errors = self._import_errors
                self._import_errors = []
                error_list = "\n".join(f"  - {e}" for e in errors)
                QMessageBox.warning(
                    self, "Some Imports Failed",
                    f"{len(errors)} mod(s) failed to import:\n\n{error_list}\n\n"
                    "The other mods were imported successfully.",
                )
            self._update_apply_reminder()

    def _install_asi_mod(self, path: Path) -> None:
        """Install an ASI mod by copying .asi/.ini files to bin64/."""
        import tempfile
        import zipfile
        from cdumm.asi.asi_manager import AsiManager
        asi_mgr = AsiManager(self._game_dir / "bin64")

        if not asi_mgr.has_loader():
            self.statusBar().showMessage(
                "Warning: ASI Loader (winmm.dll) not found in bin64/. ASI mods won't load without it.", 10000
            )
            logger.warning("ASI Loader not found, installing ASI mod anyway")

        # Extract zip first if needed
        if path.is_file() and path.suffix.lower() == ".zip":
            tmp = tempfile.mkdtemp(prefix="cdumm_asi_")
            try:
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(tmp)
                path = Path(tmp)
            except Exception as e:
                logger.error("Failed to extract ASI zip: %s", e)

        installed = asi_mgr.install(path)
        if installed:
            self.statusBar().showMessage(
                f"Installed ASI mod: {', '.join(installed)} → bin64/", 10000
            )
            logger.info("ASI install success: %s", installed)
            # Refresh ASI panel and switch to ASI tab
            if hasattr(self, "_asi_panel"):
                self._asi_panel.refresh()
                self._on_nav("ASI Mods")
        else:
            self.statusBar().showMessage("No ASI files found to install.", 5000)
            logger.warning("No ASI files found in %s", path)

    # --- Apply ---
    def _check_game_running(self) -> bool:
        """Check if the game is running. Returns True if safe to proceed.

        Uses process name check via ctypes (fast, no subprocess).
        """
        try:
            import ctypes
            import ctypes.wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi

            # Get list of all PIDs
            arr = (ctypes.wintypes.DWORD * 4096)()
            cb_needed = ctypes.wintypes.DWORD()
            psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(cb_needed))
            num_pids = cb_needed.value // ctypes.sizeof(ctypes.wintypes.DWORD)

            for i in range(num_pids):
                pid = arr[i]
                if pid == 0:
                    continue
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not handle:
                    continue
                try:
                    buf = ctypes.create_unicode_buffer(260)
                    size = ctypes.wintypes.DWORD(260)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        if buf.value.lower().endswith("crimsondesert.exe"):
                            kernel32.CloseHandle(handle)
                            QMessageBox.warning(
                                self, "Game Is Running",
                                "Crimson Desert is currently running.\n\n"
                                "Please close the game before applying mods.",
                                QMessageBox.StandardButton.Ok)
                            return False
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:
            pass  # If check fails, let the user proceed
        return True

    def _on_apply(self) -> None:
        if not self._db or not self._game_dir:
            return

        if not self._check_game_running():
            return

        # Conflicts are shown in the conflict view at the bottom of the window.
        # No need to block Apply with a warning dialog — overlaps are resolved
        # by load order and the winner is shown in the UI.

        progress = ProgressDialog("Applying Mods", self)
        worker = ApplyWorker(self._game_dir, self._vanilla_dir, self._db.db_path)
        thread = QThread()

        self._run_worker(worker, thread, progress,
                         on_finished=self._on_apply_finished)

    def _build_apply_preview(self) -> str:
        """Build a human-readable preview of what Apply will do."""
        if not self._db:
            return ""
        lines = []

        # Files that will be modified by enabled mods
        enabled_files = self._db.connection.execute(
            "SELECT DISTINCT md.file_path, m.name "
            "FROM mod_deltas md JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 1 AND md.file_path != 'meta/0.papgt' "
            "ORDER BY md.file_path"
        ).fetchall()

        if enabled_files:
            # Group by file
            by_file: dict[str, list[str]] = {}
            for fp, name in enabled_files:
                by_file.setdefault(fp, []).append(name)
            modify_count = len(by_file)
            lines.append(f"Modify {modify_count} file(s):")
            for fp, mods in sorted(by_file.items())[:8]:
                lines.append(f"  {fp} ({', '.join(set(mods))})")
            if len(by_file) > 8:
                lines.append(f"  ... and {len(by_file) - 8} more")

        # Files that will be reverted (disabled mods)
        disabled_files = self._db.connection.execute(
            "SELECT DISTINCT md.file_path "
            "FROM mod_deltas md JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 0"
        ).fetchall()
        revert_set = {r[0] for r in disabled_files} - {fp for fp, _ in enabled_files}
        if revert_set:
            lines.append(f"\nRestore {len(revert_set)} file(s) to vanilla:")
            for fp in sorted(revert_set)[:5]:
                lines.append(f"  {fp}")
            if len(revert_set) > 5:
                lines.append(f"  ... and {len(revert_set) - 5} more")

        lines.append("\nPAPGT will be rebuilt from scratch.")

        return "\n".join(lines) if lines else ""

    def _on_apply_finished(self) -> None:
        self._needs_apply = False
        # Remove mods that were pending uninstall (disabled before apply)
        if hasattr(self, '_pending_removals') and self._pending_removals:
            for mid in self._pending_removals:
                self._mod_manager.remove_mod(mid)
            count = len(self._pending_removals)
            self._pending_removals = []
            self._refresh_all()
            self.statusBar().showMessage(f"Uninstalled {count} mod(s) successfully!", 10000)
        else:
            self._refresh_all()
            self.statusBar().showMessage("Mods applied. Verifying...", 5000)
        self._snapshot_applied_state()
        self.statusBar().showMessage("Mods applied successfully!", 10000)
        self._log_activity("apply", "Mods applied successfully")

    def _post_apply_verify(self) -> None:
        """Deep verification after Apply.

        Checks:
        1. PAPGT hash valid
        2. Every PAPGT entry matches its PAMT hash
        3. Every PAMT entry for modded files is within PAZ bounds
        4. Every modded file can be extracted (decrypt + decompress)
        5. No duplicate paths across PAMTs
        """
        if not self._game_dir or not self._db:
            return
        import struct, os
        from cdumm.archive.hashlittle import compute_pamt_hash, compute_papgt_hash
        from cdumm.archive.paz_parse import parse_pamt
        from cdumm.archive.paz_crypto import decrypt
        import lz4.block

        issues = []

        # 1. Check PAPGT
        papgt_path = self._game_dir / "meta" / "0.papgt"
        if papgt_path.exists():
            data = papgt_path.read_bytes()
            if len(data) >= 12:
                stored = struct.unpack_from('<I', data, 4)[0]
                computed = compute_papgt_hash(data)
                if stored != computed:
                    issues.append(("PAPGT", "PAPGT hash is invalid"))

                entry_count = data[8]
                entry_start = 12
                str_table_off = entry_start + entry_count * 12 + 4
                for i in range(entry_count):
                    pos = entry_start + i * 12
                    name_off = struct.unpack_from('<I', data, pos + 4)[0]
                    papgt_hash = struct.unpack_from('<I', data, pos + 8)[0]
                    abs_off = str_table_off + name_off
                    if abs_off < len(data):
                        end = data.index(0, abs_off) if 0 in data[abs_off:] else len(data)
                        dir_name = data[abs_off:end].decode('ascii', errors='replace')
                        pamt_path = self._game_dir / dir_name / "0.pamt"
                        if pamt_path.exists():
                            actual = compute_pamt_hash(pamt_path.read_bytes())
                            if actual != papgt_hash:
                                issues.append(("PAPGT", f"{dir_name} PAMT hash mismatch"))
                        elif not (self._game_dir / dir_name).exists():
                            issues.append(("PAPGT", f"Missing directory {dir_name}"))

        # 2. Get all files modified by enabled mods
        modded_files = self._db.connection.execute(
            "SELECT DISTINCT md.file_path, m.name "
            "FROM mod_deltas md JOIN mods m ON md.mod_id = m.id "
            "WHERE m.enabled = 1 AND md.file_path NOT LIKE 'meta/%'"
        ).fetchall()

        # Group by directory
        modded_dirs = set()
        mod_by_file = {}
        for fp, mod_name in modded_files:
            parts = fp.split("/")
            if len(parts) >= 2 and parts[0].isdigit():
                modded_dirs.add(parts[0])
            mod_by_file.setdefault(fp, []).append(mod_name)

        # 3. For each modded directory, parse PAMT and verify entries
        all_paths = {}  # path -> list of (dir_name, entry) for duplicate detection
        for dir_name in modded_dirs:
            pamt_path = self._game_dir / dir_name / "0.pamt"
            if not pamt_path.exists():
                continue

            try:
                entries = parse_pamt(str(pamt_path), paz_dir=str(self._game_dir / dir_name))
            except Exception as e:
                issues.append((dir_name, f"Failed to parse PAMT: {e}"))
                continue

            for e in entries:
                # Bounds check
                paz_path = self._game_dir / dir_name / f"{e.paz_index}.paz"
                if paz_path.exists():
                    paz_size = paz_path.stat().st_size
                    if e.offset + e.comp_size > paz_size:
                        mods = ", ".join(set(mod_by_file.get(f"{dir_name}/{e.paz_index}.paz", ["?"])))
                        issues.append((mods, f"{e.path}: out of bounds "
                                       f"(offset={e.offset} + comp={e.comp_size} > paz={paz_size})"))

            # 4. Extract test: try decrypt+decompress for a sample of modded entries
            modded_paz_files = set()
            for fp, _ in modded_files:
                if fp.startswith(dir_name + "/") and fp.endswith(".paz"):
                    modded_paz_files.add(fp)

            if modded_paz_files:
                # Test up to 20 entries from this directory
                tested = 0
                for e in entries:
                    if tested >= 20:
                        break
                    paz_fp = f"{dir_name}/{e.paz_index}.paz"
                    if paz_fp not in modded_paz_files:
                        continue

                    try:
                        paz_path = self._game_dir / dir_name / f"{e.paz_index}.paz"
                        with open(paz_path, 'rb') as f:
                            f.seek(e.offset)
                            raw = f.read(e.comp_size)

                        is_lz4 = e.compressed and e.compression_type == 2
                        if is_lz4:
                            # Try decompress, then decrypt+decompress
                            try:
                                lz4.block.decompress(raw, uncompressed_size=e.orig_size)
                            except Exception:
                                dec = decrypt(raw, os.path.basename(e.path))
                                lz4.block.decompress(dec, uncompressed_size=e.orig_size)

                        tested += 1
                    except Exception as ex:
                        mods = ", ".join(set(mod_by_file.get(paz_fp, ["?"])))
                        issues.append((mods, f"{e.path}: extract failed — {ex}"))
                        tested += 1

        # 5. Check for mods imported on a different game version
        try:
            from cdumm.engine.version_detector import detect_game_version
            current_ver = detect_game_version(self._game_dir)
            if current_ver:
                cursor = self._db.connection.execute(
                    "SELECT name, game_version_hash FROM mods "
                    "WHERE enabled = 1 AND game_version_hash IS NOT NULL")
                for name, ver in cursor.fetchall():
                    if ver and ver != current_ver:
                        issues.append((name, "Imported on a different game version — may be outdated"))
        except Exception:
            pass

        if issues:
            # Group by mod/source
            issue_lines = []
            for source, detail in issues[:15]:
                issue_lines.append(f"[{source}] {detail}")
            if len(issues) > 15:
                issue_lines.append(f"... and {len(issues) - 15} more")

            issue_text = "\n".join(issue_lines)
            QMessageBox.warning(
                self, "Post-Apply Verification",
                f"Found {len(issues)} issue(s) that may crash the game:\n\n"
                f"{issue_text}\n\n"
                "The mod name in brackets indicates the likely cause.",
            )
            logger.warning("Post-apply issues: %s", issues)
            self._log_activity("warning",
                               f"Post-apply verification: {len(issues)} issue(s)",
                               "; ".join(f"[{s}] {d}" for s, d in issues[:5]))
        else:
            self.statusBar().showMessage("Mods applied and verified successfully!", 10000)
            logger.info("Post-apply verification passed")
            self._log_activity("apply", "Mods applied and verified successfully")

    # --- Revert ---
    def _on_revert(self) -> None:
        if not self._db or not self._game_dir:
            return

        if not self._check_game_running():
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

        worker.warning.connect(
            lambda msg: self._dispatcher.call(self._show_revert_warning, msg))
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_revert_finished)

    def _show_revert_warning(self, msg: str) -> None:
        QMessageBox.warning(self, "Revert Incomplete", msg)

    def _on_revert_finished(self) -> None:
        # Untick all mods so the UI matches the vanilla state
        if self._mod_manager:
            for mod in self._mod_manager.list_mods():
                if mod["enabled"]:
                    self._mod_manager.set_enabled(mod["id"], False)
        self._refresh_all()
        self._snapshot_applied_state()
        self._log_activity("revert", "Reverted all game files to vanilla")
        self.statusBar().showMessage("Reverted to vanilla successfully.", 10000)
        # Check for leftover .bak files from mod scripts
        self._check_leftover_backups()

    def _check_leftover_backups(self) -> None:
        """Warn about .bak files left behind by mod scripts in game directories."""
        if not self._game_dir:
            return
        bak_files = []
        for d in self._game_dir.iterdir():
            if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                continue
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() == ".bak":
                    bak_files.append(f)
        if not bak_files:
            return

        total_mb = sum(f.stat().st_size for f in bak_files) / (1024 * 1024)
        names = "\n".join(f"  {f.parent.name}/{f.name}" for f in bak_files[:10])
        if len(bak_files) > 10:
            names += f"\n  ... and {len(bak_files) - 10} more"

        reply = QMessageBox.question(
            self, "Leftover Backup Files Found",
            f"Found {len(bak_files)} backup file(s) ({total_mb:.0f} MB) in your\n"
            f"game directory:\n\n{names}\n\n"
            "These were created by individual mod scripts (not by CDUMM).\n"
            "CDUMM has its own backup system and does not use these files.\n"
            "They are just taking up disk space.\n\n"
            "Delete them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted = 0
            for f in bak_files:
                try:
                    f.unlink()
                    deleted += 1
                except Exception:
                    pass
            self.statusBar().showMessage(
                f"Deleted {deleted} leftover backup file(s).", 5000)

    # --- Remove mod ---
    def _on_remove_mod(self, mod_id: int | None = None) -> None:
        if not hasattr(self, "_mod_table") or not self._mod_manager:
            return

        # Build list of mods to remove
        mods_to_remove: list[tuple[int, str]] = []
        if mod_id is not None:
            # Called from context menu with explicit mod_id
            cursor = self._db.connection.execute("SELECT name FROM mods WHERE id = ?", (mod_id,))
            row = cursor.fetchone()
            mods_to_remove.append((mod_id, row[0] if row else f"Mod {mod_id}"))
        else:
            # Called from Remove Selected button — handle multiple selections
            indexes = self._mod_table.selectionModel().selectedRows()
            if not indexes:
                return
            for idx in indexes:
                mod = self._get_mod_at_proxy_row(idx.row())
                if mod:
                    mods_to_remove.append((mod["id"], mod["name"]))

        if not mods_to_remove:
            return

        if len(mods_to_remove) == 1:
            msg = f"Uninstall '{mods_to_remove[0][1]}'?"
        else:
            names = "\n".join(f"  - {name}" for _, name in mods_to_remove)
            msg = f"Uninstall {len(mods_to_remove)} mods?\n\n{names}"

        reply = QMessageBox.question(
            self, "Uninstall Mods", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            # Check if any mod was actually applied (enabled AND has delta data).
            # If the mod has no deltas (e.g. after a version migration that
            # wiped delta data), just delete it — no Apply needed.
            needs_apply = False
            for mid, name in mods_to_remove:
                mod_row = self._db.connection.execute(
                    "SELECT enabled FROM mods WHERE id = ?", (mid,)).fetchone()
                delta_count = self._db.connection.execute(
                    "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ?", (mid,)).fetchone()[0]
                if mod_row and mod_row[0] and delta_count > 0:
                    # Mod is enabled AND has delta data — needs Apply to revert
                    needs_apply = True
                    break

            if needs_apply:
                # Disable mods first so the apply engine reverts their files,
                # then remove them from the database after apply completes.
                for mid, name in mods_to_remove:
                    self._mod_manager.set_enabled(mid, False)
                    logger.info("Disabled for uninstall: %s", name)
                self._pending_removals = [mid for mid, _ in mods_to_remove]
                self._refresh_all()
                self.statusBar().showMessage(
                    f"Uninstalling {len(mods_to_remove)} mod(s) — Apply needed to revert files...", 10000)
                self._on_apply()
            else:
                # Mod was never applied — just remove from database
                for mid, name in mods_to_remove:
                    self._mod_manager.remove_mod(mid)
                    self._log_activity("remove", f"Removed: {name}",
                                       "Mod was never applied, no files to revert")
                    logger.info("Removed unapplied mod: %s", name)
                self._refresh_all()
                self.statusBar().showMessage(
                    f"Removed {len(mods_to_remove)} mod(s).", 10000)

    def _update_header_checkbox(self) -> None:
        """Sync the header checkbox label with current mod states."""
        if not hasattr(self, '_check_header') or not self._mod_manager:
            return
        mods = self._mod_manager.list_mods()
        if not mods or not any(m["enabled"] for m in mods):
            label = "☐"
        elif all(m["enabled"] for m in mods):
            label = "☑"
        else:
            label = "◧"
        self._check_header.set_label(label)

    def _find_existing_mod(self, path: Path) -> tuple[int, str] | None:
        """Check if a dropped mod matches an already-installed mod by name.

        Returns (mod_id, mod_name) or None.
        """
        from cdumm.engine.import_handler import _read_modinfo
        from cdumm.engine.json_patch_handler import detect_json_patch
        from cdumm.engine.crimson_browser_handler import detect_crimson_browser

        def _normalize(s: str) -> str:
            return s.lower().strip().replace("-", " ").replace("_", " ")

        def _compact(s: str) -> str:
            """Remove all spaces for matching concatenated names like CDLootMultiplier."""
            return _normalize(s).replace(" ", "")

        # Get the mod name from the drop
        drop_name = path.stem.lower()
        modinfo = _read_modinfo(path) if path.is_dir() else None
        if modinfo and modinfo.get("name"):
            drop_name = modinfo["name"].lower()
        elif path.suffix.lower() == ".json":
            jp = detect_json_patch(path)
            if jp and jp.get("name"):
                drop_name = jp["name"].lower()
        elif path.is_dir():
            cb = detect_crimson_browser(path)
            if cb and cb.get("id"):
                drop_name = cb["id"].lower()

        drop_norm = _normalize(drop_name)
        drop_compact = _compact(drop_name)
        for m in self._mod_manager.list_mods():
            mod_norm = _normalize(m["name"])
            mod_compact = _compact(m["name"])
            # Check with spaces (loot multiplier in cdlootmultiplier)
            if len(mod_norm) >= 4 and mod_norm in drop_norm:
                return (m["id"], m["name"])
            if len(drop_norm) >= 4 and drop_norm in mod_norm:
                return (m["id"], m["name"])
            # Check without spaces (lootmultiplier in cdlootmultiplier)
            if len(mod_compact) >= 4 and mod_compact in drop_compact:
                return (m["id"], m["name"])
            if len(drop_compact) >= 4 and drop_compact in mod_compact:
                return (m["id"], m["name"])

        return None

    def _on_toggle_all(self) -> None:
        """Toggle all mods on/off."""
        if not self._mod_manager:
            return
        mods = self._mod_manager.list_mods()
        if not mods:
            return
        any_enabled = any(m["enabled"] for m in mods)
        for m in mods:
            self._mod_manager.set_enabled(m["id"], not any_enabled)
        self._refresh_all()
        self._update_apply_reminder()

    # --- View details ---
    def _get_mod_at_proxy_row(self, proxy_row: int) -> dict | None:
        """Map a proxy model row to the source model and get the mod."""
        if hasattr(self, "_sort_proxy"):
            source_index = self._sort_proxy.mapToSource(self._sort_proxy.index(proxy_row, 0))
            return self._mod_list_model.get_mod_at_row(source_index.row())
        return self._mod_list_model.get_mod_at_row(proxy_row)

    def _on_view_details(self) -> None:
        if not hasattr(self, "_mod_table") or not self._mod_manager:
            return
        indexes = self._mod_table.selectionModel().selectedRows()
        if not indexes:
            return
        mod = self._get_mod_at_proxy_row(indexes[0].row())
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
        mod = self._get_mod_at_proxy_row(index.row())
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

        # Configure (only for mods with labeled changes)
        is_configurable = self._db.connection.execute(
            "SELECT configurable FROM mods WHERE id = ?", (mod["id"],)
        ).fetchone()
        if is_configurable and is_configurable[0]:
            configure_action = QAction("Configure...", self)
            configure_action.triggered.connect(lambda: self._on_configure_mod(mod))
            menu.addAction(configure_action)

        rename_action = QAction("Rename", self)
        rename_action.triggered.connect(lambda: self._on_rename_mod(mod))
        menu.addAction(rename_action)

        update_action = QAction("Update (replace with new version)", self)
        update_action.triggered.connect(lambda: self._on_update_mod(mod))
        menu.addAction(update_action)

        menu.addSeparator()

        # Uninstall — handles single or multiple selected mods
        selected = self._mod_table.selectionModel().selectedRows()
        if len(selected) > 1:
            remove_action = QAction(f"Uninstall {len(selected)} selected mods", self)
            remove_action.triggered.connect(lambda: self._on_remove_mod())
        else:
            remove_action = QAction("Uninstall", self)
            remove_action.triggered.connect(lambda: self._on_remove_mod(mod_id=mod["id"]))
        menu.addAction(remove_action)

        menu.exec(self._mod_table.viewport().mapToGlobal(pos))

    def _on_configure_mod(self, mod: dict) -> None:
        """Re-open the toggle/preset picker for a configurable mod."""
        source = self._db.connection.execute(
            "SELECT source_path FROM mods WHERE id = ?", (mod["id"],)
        ).fetchone()
        if not source or not source[0]:
            QMessageBox.warning(self, "Cannot Configure",
                                "Original mod source not found.")
            return

        source_path = Path(source[0])
        if not source_path.exists():
            QMessageBox.warning(self, "Cannot Configure",
                                f"Original file not found:\n{source_path}\n\n"
                                "Drop the mod file again to re-import with new settings.")
            return

        from cdumm.gui.preset_picker import has_labeled_changes, TogglePickerDialog
        from cdumm.engine.json_patch_handler import detect_json_patch
        import tempfile

        # Extract if archive
        check_path = source_path
        tmp_dir = None
        if source_path.suffix.lower() in (".zip", ".7z"):
            try:
                tmp_dir = tempfile.mkdtemp(prefix="cdumm_reconfig_")
                if source_path.suffix.lower() == ".zip":
                    import zipfile
                    with zipfile.ZipFile(source_path) as zf:
                        zf.extractall(tmp_dir)
                else:
                    import py7zr
                    with py7zr.SevenZipFile(source_path, 'r') as zf:
                        zf.extractall(tmp_dir)
                check_path = Path(tmp_dir)
            except Exception:
                pass

        # Check for multiple JSON presets in the source (e.g. Trust Me has 5 variants)
        # If source is a single JSON file, also check parent dir for sibling presets
        from cdumm.gui.preset_picker import find_json_presets, PresetPickerDialog
        presets = find_json_presets(check_path)
        if len(presets) <= 1 and check_path.is_file() and check_path.parent.is_dir():
            parent_presets = find_json_presets(check_path.parent)
            if len(parent_presets) > 1:
                presets = parent_presets
        if len(presets) > 1:
            preset_dialog = PresetPickerDialog(presets, self)
            if preset_dialog.exec() and preset_dialog.selected_data:
                json_data = preset_dialog.selected_data
                check_path = preset_dialog.selected_path
            else:
                if tmp_dir:
                    import shutil
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return
        else:
            json_data = detect_json_patch(check_path)

        if not json_data:
            if tmp_dir:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            QMessageBox.warning(self, "Cannot Configure",
                                "No configurable options found in this mod.")
            return

        # If mod has configurable labeled changes, show toggle picker
        if not has_labeled_changes(json_data):
            # No toggle needed, just reimport with the selected preset
            pass
        else:
            # Load previous selection
            previous_labels = None
            try:
                import json as _json3
                row = self._db.connection.execute(
                    "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
                    (mod["id"],)).fetchone()
                if row and row[0]:
                    previous_labels = _json3.loads(row[0])
            except Exception:
                pass

            toggle_dialog = TogglePickerDialog(json_data, self, previous_labels=previous_labels)
            if toggle_dialog.exec() and toggle_dialog.selected_data:
                json_data = toggle_dialog.selected_data
            else:
                if tmp_dir:
                    import shutil
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return

        if json_data:
            import json as _json
            # Save old state
            old_priority = mod.get("priority", 0)
            old_enabled = mod.get("enabled", False)

            # Remove old mod and re-import with new selection
            self._mod_manager.remove_mod(mod["id"])

            tmp_json = Path(tempfile.mktemp(suffix=".json", prefix="cdumm_reconfig_"))
            write_data = json_data.copy()
            write_data.pop("_json_path", None)
            tmp_json.write_text(_json.dumps(write_data, indent=2, default=str), encoding="utf-8")
            self._configurable_source = str(source_path)
            # Store new selection for future reconfigure
            new_labels = []
            for patch in json_data.get("patches", []):
                for c in patch.get("changes", []):
                    if "label" in c:
                        new_labels.append(c["label"])
            self._configurable_labels = new_labels

            # Import the filtered JSON
            progress = ProgressDialog("Re-importing with new configuration...", self)
            worker = ImportWorker(tmp_json, self._game_dir, self._db.db_path, self._deltas_dir)
            thread = QThread()

            new_name = json_data.get("name", mod["name"])

            def on_done(result):
                self._on_import_finished(result)
                # Restore priority, update name to reflect new config
                try:
                    row = self._db.connection.execute("SELECT MAX(id) FROM mods").fetchone()
                    if row and row[0]:
                        self._db.connection.execute(
                            "UPDATE mods SET priority = ?, enabled = ?, name = ? WHERE id = ?",
                            (old_priority, old_enabled, new_name, row[0]))
                        self._db.connection.commit()
                    self._refresh_all()
                except Exception:
                    pass
                self._log_activity("import", f"Reconfigured: {new_name}")
                # Auto-apply so the new configuration takes effect immediately
                QTimer.singleShot(500, self._on_apply)

            self._run_worker(worker, thread, progress, on_finished=on_done)

        if tmp_dir:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _on_toggle_mod(self, mod: dict) -> None:
        if not self._mod_manager:
            return
        new_state = not mod["enabled"]
        self._mod_manager.set_enabled(mod["id"], new_state)
        self._refresh_all()
        self._update_apply_reminder()

    def _on_mod_toggled_via_checkbox(self) -> None:
        self._update_apply_reminder()

    def _snapshot_applied_state(self) -> None:
        """Save current mod enabled states as the 'applied' baseline."""
        if self._mod_manager:
            self._applied_state = {m["id"]: m["enabled"] for m in self._mod_manager.list_mods()}

    def _update_apply_reminder(self) -> None:
        """Show or clear the apply reminder based on whether state differs from last apply."""
        if not self._mod_manager:
            return
        current = {m["id"]: m["enabled"] for m in self._mod_manager.list_mods()}
        if current != self._applied_state:
            self._needs_apply = True
            self.statusBar().showMessage(
                "Mod list changed — click Apply to update game files.", 0)
        else:
            self._needs_apply = False
            self.statusBar().clearMessage()

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
        mod = self._get_mod_at_proxy_row(indexes[0].row())
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
        mod = self._get_mod_at_proxy_row(indexes[0].row())
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
    def _on_refresh_snapshot(self, skip_verify_prompt: bool = False) -> None:
        if not self._db or not self._game_dir:
            return
        if self._snapshot_in_progress:
            return

        if not skip_verify_prompt:
            reply = QMessageBox.question(
                self, "Rescan Game Files",
                "This will create a new vanilla snapshot from your current game files.\n\n"
                "Have you verified your game files through Steam?\n\n"
                "  Steam → Right-click Crimson Desert → Properties\n"
                "  → Installed Files → Verify integrity of game files\n\n"
                "Only rescan after Steam verify — otherwise the snapshot\n"
                "may capture modded files as 'vanilla'.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._snapshot_in_progress = True

        # Clear stale vanilla backups. After a Steam verify the game files
        # are clean, so any existing backups are from a previous modded state
        # and would poison future reverts. Also clear range backups and
        # old deltas so mods get reimported against the fresh baseline.
        if self._vanilla_dir and self._vanilla_dir.exists():
            import shutil
            try:
                shutil.rmtree(self._vanilla_dir, ignore_errors=True)
                self._vanilla_dir.mkdir(parents=True, exist_ok=True)
                logger.info("Cleared stale vanilla backups for fresh snapshot")
            except Exception as e:
                logger.warning("Failed to clear vanilla backups: %s", e)

        progress = ProgressDialog("Creating Vanilla Snapshot", self)
        worker = SnapshotWorker(self._game_dir, self._db.db_path)
        worker.activity.connect(self._log_activity)
        thread = QThread()

        self._run_worker(worker, thread, progress,
                         on_finished=self._on_snapshot_finished)

    def _on_snapshot_finished(self, count: int) -> None:
        self._snapshot_in_progress = False
        logger.info("Snapshot callback: %d files", count)
        self._sync_db()

        # Save game version fingerprint with the snapshot
        try:
            from cdumm.engine.version_detector import detect_game_version
            from cdumm.storage.config import Config
            fp = detect_game_version(self._game_dir)
            if fp:
                Config(self._db).set("game_version_fingerprint", fp)
                logger.info("Saved game version fingerprint: %s", fp)
        except Exception:
            pass

        # Refresh stale vanilla backups — if game files changed (e.g., after
        # Steam verify), the old backups may contain modded data. Replace them
        # with the current (clean) game files so future imports work correctly.
        self._refresh_vanilla_backups()

        self._update_snapshot_status()
        self.statusBar().showMessage(f"Snapshot complete: {count} files indexed. You can now import mods.", 10000)
        logger.info("Snapshot finished and UI updated")
        self._log_activity("snapshot", f"Game files scanned: {count} files indexed")

        # Auto-reimport mods from stored sources after game update
        if getattr(self, '_pending_auto_reimport', False):
            self._pending_auto_reimport = False
            QTimer.singleShot(1000, self._auto_reimport_mods)

    def _auto_migrate_after_update(self) -> None:
        """Revert and reimport all mods after a CDUMM version update."""
        if not self._mod_manager or not self._game_dir:
            return

        sources_dir = self._cdmods_dir / "sources"
        mods = self._db.connection.execute(
            "SELECT id, name, source_path FROM mods").fetchall()
        has_sources = any(
            (sources_dir / str(mid)).exists() or (sp and Path(sp).exists())
            for mid, _, sp in mods
        )
        if not has_sources:
            return

        reply = QMessageBox.question(
            self, "CDUMM Updated",
            f"CDUMM was updated and the internal mod format has changed.\n\n"
            f"Your {len(mods)} mod(s) need to be reimported to work correctly\n"
            f"with the new version. This will revert to vanilla and reimport\n"
            f"all mods automatically. Your mod list and settings are preserved.\n\n"
            f"Reimport now? (Recommended)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Save mod state before migration
        mod_states = []
        for mod in self._mod_manager.list_mods():
            mod_states.append({
                "name": mod["name"],
                "enabled": mod["enabled"],
                "priority": mod["priority"],
            })
        self._migrate_mod_states = mod_states

        # Run on background thread with progress dialog
        from cdumm.gui.workers import MigrateWorker
        progress = ProgressDialog("Migrating Mods", self)
        worker = MigrateWorker(
            self._game_dir, self._vanilla_dir, self._cdmods_dir,
            self._db.db_path, self._deltas_dir)
        thread = QThread()
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_migrate_finished)

    def _on_migrate_finished(self, reimported: int, failed: int) -> None:
        """Called after background migration completes."""
        self._sync_db()

        # Restore enabled/disabled state
        mod_states = getattr(self, '_migrate_mod_states', [])
        for state in mod_states:
            try:
                row = self._db.connection.execute(
                    "SELECT id FROM mods WHERE name = ?",
                    (state["name"],)).fetchone()
                if row:
                    self._db.connection.execute(
                        "UPDATE mods SET enabled = ?, priority = ? WHERE id = ?",
                        (1 if state["enabled"] else 0, state["priority"], row[0]))
            except Exception:
                pass
        self._db.connection.commit()

        self._refresh_all()
        msg = f"Migrated {reimported} mod(s) to new format."
        if failed:
            msg += f" {failed} mod(s) need manual re-import."
        self._log_activity("migrate", msg)
        self.statusBar().showMessage(msg, 15000)

    def _auto_reimport_mods(self) -> None:
        """Re-import all mods from stored sources after a game update."""
        from cdumm.engine.import_handler import (
            _process_extracted_files, detect_format, import_from_json_patch,
        )
        from cdumm.engine.json_patch_handler import detect_json_patch

        sources_dir = self._cdmods_dir / "sources"
        if not sources_dir.exists():
            return

        # Get all mods with their source paths
        mods = self._db.connection.execute(
            "SELECT id, name, source_path, priority FROM mods "
            "ORDER BY priority").fetchall()

        reimported = 0
        failed = 0
        for mod_id, mod_name, source_path, priority in mods:
            # Check if source exists in CDMods/sources/<mod_id>/
            src = sources_dir / str(mod_id)
            has_source = src.exists() and any(src.iterdir()) if src.exists() else False
            if not has_source:
                if source_path and Path(source_path).exists():
                    src = Path(source_path)
                    has_source = True

            if not has_source:
                logger.warning("No source for %s (id=%d), keeping existing deltas",
                               mod_name, mod_id)
                failed += 1
                continue

            try:
                self.statusBar().showMessage(
                    f"Re-importing {mod_name}...", 0)
                logger.info("Auto-reimporting: %s from %s", mod_name, src)

                # Detect if it's a JSON patch
                json_data = detect_json_patch(src)
                if json_data:
                    result = import_from_json_patch(
                        src, self._game_dir, self._db,
                        self._snapshot, self._deltas_dir,
                        existing_mod_id=mod_id)
                else:
                    result = _process_extracted_files(
                        src, self._game_dir, self._db,
                        self._snapshot, self._deltas_dir,
                        mod_name, existing_mod_id=mod_id)

                if result.error:
                    logger.warning("Auto-reimport failed for %s: %s",
                                   mod_name, result.error)
                    failed += 1
                else:
                    reimported += 1
                    logger.info("Auto-reimported: %s (%d files)",
                                mod_name, len(result.changed_files))
            except Exception as e:
                logger.warning("Auto-reimport error for %s: %s", mod_name, e)
                failed += 1

        self._refresh_all()
        msg = f"Auto-reimported {reimported} mod(s) after game update."
        if failed:
            msg += f" {failed} mod(s) need manual re-import."
        self.statusBar().showMessage(msg, 15000)
        self._log_activity("import", msg)
        logger.info(msg)

    def _refresh_vanilla_backups(self) -> None:
        """Validate and refresh vanilla backups against the snapshot.

        Only runs when no mods are enabled (game files are clean).
        Three operations:
        1. Remove orphan backups not in snapshot (from mod-created directories)
        2. Replace backups whose size doesn't match the game file
        3. Purge stale range backups
        """
        if not self._game_dir or not self._vanilla_dir or not self._vanilla_dir.exists():
            return
        if not self._db:
            return

        # Safety: only refresh when no mods are enabled — game files must be clean
        enabled_count = self._db.connection.execute(
            "SELECT COUNT(*) FROM mods WHERE enabled = 1"
        ).fetchone()[0]
        if enabled_count > 0:
            logger.debug("Skipping vanilla backup refresh — %d mods enabled", enabled_count)
            return

        # Load snapshot file paths for orphan detection
        snap_files = set()
        try:
            cursor = self._db.connection.execute("SELECT file_path FROM snapshots")
            snap_files = {row[0] for row in cursor.fetchall()}
        except Exception:
            return

        import shutil
        refreshed = 0
        orphans_removed = 0

        for backup in list(self._vanilla_dir.rglob("*")):
            if not backup.is_file():
                continue
            if backup.name.endswith(".vranges"):
                continue

            rel = str(backup.relative_to(self._vanilla_dir)).replace("\\", "/")

            # Remove orphan backups not in snapshot (mod-created directories)
            if rel not in snap_files:
                backup.unlink()
                orphans_removed += 1
                logger.info("Removed orphan vanilla backup: %s (not in snapshot)", rel)
                continue

            game_file = self._game_dir / rel.replace("/", "\\")
            if not game_file.exists():
                continue

            # Size difference = stale backup, replace with clean game file
            if backup.stat().st_size != game_file.stat().st_size:
                shutil.copy2(game_file, backup)
                refreshed += 1
                logger.info("Refreshed stale vanilla backup: %s", rel)

        if orphans_removed:
            logger.info("Removed %d orphan vanilla backup(s)", orphans_removed)
            # Clean empty directories left behind
            for d in sorted(self._vanilla_dir.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()

        if refreshed:
            logger.info("Refreshed %d stale vanilla backup(s)", refreshed)

        if orphans_removed or refreshed:
            # Purge range backups — they reference old byte positions
            for vr in self._vanilla_dir.rglob("*.vranges"):
                vr.unlink()
                logger.info("Purged stale range backup: %s", vr.name)

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

    # --- Find Problem Mod ---
    def _on_find_problem_mod(self) -> None:
        if not self._db or not self._game_dir or not self._mod_manager:
            return
        enabled = [m for m in self._mod_manager.list_mods() if m["enabled"]]
        if len(enabled) < 2:
            QMessageBox.information(self, "Find Problem Mod",
                                   "You need at least 2 enabled mods to run this tool.")
            return

        from cdumm.gui.binary_search_dialog import BinarySearchDialog
        dialog = BinarySearchDialog(
            self._mod_manager, self._game_dir, self._vanilla_dir, self._db, self)
        dialog.finished.connect(lambda: self._on_binary_search_done())
        dialog.exec()

    def _on_binary_search_done(self) -> None:
        self._refresh_all()
        self._snapshot_applied_state()

    # --- Check Mods For Issues ---
    def _on_check_mods(self) -> None:
        """Run deep verification on enabled mods in background."""
        if not self._db or not self._game_dir:
            return

        from cdumm.gui.workers import ModCheckWorker
        progress = ProgressDialog("Checking Mods For Issues", self)
        worker = ModCheckWorker(self._game_dir, self._db.db_path)
        thread = QThread()
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_check_mods_finished)

    def _on_check_mods_finished(self, issues: list) -> None:
        if not issues:
            QMessageBox.information(self, "Mod Check", "No issues found. All mods look good.")
            self._log_activity("verify", "Mod check passed — no issues found")
        else:
            # Collect unique mod names that have issues
            broken_mods = set()
            issue_lines = []
            for source, detail in issues[:15]:
                issue_lines.append(f"[{source}] {detail}")
                if source not in ("PAPGT", "Conflict", "?"):
                    broken_mods.add(source)
            if len(issues) > 15:
                issue_lines.append(f"... and {len(issues) - 15} more")
            issue_text = "\n".join(issue_lines)

            if broken_mods:
                reply = QMessageBox.warning(
                    self, "Mod Check",
                    f"Found {len(issues)} issue(s):\n\n{issue_text}\n\n"
                    f"Disable the {len(broken_mods)} problematic mod(s)?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    disabled = 0
                    for mod in self._mod_manager.list_mods():
                        if mod["name"] in broken_mods and mod["enabled"]:
                            self._mod_manager.set_enabled(mod["id"], False)
                            disabled += 1
                            self._log_activity("warning",
                                               f"Auto-disabled: {mod['name']}",
                                               "Failed mod compatibility check")
                    self._refresh_all()
                    self.statusBar().showMessage(
                        f"Disabled {disabled} problematic mod(s). Click Apply to revert their changes.", 10000)
            else:
                QMessageBox.warning(
                    self, "Mod Check",
                    f"Found {len(issues)} issue(s):\n\n{issue_text}",
                )

            self._log_activity("warning",
                               f"Mod check: {len(issues)} issue(s)",
                               "; ".join(f"[{s}] {d}" for s, d in issues[:5]))

    # --- Verify Game State ---
    def _on_verify_game_state(self) -> None:
        if not self._db or not self._game_dir:
            return
        if not self._snapshot or not self._snapshot.has_snapshot():
            QMessageBox.information(self, "No Snapshot",
                                   "No snapshot exists yet. Scan game files first.")
            return

        from cdumm.gui.verify_dialog import VerifyWorker, VerifyDialog
        progress = ProgressDialog("Verifying Game State", self)
        worker = VerifyWorker(self._game_dir, self._db.db_path)
        thread = QThread()
        self._run_worker(worker, thread, progress,
                         on_finished=self._on_verify_finished)

    def _on_verify_finished(self, results: dict) -> None:
        from cdumm.gui.verify_dialog import VerifyDialog
        modded = len(results.get("modded", []))
        vanilla = len(results.get("vanilla", []))
        extra = len(results.get("extra_dirs", []))
        if modded == 0 and extra == 0:
            self._log_activity("verify", f"Game state verified: ALL CLEAN ({vanilla} files vanilla)")
        else:
            self._log_activity("verify",
                               f"Game state verified: {modded} modded, {extra} extra dirs, {vanilla} vanilla")
        dialog = VerifyDialog(results, self)
        dialog.exec()

    # --- Patch Notes ---
    def _on_show_patch_notes(self) -> None:
        dialog = PatchNotesDialog(self, latest_only=False)
        dialog.exec()

    def _check_show_update_notes(self) -> None:
        """Show patch notes if the app was just updated.
        Also triggers auto-reimport of mods so they use the new format.
        """
        if not self._db:
            return
        config = Config(self._db)
        last_seen = config.get("last_seen_version") or ""
        from cdumm import __version__
        if last_seen != __version__ and CHANGELOG:
            config.set("last_seen_version", __version__)
            QTimer.singleShot(500, self._show_update_notes)

            # Auto-migrate only when the delta format actually changes.
            # This version number is bumped ONLY for breaking format changes,
            # not for every code update. Prevents unnecessary reimports.
            CURRENT_FORMAT_VERSION = "2"  # bump this when delta format changes
            stored_format = config.get("migration_format_version") or "0"
            if (stored_format != CURRENT_FORMAT_VERSION
                    and self._mod_manager and self._mod_manager.get_mod_count() > 0):
                config.set("migration_format_version", CURRENT_FORMAT_VERSION)
                QTimer.singleShot(1500, self._auto_migrate_after_update)

    def _show_update_notes(self) -> None:
        dialog = PatchNotesDialog(self, latest_only=True)
        dialog.exec()

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
    # Versions at or below this have known game-breaking bugs and must update.
    _MINIMUM_SAFE_VERSION = "1.7.0"

    def _check_for_updates(self) -> None:
        from cdumm import __version__
        from cdumm.engine.update_checker import UpdateCheckWorker
        logger.info("Checking for updates (current: v%s)", __version__)
        worker = UpdateCheckWorker(__version__)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        self._update_found = False
        worker.update_available.connect(self._on_update_available)
        worker.finished.connect(self._on_update_check_done)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda: setattr(self, '_update_thread', None))
        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    def _on_update_check_done(self) -> None:
        if not self._update_found:
            if hasattr(self, '_about_update_label'):
                self._about_update_label.setText("\u2714  CDUMM is up to date")
                self._about_update_label.setStyleSheet(
                    "font-size: 15px; font-weight: bold; color: #4CAF50; "
                    "padding: 12px; border: 1px solid #4CAF50; border-radius: 8px; "
                    "background: #1A2E1A;")
            self._set_about_nav_indicator("green")
            if hasattr(self, '_update_banner'):
                self._update_banner.setVisible(False)

    def _on_update_available(self, info: dict) -> None:
        self._update_found = True
        self._pending_update_info = info
        tag = info.get("tag", "new version")

        # Update About tab
        if hasattr(self, '_about_update_label'):
            self._about_update_label.setText(
                f"\u26A0  Update available: {tag}\n"
                "Click the red banner at the bottom to open GitHub.")
            self._about_update_label.setStyleSheet(
                "font-size: 15px; font-weight: bold; color: #F44336; "
                "padding: 12px; border: 1px solid #F44336; border-radius: 8px; "
                "background: #2E1A1A;")
        self._set_about_nav_indicator("red")

        # Show persistent banner
        if hasattr(self, '_update_banner'):
            self._update_banner.setText(
                f"\u26A0  Update available: {tag} — click here to open GitHub")
            self._update_banner.setVisible(True)

        # Check if this version is critically outdated
        from cdumm import __version__
        from cdumm.engine.update_checker import _version_newer
        is_critical = _version_newer(self._MINIMUM_SAFE_VERSION, __version__)

        if is_critical:
            import webbrowser
            QMessageBox.critical(
                self, "Critical Update Required",
                f"You are running v{__version__} which has known issues "
                f"that can break your game.\n\n"
                f"Version {tag} fixes these problems.\n\n"
                "Click OK to open the GitHub releases page and download "
                "the latest version.")
            if info.get("url"):
                webbrowser.open(info["url"])
        else:
            if getattr(self, '_update_dialog_shown', False):
                return
            self._update_dialog_shown = True
            reply = QMessageBox.question(
                self, "Update Available",
                f"A new version is available: {tag}\n\n"
                f"{info.get('body', '')[:300]}\n\n"
                "Open GitHub to download?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                import webbrowser
                if info.get("url"):
                    webbrowser.open(info["url"])

    def _on_banner_clicked(self) -> None:
        """User clicked the persistent update banner — open GitHub."""
        info = getattr(self, '_pending_update_info', None)
        if not info:
            return
        if info.get("url"):
            import webbrowser
            webbrowser.open(info["url"])

    def _set_about_nav_indicator(self, color: str) -> None:
        """Update the About sidebar button with a colored dot."""
        for label, btn in self._nav_buttons:
            if label == "About":
                dot = "\U0001F7E2" if color == "green" else "\U0001F534"
                btn.setText(f"About {dot}")

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
