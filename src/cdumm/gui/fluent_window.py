"""CDUMM v3 main window — FluentWindow with sidebar navigation."""
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Property, QEasingCurve, QObject, QPropertyAnimation, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QVBoxLayout, QWidget

from qfluentwidgets import (
    FluentWindow,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    SmoothScrollArea,
    StateToolTip,
    SubtitleLabel,
)
from qfluentwidgets.components.navigation.navigation_widget import NavigationWidget

from cdumm import __version__
from cdumm.engine.conflict_detector import ConflictDetector
from cdumm.engine.mod_manager import ModManager
from cdumm.engine.snapshot_manager import SnapshotManager
from cdumm.gui.components.drop_overlay import DropOverlay
from cdumm.i18n import tr
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


def _is_standalone_paz_mod(path: Path) -> bool:
    """Check if path is a standalone PAZ mod (0.paz + 0.pamt, not in a numbered dir).
    These mods add a new PAZ directory and don't need a vanilla snapshot.
    """
    import zipfile
    if path.is_dir():
        if (path / "0.paz").exists() and (path / "0.pamt").exists():
            return True
        for sub in path.iterdir():
            if sub.is_dir() and (sub / "0.paz").exists() and (sub / "0.pamt").exists():
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


# ---------------------------------------------------------------------------
# Thread-safe callback dispatcher
# ---------------------------------------------------------------------------

class _MainThreadDispatcher(QObject):
    """Routes callbacks from worker threads to the main thread."""
    _dispatch = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dispatch.connect(self._execute)

    @Slot(object, object)
    def _execute(self, func, args):
        func(*args)

    def call(self, func, *args):
        self._dispatch.emit(func, args)


# ---------------------------------------------------------------------------
# Custom logo widget — scales with sidebar compact/expanded state
# ---------------------------------------------------------------------------

class CdummLogoWidget(NavigationWidget):
    """Logo widget that fills the sidebar: large when expanded, icon when compact.
    Pre-caches scaled pixmaps and matches the sidebar's 150ms OutQuad animation.
    """

    _COMPACT_SIZE = 36
    _EXPANDED_HEIGHT = 100

    def __init__(self, logo_path: str, parent=None):
        super().__init__(isSelectable=False, parent=parent)
        self._pixmap = QPixmap(logo_path) if logo_path else QPixmap()
        # Pre-cache scaled versions to avoid per-frame scaling
        self._compact_pm = self._pixmap.scaled(
            32, 32, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation) if not self._pixmap.isNull() else QPixmap()
        self._expanded_pm = QPixmap()  # set in setCompacted when EXPAND_WIDTH is known
        self.setFixedSize(40, self._COMPACT_SIZE)

        # Match sidebar's animation timing
        self._height_anim = QPropertyAnimation(self, b"fixedHeight")
        self._height_anim.setDuration(350)
        self._height_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_fixed_height(self):
        return self.maximumHeight()

    def _set_fixed_height(self, h):
        self.setFixedHeight(int(h))
        self.update()

    fixedHeight = Property(int, _get_fixed_height, _set_fixed_height)

    def setCompacted(self, isCompacted: bool):
        if isCompacted == self.isCompacted:
            return
        self.isCompacted = isCompacted

        # Cache expanded pixmap on first expand (EXPAND_WIDTH is set by then)
        if not isCompacted and self._expanded_pm.isNull() and not self._pixmap.isNull():
            self._expanded_pm = self._pixmap.scaled(
                self.EXPAND_WIDTH - 16, self._EXPANDED_HEIGHT - 12,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)

        target_h = self._COMPACT_SIZE if isCompacted else self._EXPANDED_HEIGHT
        if isCompacted:
            self.setFixedWidth(40)
        else:
            self.setFixedWidth(self.EXPAND_WIDTH)

        self._height_anim.stop()
        self._height_anim.setStartValue(self.height())
        self._height_anim.setEndValue(target_h)
        self._height_anim.start()

    def paintEvent(self, e):
        if self._pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHints(QPainter.SmoothPixmapTransform | QPainter.Antialiasing)

        # Use pre-cached pixmap, just center it
        pm = self._compact_pm if self.isCompacted else self._expanded_pm
        if pm.isNull():
            pm = self._compact_pm
        x = (self.width() - pm.width()) // 2
        y = (self.height() - pm.height()) // 2
        painter.drawPixmap(x, y, pm)


# ---------------------------------------------------------------------------
# Stub pages — minimal placeholders until real pages are built
# ---------------------------------------------------------------------------

class _StubPage(SmoothScrollArea):
    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.setObjectName(name)
        self.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(SubtitleLabel(name))
        self.setWidget(container)
        self.enableTransparentBackground()

    def set_managers(self, **kwargs):
        pass

    def refresh(self):
        pass


from cdumm.gui.pages.mods_page import ModsPage as PazModsPage  # noqa: E402
from cdumm.gui.pages.asi_page import AsiPluginsPage  # noqa: E402
from cdumm.gui.pages.activity_page import ActivityPage  # noqa: E402
from cdumm.gui.pages.about_page import AboutPage  # noqa: E402
from cdumm.gui.pages.settings_page import SettingsPage  # noqa: E402
from cdumm.gui.pages.tool_page import (  # noqa: E402
    VerifyStatePage, CheckModsPage, FindCulpritPage,
    InspectModPage, FixEverythingPage, RescanPage,
)


# ---------------------------------------------------------------------------
# CdummWindow — the v3 main window
# ---------------------------------------------------------------------------

