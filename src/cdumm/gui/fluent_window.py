"""CDUMM v3 main window — FluentWindow with sidebar navigation."""
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

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


from cdumm.engine.nexus_filename import parse_nexus_filename as _parse_nexus_filename


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


_NON_PRESET_JSON = {"mod.json", "manifest.json", "modinfo.json"}


def _archive_likely_needs_dialog(path: Path) -> bool:
    """Lightweight namelist peek: does this archive have multiple JSON presets or variant folders?

    Avoids full extraction during batch pre-scan. Returns True if the archive
    appears to contain either:
      - Multiple real preset .json files at the root (NexusMods variants like
        ``friendly_gain_x2.json`` / ``_x5.json``) — not manifest/modinfo metadata
      - Multiple top-level folders each containing game content (variant picker case)
    """
    import re
    suffix = path.suffix.lower()
    names: list[str] = []
    try:
        if suffix == ".zip":
            import zipfile
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        elif suffix == ".7z":
            import py7zr
            with py7zr.SevenZipFile(path, 'r') as zf:
                names = zf.getnames()
        elif suffix == ".rar":
            # 7-Zip CLI listing — zero extraction cost. First "Path = " line is
            # the archive itself; skip entries that don't look like relative paths.
            import subprocess
            _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            for tool in ("7z", "7z.exe", r"C:\Program Files\7-Zip\7z.exe"):
                try:
                    out = subprocess.run([tool, "l", "-slt", str(path)],
                                         capture_output=True, text=True, timeout=8,
                                         creationflags=_no_window)
                    if out.returncode == 0:
                        archive_abs = str(path)
                        for ln in out.stdout.splitlines():
                            ln = ln.strip()
                            if ln.startswith("Path = "):
                                val = ln[len("Path = "):]
                                if val and val != archive_abs:
                                    names.append(val)
                        break
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue
    except Exception:
        return False
    if not names:
        return False

    # Normalize Windows-style backslashes (RAR) to forward slashes.
    names = [n.replace("\\", "/") for n in names]

    # Count real preset .json files at root or one level deep. Manifest /
    # modinfo / mod.json are CDUMM metadata, not preset variants.
    root_level_presets = [
        n for n in names
        if n.lower().endswith(".json")
        and n.count("/") <= 1
        and n.rsplit("/", 1)[-1].lower() not in _NON_PRESET_JSON
    ]
    if len(root_level_presets) > 1:
        return True

    # Variant folders: 2+ top-level dirs each containing game files. NNNN/
    # top-level entries are game data dirs (e.g., 0002/, 0012/), NOT variants,
    # so exclude them from the variant candidate set.
    tops = {
        n.split("/", 1)[0] for n in names
        if n and not n.startswith("/") and not re.match(r"^\d{4}$", n.split("/", 1)[0])
    }
    if len(tops) >= 2:
        variants_with_content = 0
        for t in tops:
            prefix = t + "/"
            has_content = any(
                (m != t and m.startswith(prefix) and (
                    m.lower().endswith((".paz", ".pamt", ".bsdiff"))
                    or re.search(r"/\d{4}/", "/" + m)
                ))
                for m in names
            )
            if has_content:
                variants_with_content += 1
        if variants_with_content >= 2:
            return True

    return False