class CdummWindow(FluentWindow):
    def __init__(
        self,
        db: Database | None = None,
        game_dir: Path | None = None,
        app_data_dir: Path | None = None,
        startup_context: dict | None = None,
    ) -> None:
        super().__init__()

        # ── Window chrome ─────────────────────────────────────────────
        self.setWindowTitle(tr("app.name_short") + " v" + __version__)
        self.setMinimumSize(1000, 700)
        self.resize(1100, 750)

        # Hide the small icon in title bar, center title across full window width
        self.titleBar.iconLabel.hide()
        # Remove titleLabel from layout — we'll position it manually in resizeEvent
        self.titleBar.hBoxLayout.removeWidget(self.titleBar.titleLabel)
        self.titleBar.titleLabel.setParent(self)  # parent to main window, not title bar
        self.titleBar.titleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.titleBar.titleLabel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Still set taskbar icon
        if getattr(sys, "frozen", False):
            icon_path = Path(sys._MEIPASS) / "cdumm.ico"
        else:
            icon_path = Path(__file__).resolve().parents[3] / "cdumm.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # ── Window-scoped drag-drop ──────────────────────────────────
        self.setAcceptDrops(True)
        self._drop_overlay = DropOverlay(self)

        # ── Shared state (mirrors old MainWindow exactly) ─────────────
        self._db = db
        self._game_dir = game_dir
        self._app_data_dir = app_data_dir or Path.home() / "AppData" / "Local" / "cdumm"
        self._cdmods_dir = game_dir / "CDMods" if game_dir else self._app_data_dir
        self._cdmods_dir.mkdir(parents=True, exist_ok=True)
        self._deltas_dir = self._cdmods_dir / "deltas"
        self._vanilla_dir = self._cdmods_dir / "vanilla"
        self._migrate_from_appdata()
        self._worker_thread: QThread | None = None
        self._needs_apply = False
        self._applied_state: dict[int, bool] = {}
        self._snapshot_in_progress = False

        # ── Clean stale staging dir from previous crash ───────────────
        if game_dir:
            staging = game_dir / ".cdumm_staging"
            if staging.exists():
                lock_file = (
                    Path.home() / "AppData" / "Local" / "cdumm" / ".running"
                )
                lock_is_stale = True
                if lock_file.exists():
                    try:
                        lock_time = datetime.fromisoformat(
                            lock_file.read_text(encoding="utf-8").strip()
                        )
                        if datetime.now() - lock_time < timedelta(seconds=30):
                            lock_is_stale = False
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

        # ── Managers ──────────────────────────────────────────────────
        if db:
            self._snapshot = SnapshotManager(db)
            self._mod_manager = ModManager(db, self._deltas_dir)
            self._conflict_detector = ConflictDetector(db)
            self._mod_manager.cleanup_orphaned_deltas()
            from cdumm.engine.activity_log import ActivityLog
            self._activity_log = ActivityLog(db)
        else:
            self._snapshot = None
            self._mod_manager = None
            self._conflict_detector = None
            self._activity_log = None

        # ── Thread dispatcher ─────────────────────────────────────────
        self._dispatcher = _MainThreadDispatcher(self)
        self._active_worker = None
        self._active_progress = None

        # ── Navigation ────────────────────────────────────────────────
        self._init_navigation()

        # ── Theme change signal ──────────────────────────────────────
        from qfluentwidgets.common.config import qconfig
        qconfig.themeChanged.connect(self._on_theme_changed_global)

        # ── Startup context ───────────────────────────────────────────
        self._startup_context = startup_context or {}

        # ── Crash detection lock file ─────────────────────────────────
        self._lock_file = self._app_data_dir / ".running"
        crashed_last_time = self._lock_file.exists()
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file.write_text(str(datetime.now()), encoding="utf-8")

        # ── Deferred startup (after window is visible) ────────────────
        QTimer.singleShot(500, self._deferred_startup)

        # ── Update check ──────────────────────────────────────────────
        QTimer.singleShot(5000, self._check_for_updates)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._check_for_updates)
        self._update_timer.start(15 * 60 * 1000)  # 15 minutes

        # ── DB file watcher (external changes from CLI / CDCrashMonitor) ──
        self._db_watcher_paused = True
        self._db_last_mtime = 0.0
        if self._db and self._db.db_path.exists():
            from PySide6.QtCore import QFileSystemWatcher

            watch_paths = [str(self._db.db_path)]
            wal_path = str(self._db.db_path) + "-wal"
            self._db_wal_path = wal_path
            self._db_watcher = QFileSystemWatcher(watch_paths, self)
            self._db_watcher.fileChanged.connect(self._on_db_changed_externally)
            self._db_change_timer = QTimer(self)
            self._db_change_timer.setSingleShot(True)
            self._db_change_timer.timeout.connect(self._on_db_debounced_refresh)

            # Poll timer: check DB + WAL mtime every 2 seconds
            self._db_poll_timer = QTimer(self)
            self._db_poll_timer.timeout.connect(self._poll_db_for_changes)
            self._db_poll_timer.start(2000)

        # ── Crash report offer ────────────────────────────────────────
        if crashed_last_time:
            QTimer.singleShot(1000, self._offer_crash_report)

    # ------------------------------------------------------------------
    # Navigation setup
    # ------------------------------------------------------------------

    def _init_navigation(self) -> None:
        """Build the sidebar: logo, top nav items, bottom nav items."""
        # Logo in sidebar
        if getattr(sys, "frozen", False):
            logo_path = Path(sys._MEIPASS) / "assets" / "cdumm-logo.png"
        else:
            logo_path = (
                Path(__file__).resolve().parents[3] / "assets" / "cdumm-logo.png"
            )
        logo_str = str(logo_path) if logo_path.exists() else ""

        logo_widget = CdummLogoWidget(logo_str)
        self.navigationInterface.addWidget(
            "logo", logo_widget, lambda: None, NavigationItemPosition.TOP
        )

        # Create pages
        self.paz_mods_page = PazModsPage(self)
        if self._mod_manager and self._conflict_detector and self._db:
            self.paz_mods_page.set_managers(
                self._mod_manager, self._conflict_detector,
                self._db, self._game_dir)
            self.paz_mods_page.file_dropped.connect(self._on_import_dropped)
            self.paz_mods_page._summary_bar.apply_clicked.connect(self._on_apply)
            self.paz_mods_page._summary_bar.revert_clicked.connect(self._on_revert)
            self.paz_mods_page._summary_bar.launch_clicked.connect(self._on_launch_game)
            self.paz_mods_page.uninstall_requested.connect(self._on_uninstall_mod)
        self.asi_plugins_page = AsiPluginsPage(self)
        self.asi_plugins_page.set_managers(game_dir=self._game_dir)

        self.activity_page = ActivityPage(self)
        if hasattr(self, "_activity_log"):
            self.activity_page.set_managers(activity_log=self._activity_log)

        self.settings_page = SettingsPage(self)
        if self._db:
            self.settings_page.set_managers(db=self._db, game_dir=self._game_dir)
        self.settings_page.game_dir_changed.connect(self._on_game_dir_changed)
        self.settings_page.profile_manage_requested.connect(self._on_profiles)
        self.settings_page.export_list_requested.connect(self._on_export_list)
        self.settings_page.import_list_requested.connect(self._on_import_list)

        self.about_page = AboutPage(self)

        # ── Tool pages ───────────────────────────────────────────────
        tool_kwargs = dict(
            db=self._db,
            game_dir=self._game_dir,
            snapshot=self._snapshot,
            mod_manager=self._mod_manager,
            conflict_detector=self._conflict_detector if hasattr(self, '_conflict_detector') else None,
            vanilla_dir=self._vanilla_dir,
            deltas_dir=self._deltas_dir,
            activity_log=self._activity_log if hasattr(self, '_activity_log') else None,
        )

        self.verify_state_page = VerifyStatePage(self)
        self.verify_state_page.set_managers(**tool_kwargs)

        self.check_mods_page = CheckModsPage(self)
        self.check_mods_page.set_managers(**tool_kwargs)

        self.find_culprit_page = FindCulpritPage(self)
        self.find_culprit_page.set_managers(**tool_kwargs)

        self.inspect_mod_page = InspectModPage(self)
        self.inspect_mod_page.set_managers(**tool_kwargs)

        self.fix_everything_page = FixEverythingPage(self)
        self.fix_everything_page.set_managers(**tool_kwargs)
        self.fix_everything_page.rescan_requested.connect(self._on_refresh_snapshot)

        self.rescan_page = RescanPage(self)
        self.rescan_page.set_managers(**tool_kwargs)
        self.rescan_page.rescan_requested.connect(self._on_refresh_snapshot)

        # Sidebar: compact by default, hamburger opens MENU overlay (like Fluent Gallery)
        self.navigationInterface.setExpandWidth(220)
        self.navigationInterface.setMinimumExpandWidth(1400)
        # Acrylic disabled — saves ~40 MB (scipy/numpy dependency)
        self.navigationInterface.setAcrylicEnabled(False)
        self.navigationInterface.setReturnButtonVisible(False)

        # Slow down sidebar animation (default 150ms is too fast, HTML mockups used ~350ms)
        panel = self.navigationInterface.panel
        panel.expandAni.setDuration(350)
        panel.expandAni.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Make acrylic effect more visible in light mode
        # Default light tint is QColor(255,255,255,180) — nearly opaque white, barely shows blur
        # Override _updateAcrylicColor since it's called on every paint
        from PySide6.QtGui import QColor as _QColor
        from qfluentwidgets import isDarkTheme as _isDark
        panel = self.navigationInterface.panel
        _orig_update = panel._updateAcrylicColor
        def _custom_acrylic_color():
            if _isDark():
                panel.acrylicBrush.tintColor = _QColor(32, 32, 32, 150)
                panel.acrylicBrush.luminosityColor = _QColor(0, 0, 0, 0)
            else:
                panel.acrylicBrush.tintColor = _QColor(240, 243, 248, 110)
                panel.acrylicBrush.luminosityColor = _QColor(255, 255, 255, 0)
        panel._updateAcrylicColor = _custom_acrylic_color

        # Top navigation — pages
        self.addSubInterface(self.paz_mods_page, FluentIcon.FOLDER, tr("nav.paz_mods"))
        self.addSubInterface(self.asi_plugins_page, FluentIcon.EMBED, tr("nav.asi_mods"))
        self.addSubInterface(self.activity_page, FluentIcon.DATE_TIME, tr("nav.activity"))

        # Separator before tools
        self.navigationInterface.addSeparator()

        # Diagnostic tools — each is a full sub-interface page
        self.addSubInterface(
            self.verify_state_page, FluentIcon.CHECKBOX, tr("nav.verify_state"),
            position=NavigationItemPosition.SCROLL,
        )
        self.addSubInterface(
            self.check_mods_page, FluentIcon.HEART, tr("nav.mods_health"),
            position=NavigationItemPosition.SCROLL,
        )
        self.addSubInterface(
            self.find_culprit_page, FluentIcon.FINGERPRINT, tr("nav.find_culprit"),
            position=NavigationItemPosition.SCROLL,
        )
        self.addSubInterface(
            self.inspect_mod_page, FluentIcon.ZOOM_IN, tr("nav.inspect_mod"),
            position=NavigationItemPosition.SCROLL,
        )
        self.addSubInterface(
            self.fix_everything_page, FluentIcon.BROOM, tr("nav.fix_everything"),
            position=NavigationItemPosition.SCROLL,
        )
        self.addSubInterface(
            self.rescan_page, FluentIcon.SYNC, tr("nav.rescan"),
            position=NavigationItemPosition.SCROLL,
        )

        # Bottom navigation
        self.addSubInterface(
            self.settings_page, FluentIcon.SETTING, tr("nav.settings"),
            position=NavigationItemPosition.BOTTOM,
        )
        self.addSubInterface(
            self.about_page, FluentIcon.INFO, tr("nav.about"),
            position=NavigationItemPosition.BOTTOM,
        )

    # ------------------------------------------------------------------
    # Runtime retranslation
    # ------------------------------------------------------------------

    def _retranslate_ui(self) -> None:
        """Update all visible text after a language change."""
        # Window title
        self.setWindowTitle(tr("app.name_short") + " v" + __version__)

        # Navigation items — update text via the navigation interface panel
        _nav_texts = {
            "ModsPage": tr("nav.paz_mods"),
            "AsiPluginsPage": tr("nav.asi_mods"),
            "ActivityPage": tr("nav.activity"),
            "VerifyStatePage": tr("nav.verify_state"),
            "CheckModsPage": tr("nav.mods_health"),
            "FindCulpritPage": tr("nav.find_culprit"),
            "InspectModPage": tr("nav.inspect_mod"),
            "FixEverythingPage": tr("nav.fix_everything"),
            "RescanPage": tr("nav.rescan"),
            "SettingsPage": tr("nav.settings"),
            "AboutPage": tr("nav.about"),
        }
        panel = self.navigationInterface.panel
        for route_key, text in _nav_texts.items():
            try:
                item = panel.widget(route_key)
                if item and hasattr(item, 'setText'):
                    item.setText(text)
                    logger.info("Retranslated nav '%s' -> '%s'", route_key, text)
            except Exception as e:
                logger.warning("Failed to retranslate nav '%s': %s", route_key, e)

        # Retranslate each page that supports it
        for page in [
            self.paz_mods_page, self.asi_plugins_page,
            self.activity_page, self.settings_page, self.about_page,
            self.verify_state_page, self.check_mods_page,
            self.find_culprit_page, self.inspect_mod_page,
            self.fix_everything_page, self.rescan_page,
        ]:
            if hasattr(page, 'retranslate_ui'):
                page.retranslate_ui()

    # ------------------------------------------------------------------
    # Migration helper (one-time move of vanilla/deltas to CDMods)
    # ------------------------------------------------------------------

    def _migrate_from_appdata(self) -> None:
        """One-time migration: move vanilla/deltas from old AppData to CDMods on game drive."""
        import shutil

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
                        logger.warning(
                            "Migration failed for %s: %s (will copy instead)",
                            old_dir, e,
                        )
                        try:
                            shutil.copytree(str(old_dir), str(new_dir))
                            shutil.rmtree(old_dir, ignore_errors=True)
                            if sub == "deltas":
                                migrated_deltas_from.append(str(old_dir))
                        except Exception as e2:
                            logger.error("Copy fallback also failed: %s", e2)

        # Update delta_path references in the database
        if migrated_deltas_from and self._db:
            for old_path in migrated_deltas_from:
                new_path = str(self._deltas_dir)
                try:
                    count = self._db.connection.execute(
                        "UPDATE mod_deltas SET delta_path = REPLACE(delta_path, ?, ?)",
                        (old_path, new_path),
                    ).rowcount
                    self._db.connection.commit()
                    logger.info(
                        "Updated %d delta paths: %s -> %s", count, old_path, new_path
                    )
                except Exception as e:
                    logger.error("Failed to update delta paths in DB: %s", e)

    # ------------------------------------------------------------------
    # Deferred startup
    # ------------------------------------------------------------------

    def _deferred_startup(self) -> None:
        """Run after window is visible. Heavy checks happen here."""
        if self._game_dir and self._db:
            if self._check_one_time_reset():
                return
            if self._check_game_updated():
                return

        if self._game_dir and self._snapshot and not self._snapshot.has_snapshot():
            box = MessageBox(
                "Game Files Scan Needed",
                "Before using the mod manager, your game files need to be scanned.\n\n"
                "For best results, please verify your game files through Steam first:\n"
                "  Steam -> Right-click Crimson Desert -> Properties\n"
                "  -> Installed Files -> Verify integrity of game files\n\n"
                "Have you verified (or is this a fresh install)?",
                self,
            )
            if box.exec():
                self._on_refresh_snapshot(skip_verify_prompt=True)
            return

        self._check_stale_appdata()
        self._check_program_files_warning()
        self._check_missing_sources()
        self._check_bad_standalone_imports()
        self._check_show_update_notes()

        # Check if main.py detected a game update during splash
        if self._startup_context.get("game_updated"):
            self._check_game_updated()
        elif self._game_dir and self._snapshot and self._snapshot.has_snapshot():
            try:
                from cdumm.engine.version_detector import detect_game_version
                from cdumm.storage.config import Config

                config = Config(self._db)
                current_fp = detect_game_version(self._game_dir)
                stored_fp = config.get("game_version_fingerprint")
                if current_fp and stored_fp and current_fp != stored_fp:
                    box = MessageBox(
                        "Game Files Changed",
                        "Your game files have changed since the last snapshot.\n\n"
                        "This usually means you verified through Steam.\n\n"
                        "Rescan now to update the snapshot?",
                        self,
                    )
                    if box.exec():
                        self._on_refresh_snapshot(skip_verify_prompt=True)
            except Exception:
                pass

        # Silent startup health check
        self._startup_health_check()

        # Unpause DB watcher now that startup writes are done
        QTimer.singleShot(2000, self._unpause_db_watcher)

    # ------------------------------------------------------------------
    # Startup health check
    # ------------------------------------------------------------------

    def _startup_health_check(self) -> None:
        """Fast silent check for dirty game state on startup."""
        try:
            if not self._db or not self._game_dir or not self._snapshot:
                return
            if not self._snapshot.has_snapshot():
                return

            enabled = self._db.connection.execute(
                "SELECT COUNT(*) FROM mods WHERE enabled = 1"
            ).fetchone()[0]
            if enabled > 0:
                return

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
                has_orphans = False
                for d in self._game_dir.iterdir():
                    if (
                        d.is_dir()
                        and d.name.isdigit()
                        and len(d.name) == 4
                        and int(d.name) >= 36
                    ):
                        orphan_check = self._db.connection.execute(
                            "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                            (d.name + "/%",),
                        ).fetchone()[0]
                        if orphan_check == 0:
                            has_orphans = True
                            break
                if not has_orphans:
                    return

            logger.info("Startup health check: game files dirty, auto-fixing")
            import shutil

            for d in sorted(self._game_dir.iterdir()):
                if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                    continue
                if int(d.name) < 36:
                    continue
                orphan_check = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                    (d.name + "/%",),
                ).fetchone()[0]
                if orphan_check == 0:
                    shutil.rmtree(d, ignore_errors=True)
                    logger.info("Health check: removed orphan directory %s", d.name)

            vanilla_papgt = self._vanilla_dir / "meta" / "0.papgt"
            if vanilla_papgt.exists() and snap:
                if vanilla_papgt.stat().st_size == snap[0]:
                    shutil.copy2(vanilla_papgt, papgt_path)
                    logger.info("Health check: restored vanilla PAPGT")

            self._log_activity("health", "Auto-fixed dirty game state on startup")
        except Exception as e:
            logger.debug("Startup health check failed: %s", e)

        try:
            self._clean_contaminated_deltas()
        except Exception as e:
            logger.debug("Contaminated delta cleanup failed: %s", e)

    def _clean_contaminated_deltas(self) -> None:
        """Remove ENTR deltas incorrectly attributed to a mod (pre-v2.1.6 bug)."""
        if not self._db:
            return
        rows = self._db.connection.execute(
            "SELECT entry_path, COUNT(DISTINCT mod_id) AS cnt "
            "FROM mod_deltas WHERE delta_type = 'ENTR' "
            "GROUP BY entry_path HAVING cnt > 1"
        ).fetchall()
        if not rows:
            return
        for entry_path, _ in rows:
            mods = self._db.connection.execute(
                "SELECT mod_id, COUNT(*) AS total FROM mod_deltas "
                "WHERE delta_type = 'ENTR' AND mod_id IN "
                "(SELECT mod_id FROM mod_deltas WHERE entry_path = ?) "
                "GROUP BY mod_id ORDER BY total ASC",
                (entry_path,),
            ).fetchall()
            if len(mods) < 2:
                continue
            legit_mod = mods[0][0]
            for mod_id, _ in mods[1:]:
                self._db.connection.execute(
                    "DELETE FROM mod_deltas WHERE mod_id = ? AND entry_path = ?",
                    (mod_id, entry_path),
                )
        self._db.connection.commit()

    # ------------------------------------------------------------------
    # Update check
    # ------------------------------------------------------------------

    def _check_for_updates(self) -> None:
        if (
            hasattr(self, "_update_thread")
            and self._update_thread
            and self._update_thread.isRunning()
        ):
            return
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
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: setattr(self, "_update_thread", None))
        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    def _on_update_check_done(self) -> None:
        if not self._update_found:
            logger.info("No updates available")

    def _on_update_available(self, info: dict) -> None:
        self._update_found = True
        self._pending_update_info = info
        tag = info.get("tag", "new version")
        logger.info("Update available: %s", tag)
        InfoBar.info(
            title="Update Available",
            content=f"CDUMM {tag} is available. Check the About page for details.",
            duration=10000, position=InfoBarPosition.TOP_RIGHT, parent=self,
            isClosable=True)

    # ------------------------------------------------------------------
    # DB watcher helpers
    # ------------------------------------------------------------------

    def _get_db_mtime(self) -> float:
        """Get the latest mtime of the DB file and its WAL."""
        import os

        mtime = 0.0
        db_path = str(self._db.db_path)
        if os.path.exists(db_path):
            mtime = max(mtime, os.path.getmtime(db_path))
        wal_path = getattr(self, "_db_wal_path", "")
        if wal_path and os.path.exists(wal_path):
            mtime = max(mtime, os.path.getmtime(wal_path))
        return mtime

    def _stamp_db_mtime(self) -> None:
        """Record current DB mtime so poll ignores our own writes."""
        try:
            self._db_last_mtime = self._get_db_mtime()
        except Exception:
            pass

    def _poll_db_for_changes(self) -> None:
        """Poll DB + WAL file mtimes to detect external writes."""
        if self._db_watcher_paused or not self._db:
            return
        try:
            mtime = self._get_db_mtime()
            if mtime > self._db_last_mtime and self._db_last_mtime > 0:
                self._db_last_mtime = mtime
                self._db_change_timer.start(500)
            elif self._db_last_mtime == 0:
                self._db_last_mtime = mtime
        except Exception:
            pass

    @Slot(str)
    def _on_db_changed_externally(self, path: str) -> None:
        """Called when the database file is modified by an external process."""
        if self._db_watcher_paused:
            return
        self._db_change_timer.start(500)
        if hasattr(self, "_db_watcher") and path not in self._db_watcher.files():
            self._db_watcher.addPath(path)

    def _on_db_debounced_refresh(self) -> None:
        """Refresh UI after external DB change (debounced)."""
        if self._db_watcher_paused:
            return
        logger.info("Database changed externally -- refreshing UI")
        self._sync_db()
        self._db_watcher_paused = True
        self._refresh_all()
        QTimer.singleShot(3000, self._unpause_db_watcher)

    def _unpause_db_watcher(self) -> None:
        self._stamp_db_mtime()
        self._db_watcher_paused = False

    def _sync_db(self) -> None:
        """Sync main DB after a worker writes via WAL checkpoint."""
        if not self._db:
            return
        try:
            self._db.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as e:
            logger.error("WAL checkpoint failed: %s", e)

    # ------------------------------------------------------------------
    # Activity log helper
    # ------------------------------------------------------------------

    def _log_activity(self, category: str, message: str, detail: str = None) -> None:
        """Log an activity to the persistent activity log."""
        if hasattr(self, "_activity_log") and self._activity_log:
            try:
                self._activity_log.log(category, message, detail)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Theme change — reapply custom styles on all components
    # ------------------------------------------------------------------

    def _on_theme_changed_global(self, theme) -> None:
        """Called by qconfig.themeChanged — reapply ALL custom styles everywhere."""
        # Rebuild card-based pages (creates new widgets with correct theme)
        self._refresh_all()

        # Reapply styles on components that aren't rebuilt by refresh()
        for attr_name in dir(self):
            obj = getattr(self, attr_name, None)
            if obj is None:
                continue
            # Pages with summary bars
            if hasattr(obj, '_summary_bar') and hasattr(obj._summary_bar, '_apply_bar_style'):
                obj._summary_bar._apply_bar_style()
            # Config panels
            if hasattr(obj, '_config_panel') and hasattr(obj._config_panel, '_apply_theme'):
                obj._config_panel._apply_theme()

        # Drop overlay
        if hasattr(self, '_drop_overlay'):
            self._drop_overlay._apply_theme()
            self._drop_overlay.update()

        # Tool pages — reapply stat card/result card/button themes
        tool_pages = [
            'verify_page', 'check_page', 'culprit_page',
            'inspect_page', 'fix_page', 'rescan_page',
        ]
        for name in tool_pages:
            page = getattr(self, name, None)
            if page is None:
                continue
            # Stat cards
            if hasattr(page, '_stats_row'):
                for i in range(page._stats_row.count()):
                    item = page._stats_row.itemAt(i)
                    w = item.widget() if item else None
                    if w and hasattr(w, '_apply_theme'):
                        w._apply_theme()
            # Result cards
            if hasattr(page, '_results_layout'):
                for i in range(page._results_layout.count()):
                    item = page._results_layout.itemAt(i)
                    w = item.widget() if item else None
                    if w and hasattr(w, '_apply_theme'):
                        w._apply_theme()
            # Desc label, run button, dividers
            if hasattr(page, '_apply_desc_style'):
                page._apply_desc_style()
            if hasattr(page, '_apply_run_btn_style'):
                page._apply_run_btn_style()
            if hasattr(page, '_header_divider'):
                page._update_divider(page._header_divider)
            if hasattr(page, '_results_divider'):
                page._update_divider(page._results_divider)
            # Re-apply font sizes via QFont (survives theme changes)
            if hasattr(page, '_apply_theme'):
                page._apply_theme() if hasattr(page, '_stats_row') else None

        # Settings page combo centering
        settings = getattr(self, 'settings_page', None)
        if settings and hasattr(settings, '_reapply_custom_styles'):
            settings._reapply_custom_styles()

        logger.debug("Theme changed to %s — refreshed all pages", theme)

    # ------------------------------------------------------------------
    # Refresh all pages
    # ------------------------------------------------------------------

    def _refresh_all(self) -> None:
        """Reload all page data."""
        for page_name in ('paz_mods_page', 'asi_plugins_page', 'activity_page'):
            page = getattr(self, page_name, None)
            if page and hasattr(page, 'refresh'):
                try:
                    page.refresh()
                except Exception as e:
                    logger.debug("%s refresh error: %s", page_name, e)
        # Stamp DB mtime so the poll timer doesn't re-trigger from our own writes
        self._stamp_db_mtime()

    def _on_import_dropped(self, path) -> None:
        """Handle file dropped on the mods page — queues for sequential import."""
        if not self._db or not self._game_dir:
            InfoBar.error(
                title="Not Ready", content="Database or game directory not configured.",
                duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        self._queue_import(Path(path) if not isinstance(path, Path) else path)

    def _queue_import(self, path: Path) -> None:
        """Add a path to the import queue. Processes sequentially."""
        if not hasattr(self, '_import_queue'):
            self._import_queue: list[Path] = []
        if not hasattr(self, '_import_errors'):
            self._import_errors: list[str] = []
        self._import_queue.append(path)
        # If no import is running, start the first one
        if not self._active_worker:
            self._process_next_import()

    def _process_next_import(self) -> None:
        """Process the next item in the import queue."""
        if not hasattr(self, '_import_queue') or not self._import_queue:
            # Queue empty -- show summary if there were errors
            if hasattr(self, '_import_errors') and self._import_errors:
                errors = self._import_errors
                self._import_errors = []
                error_list = "\n".join(f"  - {e}" for e in errors)
                MessageBox(
                    "Some Imports Failed",
                    f"{len(errors)} mod(s) had issues:\n\n{error_list}",
                    self,
                ).exec()
            return

        path = self._import_queue.pop(0)
        remaining = len(self._import_queue)

        if remaining:
            logger.info("Importing %s (%d more queued)", path.name, remaining)

        self._import_with_prechecks(path)

    def _import_with_prechecks(self, path: Path) -> None:
        """Run pre-import checks (main thread, blocking), then launch ImportWorker."""
        logger.info("Import pre-checks for: %s", path)
        existing_mod_id = None

        # ── 1. ASI detection ──────────────────────────────────────────
        from cdumm.asi.asi_manager import AsiManager
        asi_mgr = AsiManager(self._game_dir / "bin64")
        if asi_mgr.contains_asi(path):
            self._install_asi_mod(path, asi_mgr)
            self._process_next_import()
            return

        # ── 2. Snapshot check ─────────────────────────────────────────
        if not self._snapshot or not self._snapshot.has_snapshot():
            if not _is_standalone_paz_mod(path):
                InfoBar.error(
                    title="No Snapshot",
                    content="Game files not scanned yet. Go to Rescan Game Files first.",
                    duration=5000, position=InfoBarPosition.TOP, parent=self)
                self._process_next_import()
                return

        # ── 3. Existing mod detection ─────────────────────────────────
        if self._mod_manager:
            existing = self._find_existing_mod(path)
            if existing:
                mid, mname = existing
                box = MessageBox(
                    "Mod Already Installed",
                    f"'{mname}' is already installed.\n\nUpdate it? (Old version will be removed)",
                    self)
                if box.exec():
                    # Save old state for restoration after import
                    for m in self._mod_manager.list_mods():
                        if m["id"] == mid:
                            self._update_priority = m.get("priority")
                            self._update_enabled = m.get("enabled")
                            break
                    self._mod_manager.remove_mod(mid)
                else:
                    self._process_next_import()
                    return

        # ── 4. Variant picker ─────────────────────────────────────────
        if path.is_dir():
            variants = []
            for sub in sorted(path.iterdir()):
                if sub.is_dir() and not sub.name.startswith('.') and not sub.name.startswith('_'):
                    # Check if subdir contains game file folders (0000-0099 or meta)
                    has_game = any(
                        d.is_dir() and ((d.name.isdigit() and len(d.name) == 4) or d.name == "meta")
                        for d in sub.iterdir() if d.is_dir()
                    )
                    if has_game:
                        variants.append(sub.name)
            if len(variants) > 1:
                from PySide6.QtWidgets import QInputDialog
                chosen, ok = QInputDialog.getItem(
                    self, "Choose Variant",
                    f"This mod has {len(variants)} variants:",
                    variants, 0, False)
                if ok and chosen:
                    path = path / chosen
                else:
                    self._process_next_import()
                    return

        # ── 5. Preset picker ──────────────────────────────────────────
        try:
            from cdumm.gui.preset_picker import find_json_presets, PresetPickerDialog
            import tempfile, zipfile
            check_path = path
            tmp_extract = None
            if path.is_file() and path.suffix.lower() in ('.zip', '.7z'):
                tmp_extract = Path(tempfile.mkdtemp(prefix="cdumm_preset_"))
                if path.suffix.lower() == '.zip':
                    with zipfile.ZipFile(path) as zf:
                        zf.extractall(tmp_extract)
                else:
                    import py7zr
                    with py7zr.SevenZipFile(path, 'r') as zf:
                        zf.extractall(tmp_extract)
                check_path = tmp_extract

            presets = find_json_presets(check_path) if check_path.is_dir() else []
            if len(presets) > 1:
                dialog = PresetPickerDialog(presets, self)
                if dialog.exec() and dialog.selected_path:
                    path = dialog.selected_path
                else:
                    if tmp_extract:
                        import shutil
                        shutil.rmtree(tmp_extract, ignore_errors=True)
                    self._process_next_import()
                    return

            if tmp_extract and not (len(presets) > 1):
                import shutil
                shutil.rmtree(tmp_extract, ignore_errors=True)
        except ImportError:
            pass  # preset_picker not available
        except Exception as e:
            logger.debug("Preset check failed: %s", e)

        # ── 6. Toggle picker ──────────────────────────────────────────
        try:
            from cdumm.engine.json_patch_handler import detect_json_patch, has_labeled_changes
            json_data = None
            if path.suffix.lower() == '.json':
                json_data = detect_json_patch(path)
            elif path.is_dir():
                for f in path.rglob("*.json"):
                    json_data = detect_json_patch(f)
                    if json_data:
                        break

            if json_data and has_labeled_changes(json_data):
                self._configurable_source = str(path)
                from cdumm.gui.preset_picker import TogglePickerDialog
                dialog = TogglePickerDialog(json_data, self)
                if dialog.exec() and dialog.selected_data:
                    # Write filtered JSON to temp
                    import tempfile, json
                    tmp = tempfile.NamedTemporaryFile(
                        suffix=".json", prefix="cdumm_toggle_",
                        delete=False, mode="w", encoding="utf-8")
                    json.dump(dialog.selected_data, tmp, indent=2)
                    tmp.close()
                    self._configurable_labels = getattr(dialog, 'selected_labels', None)
                    path = Path(tmp.name)
                    self._pending_tmp_cleanup = Path(tmp.name).parent
                else:
                    self._configurable_source = None
                    self._process_next_import()
                    return
        except ImportError:
            pass
        except Exception as e:
            logger.debug("Toggle check failed: %s", e)

        # ── Launch ImportWorker ────────────────────────────────────────
        self._launch_import_worker(path, existing_mod_id)

    def _launch_import_worker(self, path: Path, existing_mod_id: int | None = None) -> None:
        """Launch ImportWorker on a background thread after pre-checks passed."""
        logger.info("Launching import worker: %s", path)
        from cdumm.gui.workers import ImportWorker

        worker = ImportWorker(
            mod_path=path,
            game_dir=self._game_dir,
            db_path=self._db.db_path,
            deltas_dir=self._deltas_dir,
            existing_mod_id=existing_mod_id,
        )
        thread = QThread()
        remaining = len(getattr(self, '_import_queue', []))
        suffix = f" ({remaining} more queued)" if remaining else ""
        tip = self._make_state_tooltip(f"Importing {path.name}...{suffix}")

        def on_import_done(result):
            self._sync_db()

            # Clean temp dirs from pre-checks
            tmp = getattr(self, '_pending_tmp_cleanup', None)
            if tmp:
                import shutil
                shutil.rmtree(str(tmp), ignore_errors=True)
                self._pending_tmp_cleanup = None

            name = getattr(result, 'name', None) or path.stem

            # Post-import: game version stamp
            try:
                from cdumm.engine.version_detector import detect_game_version
                ver = detect_game_version(self._game_dir)
                if ver:
                    self._db.connection.execute(
                        "UPDATE mods SET game_version_hash = ? WHERE id = (SELECT MAX(id) FROM mods)",
                        (ver,))
                    self._db.connection.commit()
            except Exception:
                pass

            # Post-import: configurable flag + source path
            cfg_src = getattr(self, '_configurable_source', None)
            if cfg_src:
                try:
                    mod_id = self._db.connection.execute(
                        "SELECT MAX(id) FROM mods").fetchone()[0]
                    self._db.connection.execute(
                        "UPDATE mods SET configurable = 1, source_path = ? WHERE id = ?",
                        (cfg_src, mod_id))
                    self._db.connection.commit()
                    labels = getattr(self, '_configurable_labels', None)
                    if labels:
                        import json
                        self._db.connection.execute(
                            "INSERT OR REPLACE INTO mod_config (mod_id, selected_labels) VALUES (?, ?)",
                            (mod_id, json.dumps(labels)))
                        self._db.connection.commit()
                except Exception:
                    pass
                self._configurable_source = None
                self._configurable_labels = None

            # Post-import: restore update state (priority/enabled)
            upri = getattr(self, '_update_priority', None)
            if upri is not None:
                try:
                    mod_id = self._db.connection.execute(
                        "SELECT MAX(id) FROM mods").fetchone()[0]
                    self._db.connection.execute(
                        "UPDATE mods SET priority = ?, enabled = ? WHERE id = ?",
                        (upri, getattr(self, '_update_enabled', 0), mod_id))
                    self._db.connection.commit()
                except Exception:
                    pass
                self._update_priority = None
                self._update_enabled = None

            self._refresh_all()
            InfoBar.success(
                title="Import Complete",
                content=f"{name} imported successfully.",
                duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            self._log_activity("import", f"Imported mod: {name}")
            self._process_next_import()

        def on_import_error_queued(err):
            if not hasattr(self, '_import_errors'):
                self._import_errors = []
            self._import_errors.append(f"{path.name}: {err}")

        worker.error_occurred.connect(on_import_error_queued)
        self._run_worker(worker, thread, tip, on_import_done)

    def _install_asi_mod(self, path: Path, asi_mgr=None) -> None:
        """Install an ASI mod by copying .asi/.ini files to bin64/."""
        import tempfile
        if asi_mgr is None:
            from cdumm.asi.asi_manager import AsiManager
            asi_mgr = AsiManager(self._game_dir / "bin64")

        if not asi_mgr.has_loader():
            InfoBar.warning(
                title="ASI Loader Missing",
                content="winmm.dll not found in bin64/. ASI mods won't load without it.",
                duration=8000, position=InfoBarPosition.TOP, parent=self)

        # Extract archive if needed
        if path.is_file() and path.suffix.lower() == ".zip":
            import zipfile
            tmp = tempfile.mkdtemp(prefix="cdumm_asi_")
            try:
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(tmp)
                path = Path(tmp)
            except Exception as e:
                logger.error("Failed to extract ASI zip: %s", e)
        elif path.is_file() and path.suffix.lower() == ".7z":
            tmp = tempfile.mkdtemp(prefix="cdumm_asi_")
            try:
                import py7zr
                with py7zr.SevenZipFile(path, 'r') as zf:
                    zf.extractall(tmp)
                path = Path(tmp)
            except Exception as e:
                logger.error("Failed to extract ASI 7z: %s", e)

        installed = asi_mgr.install(path)
        if installed:
            InfoBar.success(
                title="ASI Mod Installed",
                content=f"Installed: {', '.join(installed)} to bin64/",
                duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            logger.info("ASI install success: %s", installed)
            # Refresh ASI page
            if hasattr(self, 'asi_plugins_page'):
                self.asi_plugins_page.refresh()
        else:
            InfoBar.warning(
                title="No ASI Files", content="No .asi files found in this archive.",
                duration=5000, position=InfoBarPosition.TOP, parent=self)

    def _find_existing_mod(self, path: Path) -> tuple[int, str] | None:
        """Check if a dropped mod matches an already-installed mod by name."""
        if not self._mod_manager:
            return None

        def _normalize(s):
            return s.lower().strip().replace("-", " ").replace("_", " ")

        def _compact(s):
            return _normalize(s).replace(" ", "")

        drop_name = path.stem.lower()
        # Try to read mod name from modinfo.json or JSON patch
        try:
            from cdumm.engine.import_handler import _read_modinfo
            if path.is_dir():
                modinfo = _read_modinfo(path)
                if modinfo and modinfo.get("name"):
                    drop_name = modinfo["name"].lower()
        except Exception:
            pass
        try:
            from cdumm.engine.json_patch_handler import detect_json_patch
            if path.suffix.lower() == ".json":
                jp = detect_json_patch(path)
                if jp and jp.get("name"):
                    drop_name = jp["name"].lower()
        except Exception:
            pass

        drop_norm = _normalize(drop_name)
        drop_compact = _compact(drop_name)
        for m in self._mod_manager.list_mods():
            mod_norm = _normalize(m["name"])
            mod_compact = _compact(m["name"])
            if len(mod_norm) >= 4 and mod_norm in drop_norm:
                return (m["id"], m["name"])
            if len(drop_norm) >= 4 and drop_norm in mod_norm:
                return (m["id"], m["name"])
            if len(mod_compact) >= 4 and mod_compact in drop_compact:
                return (m["id"], m["name"])
            if len(drop_compact) >= 4 and drop_compact in mod_compact:
                return (m["id"], m["name"])
        return None

    def _check_game_running(self) -> bool:
        """Check if Crimson Desert is running. Returns True if safe to proceed."""
        try:
            import ctypes
            import ctypes.wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi

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
                            MessageBox(
                                "Game Is Running",
                                "Crimson Desert is currently running.\n\n"
                                "Please close the game before applying mods.",
                                self,
                            ).exec()
                            return False
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:
            pass  # If check fails, let the user proceed
        return True

    def _on_apply(self) -> None:
        """Apply all enabled mods via ApplyWorker on a background thread."""
        if self._active_worker:
            InfoBar.warning(
                title="Busy", content="Another operation is in progress. Please wait.",
                duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        if not self._db or not self._game_dir:
            InfoBar.error(
                title="Not Ready", content="Database or game directory not configured.",
                duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return

        if not self._check_game_running():
            return

        logger.info("Apply requested")
        from cdumm.engine.apply_engine import ApplyWorker

        worker = ApplyWorker(
            game_dir=self._game_dir,
            vanilla_dir=self._vanilla_dir,
            db_path=self._db.db_path,
            force_outdated=True,
        )
        thread = QThread()
        tip = self._make_state_tooltip("Applying mods...")

        def on_apply_done():
            self._sync_db()
            self._snapshot_applied_state()
            from cdumm.engine.import_handler import clear_assigned_dirs
            clear_assigned_dirs()
            self._log_activity("apply", "Applied mod changes")
            # Handle pending removals from batch uninstall
            pending = getattr(self, '_pending_removals', [])
            if pending:
                for mid in pending:
                    try:
                        self._mod_manager.remove_mod(mid)
                    except Exception:
                        pass
                self._pending_removals = []
                self._log_activity("remove", f"Removed {len(pending)} mods after revert")
            self._refresh_all()
            # Run post-apply verification
            self._post_apply_verify()

        self._run_worker(worker, thread, tip, on_apply_done)

    def _on_revert(self) -> None:
        """Revert all game files to vanilla state."""
        if not self._db or not self._game_dir:
            return
        if self._active_worker:
            InfoBar.warning(
                title="Busy", content="Another operation is in progress. Please wait.",
                duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return

        if not self._check_game_running():
            return

        box = MessageBox(
            "Revert to Vanilla",
            "This will restore all game files to their original state.\n"
            "All applied mod changes will be removed.\n\nContinue?",
            self,
        )
        if not box.exec():
            return

        from cdumm.engine.apply_engine import RevertWorker

        worker = RevertWorker(self._game_dir, self._vanilla_dir, self._db.db_path)
        thread = QThread()
        tip = self._make_state_tooltip("Reverting to vanilla...")

        worker.warning.connect(
            lambda msg: self._dispatcher.call(self._show_revert_warning, msg))

        self._run_worker(worker, thread, tip, self._on_revert_finished)

    def _show_revert_warning(self, msg: str) -> None:
        MessageBox("Revert Incomplete", msg, self).exec()

    def _on_revert_finished(self) -> None:
        """Handle revert completion -- disable all mods to match vanilla state."""
        if self._mod_manager:
            for mod in self._mod_manager.list_mods():
                if mod["enabled"]:
                    self._mod_manager.set_enabled(mod["id"], False)
        self._refresh_all()
        self._snapshot_applied_state()
        self._log_activity("revert", "Reverted all game files to vanilla")
        InfoBar.success(
            title="Reverted to Vanilla",
            content="All game files restored to their original state.",
            duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
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

        box = MessageBox(
            "Leftover Backup Files Found",
            f"Found {len(bak_files)} backup file(s) ({total_mb:.0f} MB) in your\n"
            f"game directory:\n\n{names}\n\n"
            "These were created by individual mod scripts (not by CDUMM).\n"
            "They are just taking up disk space.\n\n"
            "Delete them?",
            self,
        )
        if box.exec():
            deleted = 0
            for f in bak_files:
                try:
                    f.unlink()
                    deleted += 1
                except Exception:
                    pass
            InfoBar.success(
                title="Cleanup Complete",
                content=f"Deleted {deleted} backup file(s) ({total_mb:.0f} MB).",
                duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _on_uninstall_mod(self, mod_id: int) -> None:
        """Handle uninstall: apply to revert game files, then remove mod from DB."""
        if self._active_worker:
            InfoBar.warning(
                title="Busy", content="Please wait for the current operation to finish.",
                duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return

        from cdumm.engine.apply_engine import ApplyWorker

        worker = ApplyWorker(
            game_dir=self._game_dir,
            vanilla_dir=self._vanilla_dir,
            db_path=self._db.db_path,
            force_outdated=True,
        )
        thread = QThread()
        tip = self._make_state_tooltip("Reverting mod files...")

        def on_uninstall_apply_done():
            self._sync_db()
            # Now safe to remove the mod from DB
            if self._mod_manager:
                try:
                    details = self._mod_manager.get_mod_details(mod_id)
                    name = details["name"] if details else str(mod_id)
                except Exception:
                    name = str(mod_id)
                self._mod_manager.remove_mod(mod_id)
                self._log_activity("uninstall", f"Uninstalled mod: {name}")
            self._snapshot_applied_state()
            self._refresh_all()
            InfoBar.success(
                title="Mod Uninstalled",
                content="Mod removed and game files reverted.",
                duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)

        self._run_worker(worker, thread, tip, on_uninstall_apply_done)

    # ------------------------------------------------------------------
    # Post-apply verification
    # ------------------------------------------------------------------

    def _post_apply_verify(self) -> None:
        """Deep verification after Apply -- checks PAPGT/PAMT integrity."""
        if not self._game_dir or not self._db:
            return
        import struct
        from cdumm.archive.hashlittle import compute_pamt_hash, compute_papgt_hash

        issues = []

        # 1. Check PAPGT hash
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

        mod_by_file = {}
        modded_dirs = set()
        for fp, mod_name in modded_files:
            parts = fp.split("/")
            if len(parts) >= 2 and parts[0].isdigit():
                modded_dirs.add(parts[0])
            mod_by_file.setdefault(fp, []).append(mod_name)

        # 3. For each modded directory, parse PAMT and verify bounds
        from cdumm.archive.paz_parse import parse_pamt

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
                paz_path = self._game_dir / dir_name / f"{e.paz_index}.paz"
                if paz_path.exists():
                    paz_size = paz_path.stat().st_size
                    if e.offset + e.comp_size > paz_size:
                        mods = ", ".join(set(mod_by_file.get(f"{dir_name}/{e.paz_index}.paz", ["?"])))
                        issues.append((mods, f"{e.path}: out of bounds "
                                       f"(offset={e.offset} + comp={e.comp_size} > paz={paz_size})"))

        # 4. Check for mods imported on a different game version
        try:
            from cdumm.engine.version_detector import detect_game_version
            current_ver = detect_game_version(self._game_dir)
            if current_ver:
                cursor = self._db.connection.execute(
                    "SELECT name, game_version_hash FROM mods "
                    "WHERE enabled = 1 AND game_version_hash IS NOT NULL")
                for name, ver in cursor.fetchall():
                    if ver and ver != current_ver:
                        issues.append((name, "Imported on a different game version -- may be outdated"))
        except Exception:
            pass

        if issues:
            issue_lines = []
            for source, detail in issues[:15]:
                issue_lines.append(f"[{source}] {detail}")
            if len(issues) > 15:
                issue_lines.append(f"... and {len(issues) - 15} more")
            issue_text = "\n".join(issue_lines)

            MessageBox(
                "Post-Apply Verification",
                f"Found {len(issues)} issue(s) that may crash the game:\n\n"
                f"{issue_text}\n\n"
                "The mod name in brackets indicates the likely cause.",
                self,
            ).exec()
            logger.warning("Post-apply issues: %s", issues)
            self._log_activity("warning",
                               f"Post-apply verification: {len(issues)} issue(s)",
                               "; ".join(f"[{s}] {d}" for s, d in issues[:5]))
        else:
            InfoBar.success(
                title="Apply Complete",
                content="All mod changes applied and verified successfully.",
                duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            logger.info("Post-apply verification passed")

    def _on_launch_game(self) -> None:
        """Launch the game executable (tries alternate names, minimizes window)."""
        import subprocess
        if not self._game_dir:
            return
        exe = None
        for candidate in ["CrimsonDesert.exe", "crimsondesert.exe"]:
            test = self._game_dir / "bin64" / candidate
            if test.exists():
                exe = test
                break
        if not exe:
            InfoBar.error(
                title="Not Found", content="CrimsonDesert.exe not found in bin64/",
                duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        try:
            subprocess.Popen([str(exe)], cwd=str(self._game_dir / "bin64"))
            InfoBar.success(
                title="Game Launched", content="Crimson Desert is starting...",
                duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            self.showMinimized()
        except Exception as e:
            InfoBar.error(
                title="Launch Failed", content=str(e),
                duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    # ------------------------------------------------------------------
    # Tool dispatcher (removed -- tools are now sub-interface pages)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Manager re-wiring (for game dir change)
    # ------------------------------------------------------------------

    def _wire_managers(self) -> None:
        """Re-wire all pages with current managers. Called after game dir change."""
        if self._mod_manager and self._conflict_detector and self._db:
            self.paz_mods_page.set_managers(
                self._mod_manager, self._conflict_detector,
                self._db, self._game_dir)
        self.asi_plugins_page.set_managers(game_dir=self._game_dir)
        if hasattr(self, "_activity_log") and self._activity_log:
            self.activity_page.set_managers(activity_log=self._activity_log)
        if self._db:
            self.settings_page.set_managers(db=self._db, game_dir=self._game_dir)

        tool_kwargs = dict(
            db=self._db,
            game_dir=self._game_dir,
            snapshot=self._snapshot,
            mod_manager=self._mod_manager,
            conflict_detector=self._conflict_detector if hasattr(self, '_conflict_detector') else None,
            vanilla_dir=self._vanilla_dir,
            deltas_dir=self._deltas_dir,
            activity_log=self._activity_log if hasattr(self, '_activity_log') else None,
        )
        for page_name in ('verify_state_page', 'check_mods_page', 'find_culprit_page',
                          'inspect_mod_page', 'fix_everything_page', 'rescan_page'):
            page = getattr(self, page_name, None)
            if page:
                page.set_managers(**tool_kwargs)

    # ------------------------------------------------------------------
    # Settings page handlers
    # ------------------------------------------------------------------

    def _on_game_dir_changed(self, new_path: Path) -> None:
        """Handle game directory change — reinitialize DB, managers, and pages."""
        # Block if a worker is active
        if self._active_worker:
            InfoBar.warning(
                title="Busy", content="Please wait for the current operation to finish.",
                duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return

        # Pause DB watcher + timer before touching DB
        self._db_watcher_paused = True
        if hasattr(self, '_db_poll_timer'):
            self._db_poll_timer.stop()

        # Close old DB
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass

        # Update paths
        self._game_dir = new_path
        self._cdmods_dir = new_path / "CDMods"
        self._cdmods_dir.mkdir(parents=True, exist_ok=True)
        self._deltas_dir = self._cdmods_dir / "deltas"
        self._vanilla_dir = self._cdmods_dir / "vanilla"

        # Open new DB
        new_db_path = self._cdmods_dir / "cdumm.db"
        self._db = Database(new_db_path)
        self._db.initialize()

        # Reinitialize managers with new DB
        self._snapshot = SnapshotManager(self._db)
        self._mod_manager = ModManager(self._db, self._deltas_dir)
        self._conflict_detector = ConflictDetector(self._db)
        from cdumm.engine.activity_log import ActivityLog
        self._activity_log = ActivityLog(self._db)

        # Update DB watcher to watch new path
        if hasattr(self, '_db_watcher'):
            # Remove old paths, add new
            old_files = self._db_watcher.files()
            if old_files:
                self._db_watcher.removePaths(old_files)
            self._db_watcher.addPath(str(new_db_path))
            self._db_wal_path = str(new_db_path) + "-wal"

        # Re-wire all pages with new managers
        self._wire_managers()
        self._refresh_all()

        # Resume DB watcher + timer
        self._stamp_db_mtime()
        self._db_watcher_paused = False
        if hasattr(self, '_db_poll_timer'):
            self._db_poll_timer.start(2000)

        logger.info("Game directory changed to %s — managers reinitialized", new_path)

    def _on_profiles(self) -> None:
        """Open the mod profiles dialog."""
        if not self._db:
            return
        from cdumm.gui.profile_dialog import ProfileDialog
        dialog = ProfileDialog(self._db, self)
        dialog.exec()
        if dialog.was_profile_loaded:
            self._refresh_all()
            self._on_apply()

    def _on_export_list(self) -> None:
        """Export the current mod list to a JSON file."""
        if not self._db:
            return
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Mod List", "cdumm_modlist.json", "JSON Files (*.json)")
        if not path:
            return
        from cdumm.engine.mod_list_io import export_mod_list
        count = export_mod_list(self._db, Path(path))
        InfoBar.success(
            title="Export Complete",
            content=f"Exported {count} mods to {Path(path).name}",
            duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _on_import_list(self) -> None:
        """Import a mod list from a JSON file."""
        if not self._db:
            return
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Import Mod List", "", "JSON Files (*.json)")
        if not path:
            return
        from cdumm.engine.mod_list_io import import_mod_list
        mods = import_mod_list(Path(path))
        if not mods:
            InfoBar.warning(
                title="Import Empty",
                content="No mods found in the file.",
                duration=5000, position=InfoBarPosition.TOP, parent=self)
            return
        installed = {m["name"].lower() for m in (
            self._mod_manager.list_mods() if self._mod_manager else [])}
        missing = sum(1 for m in mods if m["name"].lower() not in installed)
        lines = []
        for m in mods:
            status = "installed" if m["name"].lower() in installed else "MISSING"
            entry = f"[{status}] {m['name']}"
            if m.get("author"):
                entry += f" by {m['author']}"
            lines.append(entry)
        box = MessageBox(
            "Mod List",
            f"{len(mods)} mods in list, {missing} not installed:\n\n"
            + "\n".join(lines),
            self)
        box.exec()

    # ------------------------------------------------------------------
    # Worker infrastructure
    # ------------------------------------------------------------------

    def _make_state_tooltip(self, title: str) -> StateToolTip:
        """Create a StateToolTip positioned at the top-right of the window."""
        tip = StateToolTip(title, "Starting...", self)
        tip.move(tip.getSuitablePos())
        tip.show()
        return tip

    def _run_worker(self, worker, thread: QThread, progress: StateToolTip, on_finished) -> None:
        """Wire a worker + thread + StateToolTip with safe signal routing."""
        self._active_worker = worker
        self._worker_thread = thread
        self._active_progress = progress

        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # CRITICAL: Route progress through dispatcher — lambdas in PySide6
        # execute on the emitter's thread, not the receiver's. Calling
        # StateToolTip.setContent() from a worker thread segfaults silently.
        worker.progress_updated.connect(
            lambda pct, msg: self._dispatcher.call(
                self._update_progress_tip, progress, pct, msg))

        worker.finished.connect(
            lambda *args: self._dispatcher.call(
                self._worker_done, thread, progress, on_finished, *args))
        worker.error_occurred.connect(
            lambda err: self._dispatcher.call(
                self._worker_error, thread, progress, err))

        if hasattr(self, "_db_watcher_paused"):
            self._db_watcher_paused = True

        thread.start()

    @staticmethod
    def _update_progress_tip(tip: StateToolTip, pct: int, msg: str) -> None:
        """Update StateToolTip from main thread (safe for GUI)."""
        try:
            tip.setContent(f"{msg} ({pct}%)")
        except RuntimeError:
            pass  # tooltip already deleted

    def _worker_done(self, thread, progress: StateToolTip, callback, *args) -> None:
        progress.setContent("Completed!")
        progress.setState(True)
        thread.quit()
        thread.wait(5000)
        thread.deleteLater()
        self._active_progress = None
        self._active_worker = None
        self._worker_thread = None
        if hasattr(self, "_db_watcher_paused"):
            QTimer.singleShot(1000, self._unpause_db_watcher)
        try:
            callback(*args)
        except Exception:
            logger.error("Completion callback crashed", exc_info=True)

    def _worker_error(self, thread, progress: StateToolTip, err) -> None:
        progress.setContent("Failed!")
        progress.setState(True)
        thread.quit()
        thread.wait(5000)
        thread.deleteLater()
        self._active_progress = None
        self._active_worker = None
        self._worker_thread = None
        # If there's an import queue, don't show a blocking error -- just continue
        if hasattr(self, '_import_queue') and self._import_queue:
            self._process_next_import()
            return
        InfoBar.error(
            title="Error", content=str(err),
            duration=-1, position=InfoBarPosition.TOP, parent=self)

    # (Tool methods removed -- logic moved to individual tool pages)

    # ------------------------------------------------------------------
    # Snapshot applied state tracking
    # ------------------------------------------------------------------

    def _snapshot_applied_state(self) -> None:
        """Save current mod enabled states as the 'applied' baseline."""
        if self._mod_manager:
            self._applied_state = {
                m["id"]: m["enabled"] for m in self._mod_manager.list_mods()
            }

    # ------------------------------------------------------------------
    # Crash report offer
    # ------------------------------------------------------------------

    def _offer_crash_report(self) -> None:
        box = MessageBox(
            "Previous Session Crashed",
            "It looks like the app didn't close normally last time.\n"
            "This could indicate a bug.\n\n"
            "Would you like to generate a bug report?\n"
            "(You can attach it to a Nexus Mods bug report)",
            self,
        )
        if box.exec():
            from cdumm.gui.bug_report import generate_bug_report, BugReportDialog

            report = generate_bug_report(
                self._db, self._game_dir, self._app_data_dir
            )
            dialog = BugReportDialog(report, self, is_crash=True)
            dialog.exec()

    # ------------------------------------------------------------------
    # Stubs for deferred startup check methods
    # These delegate to the same logic as the old MainWindow.
    # Implemented as no-ops until the corresponding pages/dialogs exist.
    # ------------------------------------------------------------------

    def _check_one_time_reset(self) -> bool:
        """Check if a one-time DB reset was requested. Returns True if handled."""
        from cdumm.storage.config import Config

        config = Config(self._db)
        if config.get("one_time_reset"):
            config.delete("one_time_reset")
            logger.info("One-time reset flag found -- triggering rescan")
            self._on_refresh_snapshot(skip_verify_prompt=True)
            return True
        return False

    def _check_game_updated(self) -> bool:
        """Check if the game was updated and offer a rescan."""
        if not self._startup_context.get("game_updated"):
            return False

        box = MessageBox(
            "Game Updated",
            "Crimson Desert has been updated since your last snapshot.\n\n"
            "All mods will be disabled and a fresh scan is needed.\n\n"
            "Rescan now?",
            self,
        )
        if box.exec():
            self._on_refresh_snapshot(skip_verify_prompt=True)
        return True

    def _check_stale_appdata(self) -> None:
        """Detect stale data in %LocalAppData%/cdumm from old versions."""
        try:
            from cdumm.storage.config import Config
            config = Config(self._db)
            if config.get("stale_appdata_checked"):
                return

            appdata_dir = Path.home() / "AppData" / "Local" / "cdumm"
            if not appdata_dir.exists():
                config.set("stale_appdata_checked", "1")
                return

            has_stale = False
            for name in ["deltas", "vanilla", "cdumm.db"]:
                if (appdata_dir / name).exists():
                    has_stale = True
                    break

            if not has_stale:
                config.set("stale_appdata_checked", "1")
                return

            total_size = 0
            try:
                for f in appdata_dir.rglob("*"):
                    if f.is_file():
                        total_size += f.stat().st_size
            except Exception:
                pass
            size_mb = total_size / (1024 * 1024)

            box = MessageBox(
                "Old Data Found",
                f"Found leftover data from an older CDUMM version in:\n"
                f"{appdata_dir}\n"
                f"({size_mb:.0f} MB)\n\n"
                f"Since v1.7.0, all mod data is stored in the CDMods folder\n"
                f"inside your game directory. This old data is no longer needed.\n\n"
                f"Delete it to free up space?",
                self,
            )
            if box.exec():
                import shutil
                for name in ["deltas", "vanilla", "cdumm.db"]:
                    target = appdata_dir / name
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    elif target.is_file():
                        target.unlink(missing_ok=True)
                InfoBar.success(
                    title="Cleanup Complete",
                    content=f"Cleaned up {size_mb:.0f} MB of old data.",
                    duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
                self._log_activity("cleanup",
                                   f"Removed stale AppData ({size_mb:.0f} MB)")

            config.set("stale_appdata_checked", "1")
        except Exception as e:
            logger.debug("Stale appdata check failed: %s", e)

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

            MessageBox(
                "Game Location Warning",
                "Your game is installed under Program Files, which has\n"
                "restricted write permissions on Windows.\n\n"
                "This can cause issues with mod backups and configuration.\n"
                "If you experience problems, consider moving your Steam\n"
                "library to a different location (e.g. C:\\SteamLibrary).\n\n"
                "Steam -> Settings -> Storage -> Add a new library folder",
                self,
            ).exec()
            config.set("program_files_warned", "1")
        except Exception as e:
            logger.debug("Program Files warning check failed: %s", e)

    def _check_missing_sources(self) -> None:
        """Notify user about mods that have no stored source files."""
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
                names = "\n".join(f"  - {n}" for n in missing[:10])
                extra = f"\n  ...and {len(missing) - 10} more" if len(missing) > 10 else ""
                MessageBox(
                    "Mods Need Re-import",
                    f"These mods were imported on an older version and don't have\n"
                    f"stored source files. They can't be auto-updated or reconfigured.\n\n"
                    f"{names}{extra}\n\n"
                    f"To fix this, remove each mod and drag the original download\n"
                    f"file back in. You only need to do this once per mod.",
                    self,
                ).exec()

            config.set("missing_sources_checked", "1")
        except Exception as e:
            logger.debug("Missing sources check failed: %s", e)

    def _check_bad_standalone_imports(self) -> None:
        """No longer auto-disables mods. Outdated detection in get_mod_game_status
        and Apply skip logic handle this -- the user sees 'outdated' status and
        Apply skips them unless force-applied."""
        pass

    def _check_show_update_notes(self) -> None:
        """Show patch notes dialog if version changed since last run."""
        from cdumm import __version__
        from cdumm.storage.config import Config

        config = Config(self._db)
        last_ver = config.get("last_seen_version")
        if last_ver != __version__:
            config.set("last_seen_version", __version__)
            # TODO: show patch notes dialog

    def _on_refresh_snapshot(self, skip_verify_prompt: bool = False) -> None:
        """Trigger a full game file rescan via SnapshotWorker on a background thread."""
        if not self._db or not self._game_dir:
            return
        if self._snapshot_in_progress:
            return
        if self._active_worker:
            InfoBar.warning(
                title="Busy", content="Another operation is in progress. Please wait.",
                duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return

        if not skip_verify_prompt:
            box = MessageBox(
                "Rescan Game Files",
                "This will create a new vanilla snapshot from your current game files.\n\n"
                "Have you verified your game files through Steam?\n\n"
                "  Steam -> Right-click Crimson Desert -> Properties\n"
                "  -> Installed Files -> Verify integrity of game files\n\n"
                "Only rescan after Steam verify -- otherwise the snapshot\n"
                "may capture modded files as 'vanilla'.",
                self,
            )
            if not box.exec():
                return

        self._snapshot_in_progress = True

        # Clear stale vanilla backups -- after Steam verify the game files are
        # clean, so existing backups may be from a previous modded state.
        if self._vanilla_dir and self._vanilla_dir.exists():
            import shutil
            try:
                shutil.rmtree(self._vanilla_dir, ignore_errors=True)
                self._vanilla_dir.mkdir(parents=True, exist_ok=True)
                logger.info("Cleared stale vanilla backups for fresh snapshot")
            except Exception as e:
                logger.warning("Failed to clear vanilla backups: %s", e)

        from cdumm.engine.snapshot_manager import SnapshotWorker

        worker = SnapshotWorker(self._game_dir, self._db.db_path)
        worker.activity.connect(self._log_activity)
        thread = QThread()
        tip = self._make_state_tooltip("Creating vanilla snapshot...")

        self._run_worker(worker, thread, tip, self._on_snapshot_finished)

    def _on_snapshot_finished(self, count: int) -> None:
        """Handle snapshot completion."""
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

        # Refresh vanilla backups — ensure they match clean game state
        self._refresh_vanilla_backups()

        self._refresh_all()
        InfoBar.success(
            title="Snapshot Complete",
            content=f"{count} game files indexed. You can now import mods.",
            duration=6000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        self._log_activity("snapshot", f"Game files scanned: {count} files indexed")

    def _refresh_vanilla_backups(self) -> None:
        """Ensure vanilla backup files exist for all snapshot entries.

        After a snapshot (which runs after Steam verify), game files are known-clean.
        Copy any missing files to the vanilla backup directory.
        """
        if not self._db or not self._game_dir or not self._vanilla_dir:
            return
        import shutil, os
        try:
            rows = self._db.connection.execute(
                "SELECT file_path FROM snapshots"
            ).fetchall()
            copied = 0
            for (rel_path,) in rows:
                game_file = self._game_dir / rel_path.replace("/", os.sep)
                backup_file = self._vanilla_dir / rel_path.replace("/", os.sep)
                if game_file.exists() and not backup_file.exists():
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(game_file, backup_file)
                    copied += 1
            if copied:
                logger.info("Refreshed %d vanilla backups", copied)
        except Exception as e:
            logger.warning("Vanilla backup refresh failed: %s", e)

    # ------------------------------------------------------------------
    # Window-scoped drag and drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls():
            event.acceptProposedAction()
            self._drop_overlay.resize(self.size())
            self._drop_overlay.show_overlay()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drop_overlay.hide_overlay()

    def dropEvent(self, event) -> None:  # noqa: N802
        self._drop_overlay.hide_overlay()
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                local = url.toLocalFile()
                if local:
                    self._on_import_dropped(Path(local))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, '_drop_overlay'):
            self._drop_overlay.resize(self.size())
        # Center title label across the full window width
        if hasattr(self.titleBar, 'titleLabel'):
            lbl = self.titleBar.titleLabel
            lbl.adjustSize()
            lbl.move((self.width() - lbl.width()) // 2, (self.titleBar.height() - lbl.height()) // 2)

    # ------------------------------------------------------------------
    # closeEvent
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Clean shutdown -- stop timers, quit threads, close DB, remove lock."""
        # Stop timers
        for timer_name in ("_update_timer", "_db_poll_timer"):
            timer = getattr(self, timer_name, None)
            if timer:
                timer.stop()
        # Stop worker threads
        for thread_name in ("_worker_thread", "_update_thread"):
            thread = getattr(self, thread_name, None)
            if thread and thread.isRunning():
                thread.quit()
                thread.wait(2000)
        # Close DB
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
        # Remove lock file
        if hasattr(self, "_lock_file") and self._lock_file.exists():
            try:
                self._lock_file.unlink()
            except Exception:
                pass
        super().closeEvent(event)