def _has_game_content(path: Path) -> bool:
    """Check if a ZIP/archive contains game mod content (PAZ, PAMT, JSON patches, etc.).

    Fast check — reads ZIP directory only, no extraction. Used to distinguish
    pure ASI mods from mixed ZIP mods (ASI + PAZ in one archive).
    """
    import zipfile
    import re
    if path.is_dir():
        # Check if directory has any non-ASI game content
        has_asi = any(path.rglob("*.asi"))
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            sl = f.suffix.lower()
            if sl in (".asi", ".ini", ".dll"):
                continue
            if sl in (".paz", ".pamt", ".bsdiff", ".xdelta"):
                return True
            # .json only counts if no .asi files (ASI mods bundle .json configs)
            if sl == ".json" and f.name.lower() != "modinfo.json" and not has_asi:
                return True
            if re.match(r"^\d{4}$", f.parent.name):
                return True
        return False
    if not path.is_file() or path.suffix.lower() not in (".zip", ".7z", ".rar"):
        return False
    if path.suffix.lower() == ".7z":
        try:
            import py7zr
            with py7zr.SevenZipFile(path, 'r') as zf:
                names = [n.lower() for n in zf.getnames()]
                has_asi = any(n.endswith(".asi") for n in names)
                has_game = any(
                    n.endswith((".paz", ".pamt", ".bsdiff", ".xdelta"))
                    for n in names
                )
                # .json only counts as game content if no .asi files present
                # (ASI mods often bundle .json config files)
                if not has_game and not has_asi:
                    has_game = any(
                        n.endswith(".json") and "modinfo" not in n
                        for n in names
                    )
                return has_game
        except Exception:
            return True  # can't read — assume mixed
    if path.suffix.lower() == ".rar":
        return True  # rar can't be cheaply scanned
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            lower = [n.lower() for n in names]
            has_asi = any(nl.endswith(".asi") for nl in lower)
            for n, nl in zip(names, lower):
                if nl.endswith((".paz", ".pamt", ".bsdiff", ".xdelta")):
                    return True
                # .json only counts if no .asi files (ASI mods bundle .json configs and modinfo.json)
                if nl.endswith(".json") and not has_asi:
                    return True
                # Numbered directory pattern (0008/, 0036/, etc.)
                if re.match(r"^\d{4}/", n):
                    return True
    except Exception:
        return True  # can't read ZIP — let worker handle it
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
    Supports theme-aware logo switching (light/dark variants).
    """

    _COMPACT_SIZE = 36
    _EXPANDED_HEIGHT = 100

    def __init__(self, logo_light: str, logo_dark: str = "", parent=None):
        super().__init__(isSelectable=False, parent=parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._on_click = None
        self._logo_light = logo_light
        self._logo_dark = logo_dark or logo_light
        self._load_pixmap(logo_light)
        self.setFixedSize(40, self._COMPACT_SIZE)

        # Match sidebar's animation timing
        self._height_anim = QPropertyAnimation(self, b"fixedHeight")
        self._height_anim.setDuration(350)
        self._height_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _load_pixmap(self, path: str) -> None:
        self._pixmap = QPixmap(path) if path else QPixmap()
        self._compact_pm = self._pixmap.scaled(
            32, 32, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation) if not self._pixmap.isNull() else QPixmap()
        self._expanded_pm = QPixmap()  # rebuilt in setCompacted

    def set_theme_variant(self, is_dark: bool) -> None:
        """Switch between light/dark logo and rebuild caches."""
        path = self._logo_dark if is_dark else self._logo_light
        self._load_pixmap(path)
        # Rebuild expanded cache if sidebar is currently expanded
        if not self.isCompacted and not self._pixmap.isNull():
            self._expanded_pm = self._pixmap.scaled(
                self.EXPAND_WIDTH - 16, self._EXPANDED_HEIGHT - 12,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        self.update()

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

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._on_click:
            self._on_click()
        super().mousePressEvent(event)


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
from cdumm.gui.pages.bug_report_page import BugReportPage  # noqa: E402
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

        # Restore saved window geometry (size/position) from the previous session.
        # Stored as base64(QByteArray) in the config table so it survives DB copies.
        if db is not None:
            try:
                from cdumm.storage.config import Config
                from PySide6.QtCore import QByteArray
                saved = Config(db).get("window_geometry")
                if saved:
                    geom = QByteArray.fromBase64(saved.encode("ascii"))
                    if not geom.isEmpty():
                        self.restoreGeometry(geom)
            except Exception as _e:
                logger.debug("Could not restore window geometry: %s", _e)

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

        # ── System theme listener (Windows) ──────────────────────────
        # qconfig.themeChanged fires when setTheme() is called in-app,
        # but the signal never fires when Windows flips system theme
        # (e.g. user enables dark mode, or wallpaper slideshow triggers
        # a palette change). SystemThemeListener is a thread that
        # watches the system and calls setTheme(AUTO) on change, which
        # cascades into qconfig.themeChanged. Without it, CDUMM's
        # content stays white-on-white after Windows flips — the
        # ZAIAC001 bug.
        try:
            from qfluentwidgets import SystemThemeListener
            self._theme_listener = SystemThemeListener(self)
            self._theme_listener.start()
        except Exception as e:
            logger.warning("SystemThemeListener unavailable: %s", e)
            self._theme_listener = None

        # ── Startup context ───────────────────────────────────────────
        self._startup_context = startup_context or {}

        # ── Crash detection lock file ─────────────────────────────────
        self._lock_file = self._app_data_dir / ".running"
        crashed_last_time = self._lock_file.exists()
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file.write_text(str(datetime.now()), encoding="utf-8")

        # ── Deferred startup (after window is visible) ────────────────
        QTimer.singleShot(500, self._deferred_startup)

        # ── Page switch: toggle update banner visibility ──────────────
        self.stackedWidget.currentChanged.connect(
            lambda: self._position_update_banner())

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
        # Logo in sidebar (theme-aware: light + dark variants)
        if getattr(sys, "frozen", False):
            _assets = Path(sys._MEIPASS) / "assets"
        else:
            _assets = Path(__file__).resolve().parents[3] / "assets"
        _logo_light = _assets / "cdumm-logo-light.png"
        _logo_dark = _assets / "cdumm-logo-dark.png"
        _light_str = str(_logo_light) if _logo_light.exists() else ""
        _dark_str = str(_logo_dark) if _logo_dark.exists() else ""

        from qfluentwidgets import isDarkTheme
        logo_widget = CdummLogoWidget(_light_str, _dark_str)
        if isDarkTheme():
            logo_widget.set_theme_variant(True)
        self._logo_widget = logo_widget
        self.navigationInterface.addWidget(
            "logo", logo_widget, lambda: None, NavigationItemPosition.TOP
        )

        # Create pages
        self.paz_mods_page = PazModsPage(self)
        if self._mod_manager and self._conflict_detector and self._db:
            # Load applied state BEFORE set_managers (which triggers refresh/card build)
            self._load_applied_state()
            self.paz_mods_page.set_managers(
                self._mod_manager, self._conflict_detector,
                self._db, self._game_dir)
            self.paz_mods_page.file_dropped.connect(self._on_import_dropped)
            self.paz_mods_page._summary_bar.apply_clicked.connect(self._on_apply)
            self.paz_mods_page._summary_bar.revert_clicked.connect(self._on_revert)
            self.paz_mods_page._summary_bar.launch_clicked.connect(self._on_launch_game)
            self.paz_mods_page.uninstall_requested.connect(self._on_uninstall_mod)
        self.asi_plugins_page = AsiPluginsPage(self)
        self.asi_plugins_page.set_managers(game_dir=self._game_dir, db=self._db)

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
        self._logo_widget._on_click = lambda: self.switchTo(self.about_page)

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

        self.bug_report_page = BugReportPage(self)
        self.bug_report_page.set_managers(
            db=self._db, game_dir=self._game_dir,
            app_data_dir=self._app_data_dir)

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
            self.bug_report_page, FluentIcon.FEEDBACK, tr("nav.bug_report"),
            position=NavigationItemPosition.BOTTOM,
        )
        self.addSubInterface(
            self.settings_page, FluentIcon.SETTING, tr("nav.settings"),
            position=NavigationItemPosition.BOTTOM,
        )
        self.addSubInterface(
            self.about_page, FluentIcon.INFO, tr("nav.about"),
            position=NavigationItemPosition.BOTTOM,
        )

        # ── NexusMods update check (30 min timer + startup) ────────
        self._nexus_updates: dict[int, object] = {}  # nexus_mod_id -> ModUpdateStatus
        self._nexus_update_timer = QTimer(self)
        self._nexus_update_timer.setInterval(30 * 60 * 1000)  # 30 minutes
        self._nexus_update_timer.timeout.connect(self._run_nexus_update_check)
        self._nexus_update_timer.start()
        # Run first check 5 seconds after startup (let UI settle)
        QTimer.singleShot(5000, self._run_nexus_update_check)

    # ------------------------------------------------------------------
    # NexusMods automatic update check
    # ------------------------------------------------------------------

    def _run_nexus_update_check(self) -> None:
        """Check NexusMods for mod updates in background. Runs automatically."""
        if not self._db:
            return
        from cdumm.storage.config import Config
        api_key = Config(self._db).get("nexus_api_key")
        if not api_key:
            return  # No API key configured — skip silently

        # Read both PAZ mods and ASI plugins on main thread (SQLite thread safety).
        # Both tables share a single NexusMods update-check pass so the API quota
        # is spent once per cycle.
        try:
            cursor = self._db.connection.execute(
                "SELECT id, name, version, nexus_mod_id FROM mods WHERE mod_type = 'paz'")
            mods = [{"id": r[0], "name": r[1], "version": r[2], "nexus_mod_id": r[3]}
                    for r in cursor.fetchall()]
            cursor = self._db.connection.execute(
                "SELECT name, version, nexus_mod_id FROM asi_plugin_state")
            asi_mods = [{"id": r[0], "name": r[0], "version": r[1], "nexus_mod_id": r[2]}
                        for r in cursor.fetchall()]
        except Exception:
            return

        combined = mods + asi_mods
        if not any(m.get("nexus_mod_id") for m in combined):
            return  # No mods with NexusMods IDs in either table

        import threading
        def _check():
            try:
                from cdumm.engine.nexus_api import check_mod_updates
                updates = check_mod_updates(combined, api_key)
                self._pending_nexus_updates = {u.mod_id: u for u in updates}
            except Exception as e:
                logger.warning("NexusMods update check failed: %s", e)
                self._pending_nexus_updates = {}
            from PySide6.QtCore import QMetaObject, Qt as _Qt
            QMetaObject.invokeMethod(
                self, "_apply_nexus_update_colors", _Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=_check, daemon=True).start()

    @Slot()
    def _apply_nexus_update_colors(self) -> None:
        """Propagate update results to both PAZ mods page and ASI plugins page."""
        self._nexus_updates = getattr(self, "_pending_nexus_updates", {})
        if hasattr(self, 'paz_mods_page'):
            self.paz_mods_page.set_nexus_updates(self._nexus_updates)
        if hasattr(self, 'asi_plugins_page'):
            try:
                self.asi_plugins_page.set_nexus_updates(self._nexus_updates)
            except AttributeError:
                pass  # ASI page may not implement it in older builds
        logger.info("NexusMods update check: %d updates found", len(self._nexus_updates))

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
            "BugReportPage": tr("nav.bug_report"),
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
            self.bug_report_page,
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
        # Retroactive configurable-mod scan: covers imports that predate the
        # configurable-detection logic. Cheap for mods whose source_path is
        # already a directory; rescues old ZIP-backed entries by extracting
        # to CDMods/sources/<mod_id>/ the first time they are encountered.
        if self._db and self._cdmods_dir:
            try:
                from cdumm.engine.configurable_scanner import scan_configurable_mods
                stats = scan_configurable_mods(
                    self._db, self._cdmods_dir / "sources")
                if stats["flagged_a"] or stats["flagged_b"] or stats["rescued"]:
                    self._refresh_all()
            except Exception as e:
                logger.warning("Configurable scan failed (non-fatal): %s", e)

        if self._game_dir and self._db:
            if self._check_one_time_reset():
                return
            if self._check_game_updated():
                return

        if self._game_dir and self._snapshot and not self._snapshot.has_snapshot():
            from qfluentwidgets import (
                MessageBoxBase, SubtitleLabel, BodyLabel, CaptionLabel,
                CardWidget, StrongBodyLabel, setCustomStyleSheet,
            )
            from PySide6.QtGui import QFont
            from PySide6.QtWidgets import QVBoxLayout

            class _ScanDialog(MessageBoxBase):
                def __init__(self, parent):
                    super().__init__(parent)
                    self.widget.setMinimumWidth(520)

                    title = SubtitleLabel(tr("main.scan_needed"))
                    tf = title.font()
                    tf.setPixelSize(22)
                    tf.setWeight(QFont.Weight.Bold)
                    title.setFont(tf)
                    self.viewLayout.addWidget(title)
                    self.viewLayout.addSpacing(8)

                    desc = BodyLabel(
                        "Before using the mod manager, your game files need to be scanned "
                        "to create a clean baseline.")
                    df = desc.font()
                    df.setPixelSize(14)
                    desc.setFont(df)
                    desc.setWordWrap(True)
                    self.viewLayout.addWidget(desc)
                    self.viewLayout.addSpacing(12)

                    # Steps card
                    card = CardWidget(self)
                    card_layout = QVBoxLayout(card)
                    card_layout.setContentsMargins(24, 20, 24, 20)
                    card_layout.setSpacing(14)

                    steps_title = StrongBodyLabel(tr("main.verify_recommended"))
                    stf = steps_title.font()
                    stf.setPixelSize(14)
                    stf.setWeight(QFont.Weight.Bold)
                    steps_title.setFont(stf)
                    card_layout.addWidget(steps_title)

                    steps = [
                        ("1", "Open Steam"),
                        ("2", "Right-click Crimson Desert"),
                        ("3", "Properties  >  Installed Files"),
                        ("4", "Verify integrity of game files"),
                    ]
                    for num, text in steps:
                        step = BodyLabel(f"  {num}.  {text}")
                        sf = step.font()
                        sf.setPixelSize(14)
                        step.setFont(sf)
                        card_layout.addWidget(step)

                    self.viewLayout.addWidget(card)
                    self.viewLayout.addSpacing(12)

                    hint = CaptionLabel(tr("main.verify_hint"))
                    hf = hint.font()
                    hf.setPixelSize(13)
                    hint.setFont(hf)
                    self.viewLayout.addWidget(hint)

                    self.yesButton.setText(tr("main.scan_now"))
                    self.cancelButton.setText(tr("main.later"))

            box = _ScanDialog(self)
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

            self._log_activity("health", tr("activity.msg_auto_fixed_dirty"))
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
        url = info.get("url", "")
        logger.info("Update available: %s", tag)

        # 1. Show persistent update banner on all pages (except about)
        self._show_update_banner(tag, url)

        # 2. Add badge to About nav item in sidebar
        try:
            self.navigationInterface.widget("AboutPage").setShowBadge(True)
        except Exception:
            pass

        # 3. Update the About page with update info
        if hasattr(self, 'about_page'):
            self.about_page.set_update_status(tag, url, info.get("body", ""))

    def _show_update_banner(self, tag: str, url: str) -> None:
        """Show a persistent update banner at the top of the window."""
        from PySide6.QtWidgets import QHBoxLayout, QWidget
        from PySide6.QtGui import QFont, QDesktopServices, QColor, QPainter
        from PySide6.QtCore import QUrl
        from qfluentwidgets import BodyLabel, PushButton, isDarkTheme

        if hasattr(self, '_update_banner') and self._update_banner:
            self._update_banner.deleteLater()

        banner = QWidget(self)
        banner.setObjectName("updateBanner")
        banner.setFixedHeight(36)

        layout = QHBoxLayout(banner)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        label = BodyLabel(f"CDUMM {tag} is available", banner)
        lf = label.font()
        lf.setPixelSize(13)
        lf.setWeight(QFont.Weight.DemiBold)
        label.setFont(lf)
        layout.addWidget(label)
        layout.addStretch()

        btn = PushButton(tr("main.download"), banner)
        bf = btn.font()
        bf.setPixelSize(12)
        btn.setFont(bf)
        btn.setFixedHeight(26)
        btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        layout.addWidget(btn)

        # Store for theme updates and page switching
        self._update_banner = banner
        self._update_banner_tag = tag
        self._update_banner_url = url
        self._apply_update_banner_style()

        # Position it below the title bar
        banner.setParent(self)
        self._position_update_banner()
        banner.show()
        banner.raise_()

    def _apply_update_banner_style(self) -> None:
        """Apply theme-aware style to the update banner."""
        if not hasattr(self, '_update_banner') or not self._update_banner:
            return
        from qfluentwidgets import isDarkTheme
        if isDarkTheme():
            self._update_banner.setStyleSheet(
                "#updateBanner { background: #1A3A5C; border-bottom: 1px solid #2878D0; }")
        else:
            self._update_banner.setStyleSheet(
                "#updateBanner { background: #E8F0FE; border-bottom: 1px solid #2878D0; }")

    def _position_update_banner(self) -> None:
        """Position the update banner below the title bar."""
        if not hasattr(self, '_update_banner') or not self._update_banner:
            return
        # Hide on About page
        current = self.stackedWidget.currentWidget() if hasattr(self, 'stackedWidget') else None
        if current is getattr(self, 'about_page', None):
            self._update_banner.hide()
        else:
            self._update_banner.show()
        self._update_banner.setGeometry(0, self.titleBar.height(),
                                         self.width(), 36)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_update_banner()

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
        """Refresh UI after external DB change (debounced).
        Skip if a worker is active — it's our own writes, not external.
        """
        if self._active_worker:
            return
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
        """Called by qconfig.themeChanged — reapply ALL custom styles everywhere.

        Triggered by either in-app setTheme() or by SystemThemeListener
        picking up a Windows theme flip (wallpaper slideshow, system
        dark-mode toggle). The ZAIAC001 bug was caused by the listener
        never running — without it, Windows-flip events never reached
        this handler and the content stayed white-on-white.
        """
        # Switch sidebar logo between light/dark variant
        from qfluentwidgets import isDarkTheme
        logo_w = self.navigationInterface.widget("logo")
        if isinstance(logo_w, CdummLogoWidget):
            logo_w.set_theme_variant(isDarkTheme())

        # Force Qt to re-polish the main window and its chrome so the
        # FluentStyleSheet surface colors pick up the new theme. Without
        # this the background can lag one paint behind on some Windows
        # DPI configurations.
        try:
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
            if hasattr(self, "titleBar"):
                self.style().unpolish(self.titleBar)
                self.style().polish(self.titleBar)
                self.titleBar.update()
        except Exception as _e:
            logger.debug("Theme repolish failed: %s", _e)

        # Update banner theme
        self._apply_update_banner_style()

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

        # About page logo
        if hasattr(self, 'about_page') and hasattr(self.about_page, '_update_logo'):
            self.about_page._update_logo()

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
        import time as _t
        _t0 = _t.perf_counter()
        for page_name in ('paz_mods_page', 'asi_plugins_page', 'activity_page'):
            page = getattr(self, page_name, None)
            if page and hasattr(page, 'refresh'):
                try:
                    _pt0 = _t.perf_counter()
                    page.refresh()
                    _dt = (_t.perf_counter() - _pt0) * 1000
                    if _dt > 50:
                        logger.info("_refresh_all: %s took %.0fms", page_name, _dt)
                except Exception as e:
                    logger.debug("%s refresh error: %s", page_name, e)
        _total = (_t.perf_counter() - _t0) * 1000
        if _total > 100:
            logger.info("_refresh_all TOTAL: %.0fms", _total)
        # Stamp DB mtime so the poll timer doesn't re-trigger from our own writes
        self._stamp_db_mtime()

    def _show_import_errors(self, errors: list[str]) -> None:
        """Show import errors with a Copy Report button for bug reporting."""
        from qfluentwidgets import MessageBoxBase, SubtitleLabel, BodyLabel, PushButton
        from PySide6.QtWidgets import QTextEdit, QApplication
        from PySide6.QtGui import QFont

        class _ErrorDialog(MessageBoxBase):
            def __init__(self, errors, parent):
                super().__init__(parent)
                self.widget.setMinimumWidth(560)

                title = SubtitleLabel(f"{len(errors)} Import(s) Failed")
                tf = title.font()
                tf.setPixelSize(20)
                tf.setWeight(QFont.Weight.Bold)
                title.setFont(tf)
                self.viewLayout.addWidget(title)
                self.viewLayout.addSpacing(8)

                # Build diagnostic report
                import platform
                from cdumm import __version__
                lines = [
                    f"CDUMM v{__version__} — Import Error Report",
                    f"OS: {platform.platform()}",
                    f"Date: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    "",
                    f"{len(errors)} mod(s) failed:",
                ]
                for e in errors:
                    lines.append(f"  - {e}")
                self._report = "\n".join(lines)

                # Error list
                from qfluentwidgets import isDarkTheme
                preview = QTextEdit()
                preview.setReadOnly(True)
                preview.setPlainText(self._report)
                preview.setMinimumHeight(180)
                pf = preview.font()
                pf.setFamily("Consolas")
                pf.setPixelSize(12)
                preview.setFont(pf)
                if isDarkTheme():
                    preview.setStyleSheet(
                        "QTextEdit { background: #1C2028; color: #E2E8F0; "
                        "border: 1px solid #2D3340; border-radius: 6px; padding: 8px; }")
                else:
                    preview.setStyleSheet(
                        "QTextEdit { background: #FAFBFC; color: #1A202C; "
                        "border: 1px solid #E2E8F0; border-radius: 6px; padding: 8px; }")
                self.viewLayout.addWidget(preview)
                self.viewLayout.addSpacing(4)

                hint = BodyLabel(tr("main.copy_report"))
                hf = hint.font()
                hf.setPixelSize(12)
                hint.setFont(hf)
                self.viewLayout.addWidget(hint)

                # Copy button
                from PySide6.QtWidgets import QHBoxLayout
                btn_row = QHBoxLayout()
                copy_btn = PushButton(tr("main.copy_report_btn"))
                copy_btn.clicked.connect(lambda: (
                    QApplication.clipboard().setText(self._report),
                    InfoBar.success(title=tr("main.copied"), content=tr("main.report_copied"),
                                    duration=2000, position=InfoBarPosition.TOP, parent=self),
                ))
                btn_row.addWidget(copy_btn)
                btn_row.addStretch()
                self.viewLayout.addLayout(btn_row)

                self.yesButton.setText(tr("main.close"))
                self.cancelButton.hide()

        _ErrorDialog(errors, self).exec()

    def _run_diagnostic_then_show(self, errors: list[str], mod_path: Path, error: str) -> None:
        """Run diagnostic on a failed mod, then show results on Inspect page."""
        from PySide6.QtCore import QProcess
        import json as _json

        # Navigate to Inspect page and show analyzing state
        page = getattr(self, 'inspect_mod_page', None)
        if page:
            self.switchTo(page)
            page._clear_results()
            page._set_running(True)
            page._progress_detail.setText(f"Analyzing {mod_path.name}...")

        proc = QProcess(self)
        exe = sys.executable
        args = ["--worker", "diagnose", str(mod_path), str(self._game_dir),
                str(self._db.db_path), error]
        _buf = [""]

        def _on_stdout():
            data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            _buf[0] += data

        def _on_finished(exit_code, exit_status):
            proc.deleteLater()
            report = ""
            for line in _buf[0].split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                    if msg.get("type") == "done":
                        report = msg.get("report", "")
                except _json.JSONDecodeError:
                    pass
            self._show_import_errors_with_diagnostic(errors, report)

        proc.readyReadStandardOutput.connect(_on_stdout)
        proc.finished.connect(_on_finished)
        proc.start(exe, args)

    def _show_import_errors_with_diagnostic(self, errors: list[str], diagnostic: str) -> None:
        """Navigate to Inspect page and show diagnostic results as cards."""
        # Navigate to inspect page
        if hasattr(self, 'inspect_mod_page'):
            self.switchTo(self.inspect_mod_page)
            # Show the error + diagnostic as cards on the inspect page
            self.inspect_mod_page._clear_results()
            self.inspect_mod_page._set_running(False)

            # Error card
            for e in errors:
                parts = e.split(": ", 1)
                name = parts[0] if len(parts) > 1 else "Mod"
                err = parts[1] if len(parts) > 1 else e
                self.inspect_mod_page._add_result_card("Import Failed", f"{name}\n{err}", color="#BF616A")

            # Diagnostic cards
            if diagnostic:
                self.inspect_mod_page._add_diagnostic_card(diagnostic, errors[0].split(":")[0] if errors else "")

            self.inspect_mod_page._set_status("Import failed — diagnostic report below", "#BF616A")
        else:
            # Fallback: just show InfoBar
            error_text = "; ".join(e.split(": ", 1)[-1][:60] for e in errors[:3])
            InfoBar.error(
                title=f"{len(errors)} Import(s) Failed",
                content=error_text,
                duration=8000, position=InfoBarPosition.TOP, parent=self)

    def _diagnose_failed_mod(self, mod_path: Path, error: str) -> None:
        """Run diagnostic analysis on a failed mod import via QProcess.

        Shows a detailed report dialog with findings the user can share
        with the mod author.
        """
        from PySide6.QtCore import QProcess
        import json as _json

        proc = QProcess(self)
        exe = sys.executable
        args = ["--worker", "diagnose", str(mod_path), str(self._game_dir),
                str(self._db.db_path), error]
        _buf = [""]

        def _on_stdout():
            data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            _buf[0] += data
            while "\n" in _buf[0]:
                line, _buf[0] = _buf[0].split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if msg.get("type") == "done":
                    report = msg.get("report", "No diagnostic data available.")
                    self._show_diagnostic_report(mod_path.name, report)

        def _on_finished(exit_code, exit_status):
            proc.deleteLater()

        proc.readyReadStandardOutput.connect(_on_stdout)
        proc.finished.connect(_on_finished)
        proc.start(exe, args)

    def _show_diagnostic_report(self, mod_name: str, report: str) -> None:
        """Show a diagnostic report dialog for a failed mod."""
        from qfluentwidgets import MessageBoxBase, SubtitleLabel, PushButton
        from PySide6.QtWidgets import QTextEdit, QApplication, QHBoxLayout
        from PySide6.QtGui import QFont

        class _DiagDialog(MessageBoxBase):
            def __init__(self, name, report, parent):
                super().__init__(parent)
                self.widget.setMinimumWidth(620)

                title = SubtitleLabel(f"Diagnostic Report: {name}")
                tf = title.font()
                tf.setPixelSize(18)
                tf.setWeight(QFont.Weight.Bold)
                title.setFont(tf)
                self.viewLayout.addWidget(title)
                self.viewLayout.addSpacing(8)

                from qfluentwidgets import isDarkTheme
                preview = QTextEdit()
                preview.setReadOnly(True)
                preview.setPlainText(report)
                preview.setMinimumHeight(300)
                pf = preview.font()
                pf.setFamily("Consolas")
                pf.setPixelSize(12)
                preview.setFont(pf)
                if isDarkTheme():
                    preview.setStyleSheet(
                        "QTextEdit { background: #1C2028; color: #E2E8F0; "
                        "border: 1px solid #2D3340; border-radius: 6px; padding: 8px; }")
                else:
                    preview.setStyleSheet(
                        "QTextEdit { background: #FAFBFC; color: #1A202C; "
                        "border: 1px solid #E2E8F0; border-radius: 6px; padding: 8px; }")
                self.viewLayout.addWidget(preview)
                self.viewLayout.addSpacing(4)

                btn_row = QHBoxLayout()
                copy_btn = PushButton(tr("main.copy_report_btn"))
                copy_btn.clicked.connect(lambda: (
                    QApplication.clipboard().setText(report),
                    InfoBar.success(title=tr("main.copied"), content=tr("main.report_copied"),
                                    duration=2000, position=InfoBarPosition.TOP, parent=self),
                ))
                btn_row.addWidget(copy_btn)
                btn_row.addStretch()
                self.viewLayout.addLayout(btn_row)

                self.yesButton.setText(tr("main.close"))
                self.cancelButton.hide()

        _DiagDialog(mod_name, report, self).exec()

    def _on_import_dropped(self, path) -> None:
        """Handle file dropped on the mods page — queues for sequential import."""
        if not self._db or not self._game_dir:
            InfoBar.error(
                title=tr("main.not_ready"), content=tr("main.not_ready_msg"),
                duration=3000, position=InfoBarPosition.TOP, parent=self)
            return
        self._queue_import(Path(path) if not isinstance(path, Path) else path)

    def _queue_import(self, path: Path) -> None:
        """Add a path to the import queue. Processes sequentially."""
        if not hasattr(self, '_import_queue'):
            self._import_queue: list[Path] = []
        if not hasattr(self, '_import_errors'):
            self._import_errors: list[str] = []
        self._import_queue.append(path)
        logger.info("Queued for import: %s (queue size: %d, worker active: %s)",
                     path.name, len(self._import_queue), self._active_worker is not None)
        # If no import is running, start the first one
        if not self._active_worker:
            self._process_next_import()

    def _process_next_import(self) -> None:
        """Process the next item(s) in the import queue.

        If multiple items are queued, launches a single batch worker process
        for all of them (one Python startup instead of N).
        """
        if not hasattr(self, '_import_queue') or not self._import_queue:
            # Queue empty -- run diagnostics on failed mods, then show errors
            if hasattr(self, '_import_errors') and self._import_errors:
                errors = self._import_errors
                self._import_errors = []
                failed = getattr(self, '_failed_mod_paths', {})
                self._failed_mod_paths = {}
                if failed:
                    first_path, first_err = next(iter(failed.values()))
                    self._run_diagnostic_then_show(errors, first_path, first_err)
                else:
                    self._show_import_errors(errors)
            return

        # If multiple items queued, use batch import (single process)
        # But first, separate out multi-preset / multi-variant mods that need
        # user interaction. Archives are peeked via a lightweight namelist
        # check to avoid extracting every zip up-front.
        if len(self._import_queue) > 1:
            from cdumm.gui.preset_picker import find_json_presets, find_folder_variants
            batch = []
            deferred = []  # multi-preset mods that need dialog
            for p in self._import_queue:
                needs_dialog = False
                if p.is_dir():
                    presets = find_json_presets(p)
                    if len(presets) > 1:
                        needs_dialog = True
                    elif len(find_folder_variants(p)) >= 2:
                        needs_dialog = True
                elif p.is_file() and p.suffix.lower() in (".zip", ".7z", ".rar"):
                    if _archive_likely_needs_dialog(p):
                        needs_dialog = True
                if needs_dialog:
                    deferred.append(p)
                else:
                    batch.append(p)
            self._import_queue.clear()
            # Re-queue deferred mods — they'll be processed one-by-one after batch
            self._import_queue.extend(deferred)
            if batch:
                self._launch_batch_import(batch)
                return
            # If ALL items were deferred, fall through to single import

        path = self._import_queue.pop(0)
        remaining = len(self._import_queue)

        if remaining:
            logger.info("Importing %s (%d more queued)", path.name, remaining)

        self._import_with_prechecks(path)

    def _launch_batch_import(self, paths: list) -> None:
        """Launch a single worker process to import multiple mods at once."""
        import tempfile
        import json as _json

        if self._active_worker:
            # Re-queue and retry
            self._import_queue.extend(paths)
            QTimer.singleShot(500, self._process_next_import)
            return

        # Separate ASI mods from PAZ mods — ASI installs are instant (file copy)
        from cdumm.asi.asi_manager import AsiManager
        import tempfile as _tmpmod
        import shutil as _shmod
        asi_mgr = AsiManager(self._game_dir / "bin64")
        paz_paths = []
        asi_count = 0
        for p in paths:
            if asi_mgr.contains_asi(p) and not _has_game_content(p):
                # Extract ZIP/7z archives to a temp dir so asi_mgr.install() (which
                # only handles single .asi files or directories) can find them.
                extracted_tmp = None
                install_src = p
                try:
                    if p.is_file() and p.suffix.lower() == ".zip":
                        import zipfile as _zf
                        extracted_tmp = _tmpmod.mkdtemp(prefix="cdumm_batch_asi_")
                        with _zf.ZipFile(p) as zf:
                            zf.extractall(extracted_tmp)
                        install_src = Path(extracted_tmp)
                    elif p.is_file() and p.suffix.lower() == ".7z":
                        import py7zr as _p7
                        extracted_tmp = _tmpmod.mkdtemp(prefix="cdumm_batch_asi_")
                        with _p7.SevenZipFile(p, 'r') as zf:
                            zf.extractall(extracted_tmp)
                        install_src = Path(extracted_tmp)

                    installed = asi_mgr.install(install_src)
                    if installed:
                        asi_count += len([f for f in installed if f.endswith('.asi')])
                        # Version lookup still uses the original drop path so
                        # the NexusMods filename parser has the real name.
                        self._store_asi_version(p, installed)
                        logger.info("Batch: ASI installed directly: %s (%s)", p.name, installed)
                    else:
                        logger.warning("Batch: no ASI files extracted from %s", p.name)
                except Exception as e:
                    logger.warning("Batch: ASI install failed for %s: %s", p.name, e)
                finally:
                    if extracted_tmp:
                        _shmod.rmtree(extracted_tmp, ignore_errors=True)
            else:
                paz_paths.append(p)

        if asi_count:
            InfoBar.success(
                title=tr("main.import_complete"),
                content=f"{asi_count} ASI plugin(s) installed.",
                duration=3000, position=InfoBarPosition.TOP, parent=self)
            self._refresh_all()

        if not paz_paths:
            logger.info("Batch: all %d items were ASI mods, no PAZ import needed", len(paths))
            return

        total = len(paz_paths)
        logger.info("Batch import: %d PAZ mods in one process (%d ASI installed directly)",
                     total, asi_count)

        # Write paths to a temp file for the worker
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                          encoding="utf-8")
        for p in paz_paths:
            tmp.write(str(p) + "\n")
        tmp.close()
        self._batch_paths_file = tmp.name
        self._batch_paths = paz_paths

        tip = self._make_state_tooltip(f"Importing {total} mods...")
        self._active_progress = tip

        from PySide6.QtCore import QProcess
        proc = QProcess(self)
        self._active_worker = proc

        exe = sys.executable
        args = ["--worker", "import_batch",
                tmp.name, str(self._game_dir),
                str(self._db.db_path), str(self._deltas_dir)]

        _buf = [""]
        _batch_results = []
        _batch_errors = []

        def _on_stdout():
            raw = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            _buf[0] += raw
            while "\n" in _buf[0]:
                line, _buf[0] = _buf[0].split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "batch_progress":
                    idx = msg.get("index", 0)
                    name = msg.get("name", "")
                    try:
                        tip.setContent(f"({idx + 1}/{total}) {name}")
                    except RuntimeError:
                        pass
                elif mtype == "progress":
                    try:
                        tip.setContent(f"({msg.get('batch_index', 0) + 1}/{total}) {msg.get('msg', '')}")
                    except RuntimeError:
                        pass
                elif mtype == "batch_item":
                    if msg.get("error"):
                        _batch_errors.append(f"{msg.get('name', '?')}: {msg['error']}")
                    else:
                        _batch_results.append(msg)
                elif mtype == "done":
                    pass  # handled in _on_finished

        def _on_finished(exit_code, exit_status):
            tip.setContent(f"Completed! {len(_batch_results)}/{total} imported")
            tip.setState(True)
            proc.deleteLater()
            self._active_worker = None
            self._active_progress = None

            # Cleanup temp file
            try:
                import os
                os.unlink(self._batch_paths_file)
            except Exception:
                pass

            self._sync_db()

            # Install any staged ASI files from batch results. The worker
            # stages .asi + .ini + loader .dlls into a staging dir; hand the
            # whole dir to asi_mgr.install() so companion files come along,
            # then record the version via _store_asi_version.
            asi_total = 0
            try:
                from cdumm.asi.asi_manager import AsiManager
                _asi_mgr = AsiManager(self._game_dir / "bin64")
                from pathlib import Path as _P
                for item, bp in zip(_batch_results, self._batch_paths):
                    asi_staged = item.get("asi_staged", [])
                    if not asi_staged:
                        continue
                    # All staged paths share a parent (the staging dir).
                    staging_dirs = {_P(asp).parent for asp in asi_staged if _P(asp).exists()}
                    for sd in staging_dirs:
                        try:
                            installed = _asi_mgr.install(sd)
                        except Exception as _e:
                            logger.warning("Batch result: ASI install from %s failed: %s", sd, _e)
                            installed = []
                        if installed:
                            asi_total += sum(1 for f in installed if f.endswith('.asi'))
                            try:
                                self._store_asi_version(bp, installed)
                            except Exception:
                                pass
            except Exception as _e:
                logger.warning("Batch result: ASI post-install block failed: %s", _e)

            # Post-batch: detect configurable mods and set source_path
            # Build a map of mod name -> (mod_id, source_path) from results + paths
            try:
                from cdumm.engine.json_patch_handler import detect_json_patch
                from cdumm.gui.preset_picker import has_labeled_changes
                # Match results to paths by index (same order)
                for idx, bp in enumerate(self._batch_paths):
                    # Find the result with matching index
                    item = None
                    for r in _batch_results:
                        if r.get("_batch_idx") == idx:
                            item = r
                            break
                    # Fallback: match by name similarity
                    if not item:
                        for r in _batch_results:
                            rname = r.get("name", "")
                            if bp.stem in rname or rname in bp.stem:
                                item = r
                                break
                    if not item or not item.get("mod_id"):
                        continue
                    mod_id = item["mod_id"]
                    # Check if configurable
                    json_data = None
                    if bp.suffix.lower() == '.json':
                        json_data = detect_json_patch(bp)
                    elif bp.is_dir():
                        for f in bp.rglob("*.json"):
                            json_data = detect_json_patch(f)
                            if json_data:
                                break
                    elif bp.suffix.lower() == '.zip':
                        import zipfile
                        try:
                            with zipfile.ZipFile(bp) as zf:
                                for n in zf.namelist():
                                    if n.lower().endswith('.json'):
                                        import tempfile, json as _jj
                                        tmp_j = Path(tempfile.mktemp(suffix='.json'))
                                        tmp_j.write_bytes(zf.read(n))
                                        json_data = detect_json_patch(tmp_j)
                                        tmp_j.unlink(missing_ok=True)
                                        if json_data:
                                            break
                        except Exception:
                            pass
                    if json_data and has_labeled_changes(json_data):
                        self._db.connection.execute(
                            "UPDATE mods SET configurable = 1, source_path = ? WHERE id = ?",
                            (str(bp), mod_id))
                        self._db.connection.commit()
                        logger.info("Batch: marked mod %d as configurable (source=%s)", mod_id, bp.name)
            except Exception as e:
                logger.warning("Batch configurable detection failed: %s", e)

            # Store errors for diagnostic display
            if _batch_errors:
                if not hasattr(self, '_import_errors'):
                    self._import_errors = []
                self._import_errors.extend(_batch_errors)

            self._refresh_all()

            # Show result
            ok = len(_batch_results)
            fail = len(_batch_errors)
            if fail == 0:
                InfoBar.success(
                    title=tr("main.import_complete"),
                    content=f"{ok} mod(s) imported successfully.",
                    duration=5000, position=InfoBarPosition.TOP, parent=self)
            else:
                InfoBar.warning(
                    title=tr("main.import_complete"),
                    content=f"{ok} imported, {fail} failed.",
                    duration=5000, position=InfoBarPosition.TOP, parent=self)

            self._log_activity("import", tr("activity.msg_batch_imported", ok=ok, fail=fail))
            # Process any remaining items (shouldn't be any, but safety)
            QTimer.singleShot(100, self._process_next_import)

        def _on_stderr():
            raw = proc.readAllStandardError().data().decode("utf-8", errors="replace")
            if raw.strip():
                logger.info("Batch import worker stderr: %s", raw.strip()[:500])

        proc.readyReadStandardOutput.connect(_on_stdout)
        proc.readyReadStandardError.connect(_on_stderr)
        proc.finished.connect(_on_finished)
        proc.start(exe, args)
        logger.info("Batch import QProcess started: PID %s", proc.processId())

    def _import_with_prechecks(self, path: Path) -> None:
        """Run pre-import checks (main thread, blocking), then launch ImportWorker."""
        # Safety: never start a new import while one is running
        if self._active_worker:
            logger.warning("Import blocked — worker still active, re-queuing %s", path.name)
            if not hasattr(self, '_import_queue'):
                self._import_queue = []
            self._import_queue.insert(0, path)  # put it back at front
            QTimer.singleShot(500, self._process_next_import)
            return
        logger.info("Import pre-checks for: %s", path)
        self._original_drop_path = path  # saved for version extraction after import
        existing_mod_id = None

        # ── 1. ASI detection ──────────────────────────────────────────
        from cdumm.asi.asi_manager import AsiManager
        asi_mgr = AsiManager(self._game_dir / "bin64")
        if asi_mgr.contains_asi(path) and not _has_game_content(path):
            # Pure ASI mod — install directly (no PAZ content)
            self._install_asi_mod(path, asi_mgr)
            self._process_next_import()
            return
        # Mixed ZIPs (ASI + PAZ) go through worker — ASI files are staged
        # and installed from the result handler after worker completes

        # ── 2. Snapshot check ─────────────────────────────────────────
        if not self._snapshot or not self._snapshot.has_snapshot():
            if not _is_standalone_paz_mod(path):
                InfoBar.error(
                    title=tr("main.no_snapshot"),
                    content=tr("main.no_snapshot_msg"),
                    duration=5000, position=InfoBarPosition.TOP, parent=self)
                self._process_next_import()
                return

        # ── 3. Existing mod detection ─────────────────────────────────
        if self._mod_manager:
            existing = self._find_existing_mod(path)
            if existing:
                mid, mname, match_level = existing
                # Near matches (token overlap ≥ 0.6 but not exact) route
                # through a pre-prompt so users never silently overwrite
                # a mod that shares words with the new one. See plan 2.2.
                treat_as_dup = True
                if match_level == "near":
                    near_box = MessageBox(
                        "Similar Mod Detected",
                        f"The mod you're importing looks similar to "
                        f"'{mname}' which is already installed.\n\n"
                        f"Is this an update to that mod, or a separate mod?",
                        self)
                    near_box.yesButton.setText("Update existing")
                    near_box.cancelButton.setText("Import as new")
                    if not near_box.exec():
                        # 'Import as new' — bypass the dup flow entirely
                        treat_as_dup = False
                if treat_as_dup:
                    # Get installed mod's version
                    installed_version = None
                    for m in self._mod_manager.list_mods():
                        if m["id"] == mid:
                            installed_version = m.get("version") or ""
                            break

                    # Get dropped mod's version
                    drop_version = self._get_drop_version(path)

                    # Compare versions
                    if installed_version and drop_version and installed_version == drop_version:
                        # Same version — skip silently with toast
                        InfoBar.info(
                            title=tr("infobar.skipped"),
                            content=tr("infobar.skipped_msg", name=mname, version=drop_version),
                            duration=3000, position=InfoBarPosition.TOP, parent=self)
                        logger.info("Skipped duplicate: %s v%s", mname, drop_version)
                        self._process_next_import()
                        return

                    # Different version — determine if update or downgrade
                    if installed_version and drop_version and drop_version < installed_version:
                        # Downgrade
                        box = MessageBox(
                            "Downgrade Mod?",
                            f"'{mname}' v{installed_version} is installed.\n\n"
                            f"You're importing an older version (v{drop_version}).\n"
                            f"Replace with the older version?",
                            self)
                    else:
                        # Update (or unknown versions)
                        old_ver = f" v{installed_version}" if installed_version else ""
                        new_ver = f" v{drop_version}" if drop_version else ""
                        box = MessageBox(
                            "Update Mod?",
                            f"'{mname}'{old_ver} is already installed.\n\n"
                            f"Update to{new_ver}? (Old version will be removed)",
                            self)

                    if box.exec():
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
            def _has_game_files(d: Path) -> bool:
                """Check if dir has game files at any depth (direct or inside files/)."""
                for child in d.iterdir():
                    if child.is_dir():
                        if (child.name.isdigit() and len(child.name) == 4) or child.name == "meta":
                            return True
                        # Check one level deeper (e.g., variant/files/0000/)
                        if child.name.lower() in ("files", "data", "gamedata"):
                            for grandchild in child.iterdir():
                                if grandchild.is_dir() and (
                                    (grandchild.name.isdigit() and len(grandchild.name) == 4)
                                    or grandchild.name == "meta"):
                                    return True
                return False

            variants = []
            for sub in sorted(path.iterdir()):
                if sub.is_dir() and not sub.name.startswith('.') and not sub.name.startswith('_'):
                    if _has_game_files(sub):
                        variants.append(sub.name)
            if len(variants) > 1:
                from qfluentwidgets import (
                    MessageBoxBase, SingleDirectionScrollArea,
                    SubtitleLabel, CaptionLabel,
                )
                from PySide6.QtWidgets import QRadioButton, QFrame
                from PySide6.QtGui import QFont as _QF

                class _VariantDialog(MessageBoxBase):
                    def __init__(self, variants, parent):
                        super().__init__(parent)
                        self.chosen = None
                        self.widget.setMinimumWidth(480)

                        title = SubtitleLabel(tr("main.choose_variant"))
                        tf = title.font()
                        tf.setPixelSize(20)
                        tf.setWeight(_QF.Weight.Bold)
                        title.setFont(tf)
                        self.viewLayout.addWidget(title)
                        self.viewLayout.addSpacing(4)

                        hint = CaptionLabel(f"This mod has {len(variants)} variants. Choose one:")
                        hf = hint.font()
                        hf.setPixelSize(13)
                        hint.setFont(hf)
                        self.viewLayout.addWidget(hint)
                        self.viewLayout.addSpacing(8)

                        from qfluentwidgets import isDarkTheme
                        scroll = SingleDirectionScrollArea(
                            orient=Qt.Orientation.Vertical)
                        scroll.setWidgetResizable(True)
                        scroll.setFrameShape(
                            SingleDirectionScrollArea.Shape.NoFrame)
                        scroll.setMaximumHeight(300)
                        container = QWidget()
                        if isDarkTheme():
                            container.setStyleSheet(
                                "QWidget { background: #1C2028; } "
                                "QRadioButton { color: #E2E8F0; padding: 12px; spacing: 8px; }")
                        else:
                            container.setStyleSheet(
                                "QWidget { background: #FAFBFC; } "
                                "QRadioButton { color: #1A202C; padding: 12px; spacing: 8px; }")
                        from PySide6.QtWidgets import QVBoxLayout
                        rl = QVBoxLayout(container)
                        rl.setContentsMargins(8, 8, 8, 8)
                        rl.setSpacing(4)

                        self._radios = []
                        for i, v in enumerate(variants):
                            # Clean up underscores for display
                            display = v.replace("_", " ")
                            r = QRadioButton(display)
                            rf = r.font()
                            rf.setPixelSize(14)
                            r.setFont(rf)
                            if i == 0:
                                r.setChecked(True)
                            self._radios.append((r, v))
                            rl.addWidget(r)
                        rl.addStretch()
                        scroll.setWidget(container)
                        self.viewLayout.addWidget(scroll)

                        self.yesButton.setText(tr("main.install"))
                        self.yesButton.clicked.disconnect()
                        self.yesButton.clicked.connect(self._on_accept)
                        self.cancelButton.setText(tr("main.cancel"))

                    def _on_accept(self):
                        for r, v in self._radios:
                            if r.isChecked():
                                self.chosen = v
                                break
                        self.accept()

                dialog = _VariantDialog(variants, self)
                if dialog.exec() and dialog.chosen:
                    path = path / dialog.chosen
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
                # Mark as configurable so user can re-pick preset later
                self._configurable_source = str(path)
                dialog = PresetPickerDialog(presets, self)
                if dialog.exec() and dialog.selected_presets:
                    selected = dialog.selected_presets
                    if len(selected) > 1:
                        # Multi-select → ONE mod row with ALL presets from
                        # the archive stored under variants/. Ticked presets
                        # start enabled; the rest are off but accessible via
                        # the cog. Skip the normal import-worker path.
                        from cdumm.engine.variant_handler import import_multi_variant
                        try:
                            game_dir = self._game_dir
                            mods_dir = game_dir / "CDMods" / "mods"
                            ticked_paths = {p for p, _d in selected}
                            # Pass EVERY detected preset so the cog can
                            # toggle the full set later.
                            result = import_multi_variant(
                                presets, path, game_dir, mods_dir, self._db,
                                initial_selection=ticked_paths)
                            if result and hasattr(self, "_activity_log") and self._activity_log:
                                enabled_ct = sum(1 for v in result["variants"] if v["enabled"])
                                self._activity_log.log(
                                    "import",
                                    f"Imported variant mod: {result['mod_name']}",
                                    f"{len(result['variants'])} variants "
                                    f"({enabled_ct} enabled)")
                            if result:
                                self._refresh_all()
                        except Exception as e:
                            logger.error(
                                "multi-variant import failed: %s", e, exc_info=True)
                        if tmp_extract:
                            import shutil
                            shutil.rmtree(tmp_extract, ignore_errors=True)
                        self._process_next_import()
                        return
                    # Single-select → fall through the normal import worker
                    path = selected[0][0]
                else:
                    self._configurable_source = None
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

        # ── 5b. Folder variant picker ─────────────────────────────────
        try:
            from cdumm.gui.preset_picker import find_folder_variants, FolderVariantDialog
            if path.is_dir():
                folder_vars = find_folder_variants(path)
                if len(folder_vars) >= 2:
                    fv_dialog = FolderVariantDialog(folder_vars, self)
                    result = fv_dialog.exec()
                    if result and fv_dialog.selected_path:
                        path = fv_dialog.selected_path
                        logger.info("User selected folder variant: %s", path.name)
                    else:
                        self._process_next_import()
                        return
        except Exception as e:
            logger.debug("Folder variant check failed: %s", e)

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
        """Launch import in a SEPARATE PROCESS via QProcess.

        Using QProcess instead of QThread completely eliminates GIL contention —
        the subprocess has its own GIL so the GUI thread is never starved.
        Progress is streamed as JSON lines on stdout.
        """
        logger.info("Launching import subprocess: %s", path)
        from PySide6.QtCore import QProcess
        import json as _json

        remaining = len(getattr(self, '_import_queue', []))
        suffix = f" ({remaining} more queued)" if remaining else ""
        tip = self._make_state_tooltip(f"Importing {path.name}...{suffix}")
        self._active_progress = tip

        proc = QProcess(self)
        self._active_worker = proc  # reuse guard flag

        # Build command: the exe calls itself with --worker
        exe = sys.executable
        args = ["--worker", "import",
                str(path), str(self._game_dir),
                str(self._db.db_path), str(self._deltas_dir)]
        if existing_mod_id is not None:
            args.append(str(existing_mod_id))

        # Buffer for partial JSON lines
        _buf = [""]

        def _on_stdout():
            data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            _buf[0] += data
            while "\n" in _buf[0]:
                line, _buf[0] = _buf[0].split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if msg.get("type") == "progress":
                    try:
                        tip.setContent(f"{msg.get('msg', '')} ({msg.get('pct', 0)}%)")
                    except RuntimeError:
                        pass
                elif msg.get("type") == "done":
                    self._import_result_name = msg.get("name", path.stem)
                    self._import_result_error = None
                    self._import_result_asi_staged = msg.get("asi_staged", [])
                elif msg.get("type") == "error":
                    self._import_result_name = path.stem
                    self._import_result_error = msg.get("msg", "Unknown error")

        def _on_finished(exit_code, exit_status):
            tip.setContent(tr("progress.completed"))
            tip.setState(True)
            proc.deleteLater()
            self._active_worker = None
            self._active_progress = None

            err = getattr(self, '_import_result_error', None)
            name = getattr(self, '_import_result_name', path.stem)

            if err:
                if not hasattr(self, '_import_errors'):
                    self._import_errors = []
                self._import_errors.append(f"{path.name}: {err}")
                # Store path for diagnostic analysis when errors are shown
                if not hasattr(self, '_failed_mod_paths'):
                    self._failed_mod_paths = {}
                self._failed_mod_paths[path.name] = (path, err)
                QTimer.singleShot(100, self._process_next_import)
                return

            # ── Post-import actions (main thread, main DB connection) ──
            self._sync_db()

            # Clean temp dirs from pre-checks
            tmp = getattr(self, '_pending_tmp_cleanup', None)
            if tmp:
                import shutil
                shutil.rmtree(str(tmp), ignore_errors=True)
                self._pending_tmp_cleanup = None

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

            # Post-import: NexusMods mod ID from filename
            nexus_id, nexus_file_ver = _parse_nexus_filename(path.stem)
            if nexus_id:
                try:
                    self._db.connection.execute(
                        "UPDATE mods SET nexus_mod_id = ?, nexus_file_id = ? "
                        "WHERE id = (SELECT MAX(id) FROM mods)",
                        (nexus_id, nexus_file_ver))
                    self._db.connection.commit()
                    logger.info("Stored NexusMods ID: mod=%d file=%s", nexus_id, nexus_file_ver)
                except Exception:
                    pass

            # Post-import: store original drop name + extract version
            try:
                mod_id = self._db.connection.execute(
                    "SELECT MAX(id) FROM mods").fetchone()[0]
                orig = getattr(self, '_original_drop_path', None)
                drop_name = orig.name if orig else path.name
                self._db.connection.execute(
                    "UPDATE mods SET drop_name = ? WHERE id = ?",
                    (drop_name, mod_id))
                drop_ver = self._get_drop_version(orig) if orig else ""
                if not drop_ver:
                    drop_ver = self._get_drop_version(path)
                if not drop_ver:
                    row = self._db.connection.execute(
                        "SELECT version FROM mods WHERE id = ?", (mod_id,)).fetchone()
                    drop_ver = row[0] if row and row[0] else ""
                if drop_ver:
                    self._db.connection.execute(
                        "UPDATE mods SET version = ? WHERE id = ?",
                        (drop_ver, mod_id))
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

            # Install staged ASI files from mixed ZIP import
            asi_staged = getattr(self, '_import_result_asi_staged', [])
            asi_count = 0
            if asi_staged:
                try:
                    from cdumm.asi.asi_manager import AsiManager
                    _asi_mgr = AsiManager(self._game_dir / "bin64")
                    from pathlib import Path as _P
                    for asi_path in asi_staged:
                        p = _P(asi_path)
                        if p.exists() and p.suffix.lower() == ".asi":
                            import shutil
                            shutil.copy2(str(p), str(_asi_mgr._bin64 / p.name))
                            asi_count += 1
                        elif p.exists() and p.suffix.lower() == ".ini":
                            import shutil
                            shutil.copy2(str(p), str(_asi_mgr._bin64 / p.name))
                    logger.info("Installed %d ASI plugin(s) from mixed ZIP", asi_count)
                except Exception as e:
                    logger.warning("Failed to install staged ASI files: %s", e)

            self._refresh_all()
            if asi_count > 0:
                InfoBar.success(
                    title=tr("main.import_complete"),
                    content=f"{name} imported + {asi_count} ASI plugin(s) installed.",
                    duration=4000, position=InfoBarPosition.TOP, parent=self)
            else:
                InfoBar.success(
                    title=tr("main.import_complete"),
                    content=tr("main.import_success", name=name),
                    duration=4000, position=InfoBarPosition.TOP, parent=self)
            self._log_activity("import", tr("activity.msg_imported_mod", name=name))
            QTimer.singleShot(100, self._process_next_import)

        def _on_stderr():
            data = proc.readAllStandardError().data().decode("utf-8", errors="replace")
            if data.strip():
                logger.info("Import worker stderr: %s", data.strip()[:500])

        proc.readyReadStandardOutput.connect(_on_stdout)
        proc.readyReadStandardError.connect(_on_stderr)
        proc.finished.connect(_on_finished)
        proc.start(exe, args)
        logger.info("Import QProcess started: PID %s", proc.processId())

    def _install_asi_mod(self, path: Path, asi_mgr=None) -> None:
        """Install an ASI mod by copying .asi/.ini files to bin64/."""
        import tempfile
        if asi_mgr is None:
            from cdumm.asi.asi_manager import AsiManager
            asi_mgr = AsiManager(self._game_dir / "bin64")

        if not asi_mgr.has_loader():
            InfoBar.warning(
                title=tr("infobar.asi_loader_missing"),
                content=tr("main.no_asi_loader"),
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
            # Save version sidecar from original drop path
            orig = getattr(self, '_original_drop_path', path)
            drop_ver = self._get_drop_version(orig)
            if drop_ver:
                bin64 = self._game_dir / "bin64"
                for asi_name in installed:
                    if asi_name.endswith('.asi'):
                        ver_file = bin64 / (asi_name.replace('.asi', '.version'))
                        try:
                            ver_file.write_text(drop_ver, encoding='utf-8')
                        except Exception:
                            pass

            # Store version in asi_plugin_state DB
            self._store_asi_version(orig, installed)

            InfoBar.success(
                title=tr("infobar.asi_installed"),
                content=tr("infobar.asi_installed_msg", files=", ".join(installed)),
                duration=5000, position=InfoBarPosition.TOP, parent=self)
            logger.info("ASI install success: %s", installed)
            # Refresh ASI page
            if hasattr(self, 'asi_plugins_page'):
                self.asi_plugins_page.refresh()
        else:
            InfoBar.warning(
                title=tr("main.no_asi_files"), content=tr("main.no_asi_files_msg"),
                duration=5000, position=InfoBarPosition.TOP, parent=self)

    def _store_asi_version(self, source_path: Path, installed_files: list[str]) -> None:
        """Extract version + NexusMods mod_id from the drop path and store them
        on each installed ASI plugin row in asi_plugin_state."""
        if not self._db:
            return
        version = self._get_drop_version(source_path)
        # Parse NexusMods mod_id from the drop filename (used for update checks)
        try:
            stem = source_path.name if source_path.is_dir() else source_path.stem
            nexus_id, _ = _parse_nexus_filename(stem)
        except Exception:
            nexus_id = None
        if not version and not nexus_id:
            return
        for fname in installed_files:
            if not fname.endswith('.asi'):
                continue
            plugin_name = fname.replace('.asi', '')
            try:
                # Always INSERT OR IGNORE first so a row exists, then UPDATE
                # the individual columns only when we have real values.
                self._db.connection.execute(
                    "INSERT OR IGNORE INTO asi_plugin_state (name) VALUES (?)",
                    (plugin_name,))
                if version:
                    self._db.connection.execute(
                        "UPDATE asi_plugin_state SET version = ? WHERE name = ?",
                        (version, plugin_name))
                if nexus_id:
                    self._db.connection.execute(
                        "UPDATE asi_plugin_state SET nexus_mod_id = ? WHERE name = ?",
                        (nexus_id, plugin_name))
                self._db.connection.commit()
                logger.info("Stored ASI metadata: %s version=%s nexus_mod_id=%s",
                            plugin_name, version, nexus_id)
            except Exception as e:
                logger.warning("Failed to store ASI metadata: %s", e)

    @staticmethod
    def _get_drop_version(path: Path) -> str:
        """Extract version string from a dropped mod (modinfo.json, JSON patch, or folder name)."""
        import re
        # Try modinfo.json
        try:
            from cdumm.engine.import_handler import _read_modinfo
            if path.is_dir():
                info = _read_modinfo(path)
                if info and info.get("version"):
                    return info["version"]
        except Exception:
            pass
        # Try JSON patch version field
        try:
            from cdumm.engine.json_patch_handler import detect_json_patch
            target = path
            if path.is_dir():
                jsons = list(path.glob("*.json"))
                if jsons:
                    target = jsons[0]
            if target.suffix.lower() == ".json":
                jp = detect_json_patch(target)
                if jp and jp.get("version"):
                    return jp["version"]
        except Exception:
            pass
        # Try NexusMods filename format: "ModName-1181-1-1776169023"
        # The file version part is between mod_id and timestamp
        nexus_id, nexus_ver = _parse_nexus_filename(path.name if path.is_dir() else path.stem)
        if nexus_id and nexus_ver:
            return nexus_ver

        # Try "vN.N.N" or "vN" pattern in folder/file name
        # Use .name for dirs (no extension to strip), .stem for files
        name = path.name if path.is_dir() else path.stem
        match = re.search(r'[vV](\d+(?:\.\d+)*)', name)
        if match:
            return match.group(1)
        return ""

    def _find_existing_mod(self, path: Path) -> tuple[int, str, str] | None:
        """Check if a dropped mod matches an already-installed mod.

        Returns ``(mod_id, mod_name, match_level)`` where ``match_level``
        is ``"exact"`` (prettified names equal → default to Update) or
        ``"near"`` (Jaccard token overlap ≥ 0.6 → show Update / Add as
        new / Cancel). Returns ``None`` when no mod meets either bar.

        Replaces the earlier substring match which false-matched short
        names (e.g. ``"Infinite Stamina"``) against longer scoped names
        (``"Infinite Stamina (All Skills Horse Spirit)"``).
        """
        if not self._mod_manager:
            return None

        from cdumm.engine.mod_matching import is_same_mod, token_overlap_ratio

        drop_name = path.stem
        # Try to read mod name from modinfo.json or JSON patch
        try:
            from cdumm.engine.import_handler import _read_modinfo
            if path.is_dir():
                modinfo = _read_modinfo(path)
                if modinfo and modinfo.get("name"):
                    drop_name = modinfo["name"]
        except Exception:
            pass
        try:
            from cdumm.engine.json_patch_handler import detect_json_patch
            if path.suffix.lower() == ".json":
                jp = detect_json_patch(path)
                if jp and jp.get("name"):
                    drop_name = jp["name"]
        except Exception:
            pass

        mods = list(self._mod_manager.list_mods())
        # Prefer exact matches over near matches
        for m in mods:
            if is_same_mod(drop_name, m["name"]):
                return (m["id"], m["name"], "exact")
        for m in mods:
            if token_overlap_ratio(drop_name, m["name"]) >= 0.6:
                return (m["id"], m["name"], "near")
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
                title=tr("main.busy"), content=tr("main.busy_msg"),
                duration=3000, position=InfoBarPosition.TOP, parent=self)
            return
        if not self._db or not self._game_dir:
            InfoBar.error(
                title=tr("main.not_ready"), content=tr("main.not_ready_msg"),
                duration=3000, position=InfoBarPosition.TOP, parent=self)
            return

        if not self._check_game_running():
            return

        logger.info("Apply requested")
        tip = self._make_state_tooltip("Applying mods...")

        def on_apply_done(msgs):
            # Check for errors
            errors = [m["msg"] for m in msgs if m.get("type") == "error"]
            if errors:
                InfoBar.error(title=tr("main.apply_failed"), content=errors[-1],
                              duration=-1, position=InfoBarPosition.TOP, parent=self)
                return
            # Soft warnings (mount-time fallback, empty overlay) surface
            # as InfoBar.warning so the user notices even when apply
            # technically succeeds. Task 1.2 + 1.3.
            warnings = [m["msg"] for m in msgs if m.get("type") == "warning"]
            if warnings:
                InfoBar.warning(
                    title=tr("main.apply_warnings"),
                    content="\n".join(warnings),
                    duration=-1, position=InfoBarPosition.TOP, parent=self)
            self._sync_db()
            self._snapshot_applied_state()
            logger.info("on_apply_done: applied_state has %d entries, %d enabled",
                        len(self._applied_state),
                        sum(1 for v in self._applied_state.values() if v))
            from cdumm.engine.import_handler import clear_assigned_dirs
            clear_assigned_dirs()
            self._log_activity("apply", tr("activity.msg_applied"))
            # Handle pending removals from batch uninstall
            pending = getattr(self, '_pending_removals', [])
            if pending:
                for mid in pending:
                    try:
                        self._mod_manager.remove_mod(mid)
                    except Exception:
                        pass
                self._pending_removals = []
                self._log_activity("remove", tr("activity.msg_removed_after_revert", count=len(pending)))
            self._refresh_all()
            self._post_apply_verify()

        self._run_qprocess(
            ["apply", str(self._game_dir), str(self._vanilla_dir),
             str(self._db.db_path), "1"],
            tip, on_apply_done)

    def _on_revert(self) -> None:
        """Revert all game files to vanilla state."""
        if not self._db or not self._game_dir:
            return
        if self._active_worker:
            InfoBar.warning(
                title=tr("main.busy"), content=tr("main.busy_msg"),
                duration=3000, position=InfoBarPosition.TOP, parent=self)
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

        tip = self._make_state_tooltip("Reverting to vanilla...")

        def on_revert_msg(msg):
            if msg.get("type") == "warning":
                self._show_revert_warning(msg["msg"])

        def on_revert_done(msgs):
            errors = [m["msg"] for m in msgs if m.get("type") == "error"]
            if errors:
                InfoBar.error(title=tr("main.revert_failed"), content=errors[-1],
                              duration=-1, position=InfoBarPosition.TOP, parent=self)
                return
            self._on_revert_finished()

        self._run_qprocess(
            ["revert", str(self._game_dir), str(self._vanilla_dir),
             str(self._db.db_path)],
            tip, on_revert_done, on_msg=on_revert_msg)

    def _show_revert_warning(self, msg: str) -> None:
        MessageBox(tr("dialog.revert_incomplete"), msg, self).exec()

    def _on_revert_finished(self) -> None:
        """Handle revert completion -- disable all mods to match vanilla state."""
        if self._mod_manager:
            for mod in self._mod_manager.list_mods():
                if mod["enabled"]:
                    self._mod_manager.set_enabled(mod["id"], False)
        self._refresh_all()
        self._snapshot_applied_state()
        self._log_activity("revert", tr("activity.msg_reverted_vanilla"))
        InfoBar.success(
            title=tr("infobar.reverted_to_vanilla"),
            content=tr("main.reverted"),
            duration=5000, position=InfoBarPosition.TOP, parent=self)
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
                title=tr("infobar.cleanup_complete"),
                content=tr("infobar.cleanup_complete_msg", count=deleted, size_mb=f"{total_mb:.0f}"),
                duration=5000, position=InfoBarPosition.TOP, parent=self)

    def _on_uninstall_mod(self, mod_id: int) -> None:
        """Handle uninstall: apply to revert game files, then remove mod from DB."""
        if self._active_worker:
            InfoBar.warning(
                title=tr("main.busy"), content=tr("main.busy_msg"),
                duration=3000, position=InfoBarPosition.TOP, parent=self)
            return

        tip = self._make_state_tooltip("Reverting mod files...")

        def on_uninstall_apply_done(msgs):
            errors = [m["msg"] for m in msgs if m.get("type") == "error"]
            if errors:
                InfoBar.error(title=tr("main.uninstall_failed"), content=errors[-1],
                              duration=-1, position=InfoBarPosition.TOP, parent=self)
                return
            self._sync_db()
            if self._mod_manager:
                try:
                    details = self._mod_manager.get_mod_details(mod_id)
                    name = details["name"] if details else str(mod_id)
                except Exception:
                    name = str(mod_id)
                self._mod_manager.remove_mod(mod_id)
                self._log_activity("uninstall", tr("activity.msg_uninstalled_mod", name=name))
            self._snapshot_applied_state()
            self._refresh_all()
            InfoBar.success(
                title=tr("infobar.mod_uninstalled"),
                content=tr("main.mod_removed"),
                duration=4000, position=InfoBarPosition.TOP, parent=self)

        self._run_qprocess(
            ["apply", str(self._game_dir), str(self._vanilla_dir),
             str(self._db.db_path), "1"],
            tip, on_uninstall_apply_done)

    # ------------------------------------------------------------------
    # Post-apply verification
    # ------------------------------------------------------------------

    def _post_apply_verify(self) -> None:
        """Deep verification after Apply -- checks PAPGT/PAMT integrity.

        Runs on the GUI thread — keep it fast or skip heavy checks.
        """
        if not self._game_dir or not self._db:
            return
        import time as _t
        _t0 = _t.perf_counter()
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
                            # Skip vanilla placeholder dirs (< 0036) that don't exist on disk
                            try:
                                if int(dir_name) >= 36:
                                    issues.append(("PAPGT", f"Missing directory {dir_name}"))
                            except (ValueError, TypeError):
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

        # 3. PAMT bounds checking skipped — runs on GUI thread and is too slow
        #    for large installations. The apply engine already ensures correct
        #    offsets/sizes during PAZ composition.

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

        _dt = _t.perf_counter() - _t0
        logger.info("Post-apply verify took %.1fs, found %d issue(s)", _dt, len(issues))

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
                               tr("activity.msg_post_apply_issues", count=len(issues)),
                               "; ".join(f"[{s}] {d}" for s, d in issues[:5]))
        else:
            InfoBar.success(
                title=tr("infobar.apply_complete"),
                content=tr("main.apply_success"),
                duration=5000, position=InfoBarPosition.TOP, parent=self)
            logger.info("Post-apply verification passed")
            # Recheck NexusMods for updates after apply
            QTimer.singleShot(2000, self._run_nexus_update_check)

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
                title=tr("main.game_not_found"), content=tr("main.game_not_found_msg"),
                duration=5000, position=InfoBarPosition.TOP, parent=self)
            return
        try:
            from cdumm.storage.game_finder import is_steam_install, is_xbox_install
            if is_steam_install(self._game_dir):
                # Launch through Steam for proper overlay/DRM.
                # get_steam_app_id reads steam_appid.txt / appmanifest_*.acf
                # and falls back to the verified Crimson Desert AppID (3321460).
                # Previously this hard-coded the wrong AppID which caused Steam
                # to show "Game configuration unavailable".
                import os
                from cdumm.engine.game_monitor import get_steam_app_id
                app_id = get_steam_app_id(self._game_dir)
                os.startfile(f"steam://rungameid/{app_id}")
            elif is_xbox_install(self._game_dir):
                # Xbox Game Pass — launch through the Xbox app
                import os
                os.startfile("shell:AppsFolder\\PearlAbyss.CrimsonDesert_8wekyb3d8bbwe!Game")
            else:
                subprocess.Popen([str(exe)], cwd=str(self._game_dir / "bin64"))
            InfoBar.success(
                title=tr("main.game_launched"), content=tr("main.game_launched_msg"),
                duration=3000, position=InfoBarPosition.TOP, parent=self)
            self.showMinimized()
        except Exception as e:
            # Fallback: try direct exe launch
            try:
                subprocess.Popen([str(exe)], cwd=str(self._game_dir / "bin64"))
                InfoBar.success(
                    title=tr("main.game_launched"), content=tr("main.game_launched_msg"),
                    duration=3000, position=InfoBarPosition.TOP, parent=self)
                self.showMinimized()
            except Exception as e2:
                InfoBar.error(
                    title=tr("infobar.launch_failed"), content=str(e2),
                    duration=5000, position=InfoBarPosition.TOP, parent=self)

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
        self.asi_plugins_page.set_managers(game_dir=self._game_dir, db=self._db)
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
                title=tr("main.busy"), content=tr("main.busy_msg"),
                duration=3000, position=InfoBarPosition.TOP, parent=self)
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
        from cdumm.storage.config import default_export_dir

        default_path = default_export_dir(self._db) / "cdumm_modlist.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Mod List", str(default_path), "JSON Files (*.json)")
        if not path:
            return
        from cdumm.engine.mod_list_io import export_mod_list
        count = export_mod_list(self._db, Path(path))
        InfoBar.success(
            title=tr("infobar.export_complete"),
            content=tr("infobar.export_complete_msg", count=count, file=Path(path).name),
            duration=5000, position=InfoBarPosition.TOP, parent=self)

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
                title=tr("infobar.import_empty"),
                content=tr("main.no_mods_in_file"),
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
        """Create a StateToolTip centered horizontally in the window.

        The default StateToolTip spins a SYNC icon at 20 fps via a 50ms timer
        and composites through QGraphicsOpacityEffect — both extremely expensive.
        We disable the spinner and remove the opacity effect to keep the GUI
        smooth while workers are running.
        """
        tip = StateToolTip(title, "Starting...", self)
        # Kill the 50ms spinner animation — 20 repaints/sec with compositing
        tip.rotateTimer.stop()
        # Remove QGraphicsOpacityEffect — forces offscreen render every repaint
        tip.setGraphicsEffect(None)
        # Widen and center horizontally
        tip.setFixedWidth(420)
        tip.closeButton.move(tip.width() - 24, 19)
        x = (self.width() - 420) // 2
        tip.move(x, 120)
        tip.show()

        # Override setContent to re-center on text updates
        _orig_setContent = tip.setContent
        _self_ref = self
        def _centered_setContent(text):
            _orig_setContent(text)
            tip.setFixedWidth(420)
            tip.closeButton.move(tip.width() - 24, 19)
            x = (_self_ref.width() - 420) // 2
            tip.move(x, 120)
        tip.setContent = _centered_setContent

        return tip

    def _run_qprocess(self, worker_args: list[str], tip: StateToolTip,
                       on_done: Callable, on_msg: Callable | None = None) -> None:
        """Launch a worker subprocess via QProcess.

        Args:
            worker_args: args after --worker (e.g. ["apply", game_dir, ...])
            tip: StateToolTip to update with progress
            on_done: called when process finishes (receives parsed JSON msgs list)
            on_msg: optional callback for each JSON message as it arrives
        """
        from PySide6.QtCore import QProcess
        import json as _json

        self._active_progress = tip
        proc = QProcess(self)
        self._active_worker = proc

        # Pause non-essential timers
        if hasattr(self, "_db_watcher_paused"):
            self._db_watcher_paused = True
        if hasattr(self, '_db_poll_timer'):
            self._db_poll_timer.stop()
        if hasattr(self, '_update_timer'):
            self._update_timer.stop()

        exe = sys.executable
        args = ["--worker"] + worker_args
        _buf = [""]
        _msgs = []

        def _on_stdout():
            data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            _buf[0] += data
            while "\n" in _buf[0]:
                line, _buf[0] = _buf[0].split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                _msgs.append(msg)
                if msg.get("type") == "progress":
                    try:
                        tip.setContent(f"{msg.get('msg', '')} ({msg.get('pct', 0)}%)")
                    except RuntimeError:
                        pass
                if on_msg:
                    on_msg(msg)

        def _on_finished(exit_code, exit_status):
            tip.setContent(tr("progress.completed"))
            tip.setState(True)
            proc.deleteLater()
            self._active_worker = None
            self._active_progress = None
            self._resume_timers()
            on_done(_msgs)

        def _on_stderr():
            data = proc.readAllStandardError().data().decode("utf-8", errors="replace")
            if data.strip():
                logger.debug("Worker stderr: %s", data.strip()[:500])

        proc.readyReadStandardOutput.connect(_on_stdout)
        proc.readyReadStandardError.connect(_on_stderr)
        proc.finished.connect(_on_finished)
        proc.start(exe, args)
        logger.info("QProcess started [%s]: PID %s", worker_args[0], proc.processId())

    def _run_worker(self, worker, thread: QThread, progress: StateToolTip, on_finished) -> None:
        """Wire a worker + thread + StateToolTip with safe signal routing."""
        self._active_worker = worker
        self._worker_thread = thread
        self._active_progress = progress
        self._last_progress_time = 0.0

        # Disable AUTOMATIC GC — mandatory to prevent crash.
        # PySide6/shiboken crashes when GC runs on the worker thread and
        # finalizes Qt objects (confirmed by crash_trace.txt: "Garbage-collecting"
        # on QThread → access violation in summary_bar.paintEvent).
        # NO periodic gc.collect() during workers — profiling showed even
        # gen-0 collection takes 300-660ms when objects have accumulated,
        # causing the worst UI stalls. Single full collect after worker ends.
        import gc
        gc.disable()

        # Pause non-essential timers to keep event loop lean
        if hasattr(self, "_db_watcher_paused"):
            self._db_watcher_paused = True
        if hasattr(self, '_db_poll_timer'):
            self._db_poll_timer.stop()
        if hasattr(self, '_update_timer'):
            self._update_timer.stop()

        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # CRITICAL: Route progress through dispatcher — lambdas in PySide6
        # execute on the emitter's thread, not the receiver's. Calling
        # StateToolTip.setContent() from a worker thread segfaults silently.
        worker.progress_updated.connect(
            lambda pct, msg: self._dispatcher.call(
                self._update_progress_tip, pct, msg))

        worker.finished.connect(
            lambda *args: self._dispatcher.call(
                self._worker_done, thread, progress, on_finished, *args))
        worker.error_occurred.connect(
            lambda err: self._dispatcher.call(
                self._worker_error, thread, progress, err))

        thread.start()

    def _update_progress_tip(self, pct: int, msg: str) -> None:
        """Update StateToolTip from main thread — throttled to max ~7 fps."""
        import time
        now = time.monotonic()
        # Always show 0% (start) and 100% (end); throttle the rest to 150ms
        if pct not in (0, 100) and (now - self._last_progress_time) < 0.15:
            return
        self._last_progress_time = now
        tip = self._active_progress
        if tip is None:
            return
        try:
            tip.setContent(f"{msg} ({pct}%)")
        except RuntimeError:
            pass  # tooltip already deleted

    def _resume_timers(self) -> None:
        """Restart non-essential timers after worker completes."""
        if hasattr(self, '_db_poll_timer'):
            self._db_poll_timer.start(2000)
        if hasattr(self, '_update_timer'):
            self._update_timer.start(15 * 60 * 1000)
        if hasattr(self, "_db_watcher_paused"):
            self._db_watcher_paused = False
            self._stamp_db_mtime()

    def _worker_done(self, thread, progress: StateToolTip, callback, *args) -> None:
        progress.setContent(tr("progress.completed"))
        progress.setState(True)
        # Disconnect all signals BEFORE quit — prevents lambda closures
        # (which capture Qt objects) from lingering on the worker thread
        # where GC could finalize them on the wrong thread.
        worker = self._active_worker
        if worker is not None:
            try:
                worker.progress_updated.disconnect()
                worker.finished.disconnect()
                worker.error_occurred.disconnect()
            except (RuntimeError, TypeError):
                pass
        thread.quit()
        thread.wait(5000)
        thread.deleteLater()
        self._active_progress = None
        self._active_worker = None
        self._worker_thread = None
        import gc
        gc.enable()
        # DON'T call gc.collect() here — profiling showed it takes 1-1.5s
        # after gc.disable(). Let automatic GC handle it incrementally.
        self._resume_timers()
        try:
            callback(*args)
        except Exception:
            logger.error("Completion callback crashed", exc_info=True)

    def _worker_error(self, thread, progress: StateToolTip, err) -> None:
        progress.setContent(tr("progress.failed_short"))
        progress.setState(True)
        worker = self._active_worker
        if worker is not None:
            try:
                worker.progress_updated.disconnect()
                worker.finished.disconnect()
                worker.error_occurred.disconnect()
            except (RuntimeError, TypeError):
                pass
        thread.quit()
        thread.wait(5000)
        thread.deleteLater()
        self._active_progress = None
        self._active_worker = None
        self._worker_thread = None
        import gc
        gc.enable()
        self._resume_timers()
        # If there's an import queue, continue after cleanup settles
        if hasattr(self, '_import_queue') and self._import_queue:
            QTimer.singleShot(100, self._process_next_import)
            return
        InfoBar.error(
            title=tr("main.error"), content=str(err),
            duration=-1, position=InfoBarPosition.TOP, parent=self)

    # (Tool methods removed -- logic moved to individual tool pages)

    # ------------------------------------------------------------------
    # Snapshot applied state tracking
    # ------------------------------------------------------------------

    def _load_applied_state(self) -> None:
        """Load persisted applied state from DB on startup."""
        if not self._db:
            return
        try:
            rows = self._db.connection.execute(
                "SELECT id, applied FROM mods WHERE applied = 1"
            ).fetchall()
            self._applied_state = {row[0]: True for row in rows}
            if hasattr(self, 'paz_mods_page'):
                self.paz_mods_page._applied_state = dict(self._applied_state)
            logger.info("Loaded applied state: %d mods applied", len(self._applied_state))
        except Exception as e:
            logger.warning("Failed to load applied state: %s", e)

    def _snapshot_applied_state(self) -> None:
        """Save current mod enabled states as the 'applied' baseline.

        Persists to DB (applied column) so status survives across sessions.
        """
        if self._mod_manager and self._db:
            self._applied_state = {
                m["id"]: m["enabled"] for m in self._mod_manager.list_mods()
            }
            # Persist to DB
            try:
                for mod_id, enabled in self._applied_state.items():
                    self._db.connection.execute(
                        "UPDATE mods SET applied = ? WHERE id = ?",
                        (1 if enabled else 0, mod_id),
                    )
                self._db.connection.commit()
            except Exception as e:
                logger.warning("Failed to persist applied state: %s", e)
            # Push to mods page so cards show "installed" vs "loaded"
            if hasattr(self, 'paz_mods_page'):
                self.paz_mods_page._applied_state = dict(self._applied_state)
                logger.info("Applied state pushed: %d mods, %d enabled",
                            len(self._applied_state),
                            sum(1 for v in self._applied_state.values() if v))

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
                    title=tr("infobar.cleanup_complete"),
                    content=tr("infobar.cleaned_old_data", size_mb=f"{size_mb:.0f}"),
                    duration=5000, position=InfoBarPosition.TOP, parent=self)
                self._log_activity("cleanup",
                                   tr("activity.msg_removed_stale_appdata", size_mb=f"{size_mb:.0f}"))

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
                title=tr("main.busy"), content=tr("main.busy_msg"),
                duration=3000, position=InfoBarPosition.TOP, parent=self)
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

        tip = self._make_state_tooltip("Creating vanilla snapshot...")

        def on_snapshot_msg(msg):
            if msg.get("type") == "activity":
                self._log_activity(msg["cat"], msg["msg"], msg.get("detail", ""))

        def on_snapshot_done(msgs):
            errors = [m["msg"] for m in msgs if m.get("type") == "error"]
            if errors:
                self._snapshot_in_progress = False
                InfoBar.error(title=tr("main.snapshot_failed"), content=errors[-1],
                              duration=-1, position=InfoBarPosition.TOP, parent=self)
                return
            done_msgs = [m for m in msgs if m.get("type") == "done"]
            count = done_msgs[-1].get("count", 0) if done_msgs else 0
            self._on_snapshot_finished(count)

        self._run_qprocess(
            ["snapshot", str(self._game_dir), str(self._db.db_path)],
            tip, on_snapshot_done, on_msg=on_snapshot_msg)

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
            title=tr("infobar.snapshot_complete"),
            content=tr("infobar.snapshot_complete_msg", count=count),
            duration=6000, position=InfoBarPosition.TOP, parent=self)
        self._log_activity("snapshot", tr("activity.msg_snapshot_created", count=count))

    def _refresh_vanilla_backups(self) -> None:
        """Ensure critical vanilla backup files exist (PAMT, PAPGT, PATHC).

        Small files (<10MB) always copy synchronously. Large PAZ files are
        normally backed up lazily during Apply via _ensure_backups, EXCEPT
        for archives that enabled JSON mods patch — those must have a
        clean vanilla copy on disk so mount-time extraction can produce a
        correct overlay. Without this, JSON mods targeting 0008/0.paz
        (inventory/stamina/skill) silently no-op.
        """
        if not self._db or not self._game_dir or not self._vanilla_dir:
            return
        import shutil, os
        from cdumm.engine.json_target_scanner import enabled_json_target_archives
        MAX_SYNC_SIZE = 10 * 1024 * 1024  # 10MB — PAMT/PAPGT/PATHC are all <14MB
        try:
            critical = enabled_json_target_archives(self._db)
            rows = self._db.connection.execute(
                "SELECT file_path, file_size FROM snapshots"
            ).fetchall()
            copied = 0
            critical_copied = 0
            for rel_path, file_size in rows:
                normalized = rel_path.replace("\\", "/")
                is_critical = normalized in critical
                if not is_critical and file_size and file_size > MAX_SYNC_SIZE:
                    continue  # Large PAZ files backed up lazily during Apply
                game_file = self._game_dir / rel_path.replace("/", os.sep)
                backup_file = self._vanilla_dir / rel_path.replace("/", os.sep)
                if game_file.exists() and not backup_file.exists():
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(game_file, backup_file)
                    copied += 1
                    if is_critical:
                        critical_copied += 1
            if copied:
                logger.info(
                    "Refreshed %d vanilla backups (%d critical for JSON mods)",
                    copied, critical_copied)
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
            urls = mime.urls()
            logger.info("Drop event: %d URLs received", len(urls))
            for url in urls:
                local = url.toLocalFile()
                if local:
                    logger.info("Drop queuing: %s", Path(local).name)
                    self._on_import_dropped(Path(local))
                else:
                    logger.warning("Drop skipped non-local URL: %s", url.toString())

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
        # Stop the SystemThemeListener FIRST — it's a background thread
        # that can hold references during Qt teardown if left running.
        listener = getattr(self, "_theme_listener", None)
        if listener is not None:
            try:
                listener.terminate()
                listener.deleteLater()
            except Exception as _e:
                logger.debug("Theme listener teardown failed: %s", _e)
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
        # Persist window geometry so the next launch opens at the same size/position.
        # Must happen BEFORE db.close(). Ignore errors — this is best-effort.
        if self._db:
            try:
                from cdumm.storage.config import Config
                geom = self.saveGeometry()
                Config(self._db).set("window_geometry", bytes(geom.toBase64()).decode("ascii"))
            except Exception as _e:
                logger.debug("Could not save window geometry: %s", _e)
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
