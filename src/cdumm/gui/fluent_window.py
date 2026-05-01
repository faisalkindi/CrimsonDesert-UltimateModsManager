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
# Promoted to module-level: the lazy import inside _launch_import_worker
# was crashing the frozen exe with `zlib.error: Error -3 while decompressing
# data: incorrect header check` on PyInstaller 6.x — the bootloader's
# lazy-archive-extract path occasionally fails on submodules. Importing
# at module load time bypasses that path entirely.
from cdumm.gui.import_context import snapshot_and_clear_import_context


def _quiet_qprocess(proc) -> None:
    """Suppress the brief console window flash when ``QProcess`` spawns
    a Windows subprocess.

    Background: CDUMM's exe is built ``console=False`` (no console),
    but on Windows ``CreateProcess`` still allocates a transient
    console handle for any GUI-targeting child unless we explicitly
    pass ``CREATE_NO_WINDOW`` (0x08000000). The result is a "phantom
    window" that flashes for 50-300 ms (or stays visible for the
    whole worker run on slower machines) every time CDUMM spawns
    itself with ``--worker``.

    This helper sets ``QProcess.setCreateProcessArgumentsModifier`` to
    OR the flag in. No-op on non-Windows platforms — that codepath is
    never reached on Linux/macOS QProcess.
    """
    if sys.platform != "win32":
        return
    try:
        import subprocess as _sp
        flag = getattr(_sp, "CREATE_NO_WINDOW", 0x08000000)

        def _modify(args):
            try:
                args.flags |= flag
            except Exception as _e:
                logger.debug("CREATE_NO_WINDOW modifier write failed: %s", _e)

        proc.setCreateProcessArgumentsModifier(_modify)
    except Exception as e:
        logger.debug("CREATE_NO_WINDOW modifier setup failed: %s", e)


def _probe_console_state(tag: str) -> None:
    """Log whether the current process has a console attached. On
    Windows a windowed (console=False) exe has None; if Qt/subprocess
    ever flips that during a spawn, the returned handle is non-zero.
    Used to diagnose the flash-window complaint: if this logs a non-
    zero handle around the moment of the flash, the OS is allocating
    a console and we need CREATE_NO_WINDOW on the child.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        pid = ctypes.windll.kernel32.GetCurrentProcessId()
        logger.info("[flash-probe] %s pid=%s console_hwnd=%s",
                    tag, pid, hwnd)
    except Exception as e:
        logger.debug("[flash-probe] %s probe failed: %s", tag, e)


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
# Nexus / NXM / SSO helpers (ported from CDUMM_API_Test)
# ---------------------------------------------------------------------------

def _format_nxm_download_activity(mod_id: int, file_id: int) -> str:
    """Activity log message for nxm:// download start.

    Bug #3 fix: the pure InfoBar notification was sometimes invisible
    on multi-monitor setups. Logging to Activity gives the user a
    persistent record they can always find.
    """
    return f"Downloading mod {mod_id}, file {file_id} from Nexus"


def _format_nxm_import_activity(
    mod_name: str | None, is_update: bool,
) -> str:
    """Activity log message for nxm:// import completion."""
    name = mod_name or "mod"
    verb = "Updated" if is_update else "Imported"
    return f"{verb} {name} from Nexus"


_NXM_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB hard cap


def _assert_https_download_url(url: str) -> None:
    """Bug #27: refuse to download from any non-HTTPS URL. Nexus CDN
    answers only over HTTPS — an ``http://`` URL is either a
    misconfig or a MITM, and we're about to install the bytes into
    the user's game folder. Bail before the request.
    """
    try:
        import urllib.parse
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except Exception as e:
        raise ValueError(f"cannot parse download URL: {e}") from e
    if scheme != "https":
        raise ValueError(
            f"refusing to download over non-HTTPS scheme {scheme!r}")


def _validate_download_size(
    content_length: int | None, max_bytes: int
) -> None:
    """Bug #29: reject downloads whose advertised Content-Length
    exceeds the hard cap before we open the destination file.
    ``None`` = CDN didn't send the header; the streaming loop's
    running-total check catches those cases instead.
    """
    if content_length is None:
        return
    if int(content_length) > int(max_bytes):
        raise ValueError(
            f"download too large: Content-Length {content_length} > "
            f"cap {max_bytes}")


def _check_download_progress(total_bytes: int, max_bytes: int) -> None:
    """Bug #28/#29: running-total check called per chunk in the
    streaming loop. Catches Content-Length-absent cases where the
    CDN would otherwise trickle unbounded bytes.
    """
    if total_bytes > max_bytes:
        raise ValueError(
            f"download aborted: {total_bytes} bytes exceeds cap "
            f"{max_bytes}")


def _is_nexus_rate_limited(win, now: int) -> bool:
    """True when the auto-check should skip this cycle because Nexus
    previously returned 429 and the reset epoch hasn't passed yet.

    Bug 45: wires ``_pending_nexus_rate_limited_at`` into the
    actual timer decision so the captured reset_at isn't just a
    write-only breadcrumb.
    """
    until = int(getattr(win, "_nexus_rate_limited_until", 0) or 0)
    return until > int(now)


def _clear_auth_banner_state(win) -> None:
    """Bug #32: dismiss the "Nexus API key rejected" InfoBar and
    reset the flag when the user has saved a fresh valid key. Used
    by the Settings save path so the banner doesn't linger for up
    to 30 minutes until the next auto-check confirms.
    """
    banner = getattr(win, "_nexus_auth_banner", None)
    if banner is not None:
        try:
            banner.close()
        except Exception:
            pass
    win._nexus_auth_banner = None
    win._nexus_auth_banner_shown = False


def _snapshot_selected_labels(db, mod_id: int) -> dict | None:
    """Bug #24: capture the configurable-mod's ``selected_labels``
    before ``remove_mod`` cascade-deletes the mod_config row. Returns
    the parsed dict, or None when the mod was never configurable.
    """
    try:
        row = db.connection.execute(
            "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
            (mod_id,),
        ).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        import json as _json
        data = _json.loads(row[0])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _restore_selected_labels(
    db, mod_id: int, snapshot: dict | None,
    available_preset_names,
) -> None:
    """Bug #24: write the preserved ``selected_labels`` to the new
    mod's ``mod_config`` row, filtering out preset names the author
    removed/renamed in the update. A no-op when ``snapshot`` is None.
    """
    if not snapshot:
        return
    allowed = set(available_preset_names or [])
    filtered = {k: v for k, v in snapshot.items() if k in allowed}
    if not filtered:
        return
    try:
        import json as _json
        db.connection.execute(
            "INSERT OR REPLACE INTO mod_config "
            "(mod_id, selected_labels) VALUES (?, ?)",
            (mod_id, _json.dumps(filtered)))
        db.connection.commit()
    except Exception as e:
        logger.debug("restore selected_labels failed: %s", e)


def _clear_pending_post_import_state(win, path) -> None:
    """Zero the post-import scratch attributes the pre-check stages
    accumulate. Safe to call regardless of whether the worker ran to
    completion or failed — prevents stale values from bleeding into
    the next import (Bug #14).
    """
    for attr in (
        "_update_priority",
        "_update_enabled",
        "_configurable_source",
        "_configurable_labels",
        "_original_drop_path",
        "_pending_selected_labels",
        "_last_existing_mod_id",
    ):
        setattr(win, attr, None)
    nrf = getattr(win, "_nexus_real_file_id_map", None)
    if isinstance(nrf, dict) and path is not None:
        nrf.pop(str(path), None)


def _decide_auth_banner(
    had_error: bool, previously_shown: bool
) -> tuple[bool, bool]:
    """Decide whether to show the "Nexus API key rejected" banner
    for the current update cycle.

    Returns ``(show_now, new_flag)``. ``show_now`` is True only on
    the *first* failure after a success (or at startup); ``new_flag``
    replaces the previous ``_nexus_auth_banner_shown`` value and
    resets on any successful cycle so a later failure re-surfaces
    the banner (Bug #18).
    """
    if had_error:
        return (not previously_shown, True)
    return (False, False)


def _resolve_post_import_target_id(
    result_mod_id: int | None,
    existing_mod_id: int | None,
    max_row_id: int | None,
) -> int | None:
    """Pick the correct row to stamp post-import metadata onto.

    Priority: worker-emitted ``result_mod_id`` > prebound
    ``existing_mod_id`` > ``MAX(id)`` fallback.

    Before this helper existed, the post-import UPDATE statements used
    ``WHERE id = (SELECT MAX(id) FROM mods)`` unconditionally. That
    works for fresh inserts but breaks for the nxm:// update-in-place
    flow: the update lands on row 1333, while MAX(id) might be 1361.

    Returning None means "we don't know which row — write nothing"
    rather than silently targeting the wrong row.
    """
    if result_mod_id:
        return int(result_mod_id)
    if existing_mod_id:
        return int(existing_mod_id)
    if max_row_id:
        return int(max_row_id)
    return None


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
from cdumm.gui.pages.reshade_page import ReshadePage  # noqa: E402


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
        # Sweep any stale per-import staging dirs from a prior crash.
        # Each mixed-ZIP import creates `deltas/_asi_staging/<uuid>/`.
        # The GUI handler removes the subdir on success, but a worker
        # crash or app kill mid-import leaves it behind forever.
        try:
            asi_staging_root = self._deltas_dir / "_asi_staging"
            if asi_staging_root.is_dir():
                import shutil as _sh
                for sub in asi_staging_root.iterdir():
                    if sub.is_dir():
                        _sh.rmtree(sub, ignore_errors=True)
        except OSError:
            pass
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
        # install_lock writes a .running sentinel containing a timestamp.
        # The returned state dict also carries `was_stale` (True if the
        # sentinel survived from a previous run → crash) and a
        # clean_shutdown flag that closeEvent flips before exit. The
        # atexit hook only unlinks the lock when that flag is set, so
        # crashes (uncaught exceptions, SIGTERM, Windows shutdown) leave
        # the lock in place for the next launch to observe.
        from cdumm.gui.running_lock import (
            install_lock, _cleanup_running_lock, mark_clean_shutdown)
        self._lock_state = install_lock(self._app_data_dir / ".running")
        self._lock_file = self._lock_state["lock_file"]
        crashed_last_time = self._lock_state["was_stale"]
        import atexit as _atexit
        _atexit.register(_cleanup_running_lock, self._lock_state)
        # Belt-and-suspenders: QApplication.aboutToQuit fires on clean
        # exits that bypass main-window closeEvent (tray-icon quit,
        # menu-triggered QApplication.quit()). Without this, those
        # paths would leave the lock in place and the next launch
        # would report a false crash. BMAD A4.
        try:
            from PySide6.QtWidgets import QApplication
            _app = QApplication.instance()
            if _app is not None:
                _app.aboutToQuit.connect(
                    lambda: mark_clean_shutdown(self._lock_state))
        except Exception as _e_aq:
            logger.debug("aboutToQuit wiring skipped: %s", _e_aq)

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

        self.reshade_page = ReshadePage(self)
        self.reshade_page.set_managers(db=self._db, game_dir=self._game_dir)

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
        self.addSubInterface(
            self.reshade_page, FluentIcon.PALETTE, tr("nav.reshade"),
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
        # Run first check immediately on startup. Posted to the event
        # loop (0 ms) so it fires after the window's first paint
        # without blocking the constructor.
        QTimer.singleShot(0, self._run_nexus_update_check)

        # Module-level helpers read these attrs via getattr; initialise
        # them up-front so the first cycle doesn't have to special-case
        # "never ran before".
        self._nexus_rate_limited_until = 0
        self._nexus_auth_banner = None
        self._nexus_auth_banner_shown = False

        # ── nxm:// pending URL watcher ────────────────────────────────
        # Another CDUMM process launched with `--nxm <url>` drops the URL
        # into pending_nxm.txt and exits. This timer polls for queued
        # URLs and processes them as if the user had dragged the file.
        self._nxm_poll_timer = QTimer(self)
        self._nxm_poll_timer.timeout.connect(self._process_pending_nxm)
        self._nxm_poll_timer.start(2000)
        # Also drain any URL already queued before this instance started.
        QTimer.singleShot(1500, self._process_pending_nxm)

    # ------------------------------------------------------------------
    # NexusMods automatic update check
    # ------------------------------------------------------------------

    def _run_nexus_update_check(self) -> None:
        """Check NexusMods for mod updates in background. Runs automatically."""
        if not self._db:
            return
        # Bug 45: honour the rate-limit back-off. If Nexus returned
        # 429 on a prior cycle and we haven't reached the reset
        # epoch, skip — another check against 429 burns more quota
        # and can't succeed.
        import time as _time
        if _is_nexus_rate_limited(self, now=int(_time.time())):
            logger.debug(
                "NexusMods update check skipped — rate limit active "
                "until %d", int(getattr(self, "_nexus_rate_limited_until", 0)))
            return
        from cdumm.storage.config import Config
        api_key = Config(self._db).get("nexus_api_key")
        if not api_key:
            return  # No API key configured — skip silently

        # Read both PAZ mods and ASI plugins on main thread (SQLite thread safety).
        # Both tables share a single NexusMods update-check pass so the API quota
        # is spent once per cycle. nexus_last_checked_at lets the 1-week feed
        # act as an optimization rather than a blanket gate — see plan 4.2.
        try:
            cursor = self._db.connection.execute(
                "SELECT id, name, version, nexus_mod_id, nexus_last_checked_at, "
                "nexus_real_file_id "
                "FROM mods WHERE mod_type = 'paz'")
            mods = [{"id": r[0], "name": r[1], "version": r[2],
                     "nexus_mod_id": r[3],
                     "nexus_last_checked_at": r[4],
                     "nexus_real_file_id": r[5]}
                    for r in cursor.fetchall()]
            # Bug 43: read the new columns (nexus_real_file_id lets
            # the chain walk find the author-declared successor;
            # nexus_last_checked_at lets the feed-skip optimization
            # apply to ASI plugins too).
            cursor = self._db.connection.execute(
                "SELECT name, version, nexus_mod_id, "
                "nexus_real_file_id, nexus_last_checked_at "
                "FROM asi_plugin_state")
            asi_mods = [{"id": None, "name": r[0], "version": r[1],
                         "nexus_mod_id": r[2],
                         "nexus_real_file_id": r[3] or 0,
                         "nexus_last_checked_at": r[4] or 0}
                        for r in cursor.fetchall()]
        except Exception:
            return

        combined = mods + asi_mods
        if not any(m.get("nexus_mod_id") for m in combined):
            return  # No mods with NexusMods IDs in either table

        import threading
        def _check():
            try:
                from cdumm.engine.nexus_api import (
                    check_mod_updates, NexusAuthError, NexusRateLimited,
                )
                try:
                    updates, checked_ids, now_ts, backfill = check_mod_updates(
                        combined, api_key)
                    self._pending_nexus_updates = {u.mod_id: u for u in updates}
                    self._pending_nexus_checked_ids = checked_ids
                    self._pending_nexus_checked_ts = now_ts
                    self._pending_nexus_backfill = backfill
                    self._pending_nexus_auth_error = False
                    self._pending_nexus_rate_limited_at = 0
                except NexusAuthError as e:
                    # Bug #12 fix: distinguish invalid API key from
                    # generic transport failure so the UI can prompt
                    # the user to re-enter instead of silently staying
                    # grey forever.
                    logger.warning("Nexus API key rejected: %s", e)
                    self._pending_nexus_updates = {}
                    self._pending_nexus_checked_ids = []
                    self._pending_nexus_checked_ts = 0
                    self._pending_nexus_backfill = {}
                    self._pending_nexus_auth_error = True
                    self._pending_nexus_rate_limited_at = 0
                except NexusRateLimited as e:
                    # Bug 36: route rate-limit through a dedicated
                    # flag so the GUI can back off until reset_at
                    # instead of spamming 429s on the 30-min timer.
                    logger.warning(
                        "Nexus rate-limited in auto-check, reset_at=%d",
                        getattr(e, "reset_at", 0))
                    self._pending_nexus_updates = {}
                    self._pending_nexus_checked_ids = []
                    self._pending_nexus_checked_ts = 0
                    self._pending_nexus_backfill = {}
                    self._pending_nexus_auth_error = False
                    self._pending_nexus_rate_limited_at = int(
                        getattr(e, "reset_at", 0) or 0)
            except Exception as e:
                logger.warning("NexusMods update check failed: %s", e)
                self._pending_nexus_updates = {}
                self._pending_nexus_checked_ids = []
                self._pending_nexus_checked_ts = 0
                self._pending_nexus_backfill = {}
                self._pending_nexus_auth_error = False
                self._pending_nexus_rate_limited_at = 0
            from PySide6.QtCore import QMetaObject, Qt as _Qt
            QMetaObject.invokeMethod(
                self, "_apply_nexus_update_colors", _Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=_check, daemon=True).start()

    @Slot()
    def _apply_nexus_update_colors(self) -> None:
        """Propagate update results to both PAZ mods page and ASI plugins page."""
        # Bug 45: copy the rate-limit reset epoch from the per-cycle
        # _pending_* slot into persistent _nexus_rate_limited_until.
        # _is_nexus_rate_limited reads the persistent attr before
        # each auto-check fires, so setting it here is what actually
        # makes the back-off effective.
        rl_at = int(
            getattr(self, "_pending_nexus_rate_limited_at", 0) or 0)
        if rl_at:
            self._nexus_rate_limited_until = rl_at
            # InfoBar-level warning at transition into rate-limit
            # state. One-shot guarded by the same rl_at comparison.
            if getattr(self, "_rate_limit_banner_until", 0) != rl_at:
                self._rate_limit_banner_until = rl_at
                try:
                    InfoBar.warning(
                        title="Nexus rate limit reached",
                        content=(
                            "You've used your hourly Nexus API "
                            "quota. CDUMM will pause update checks "
                            "until the limit resets."),
                        duration=-1, position=InfoBarPosition.TOP,
                        parent=self)
                except Exception as _e:
                    logger.debug("rate-limit InfoBar failed: %s", _e)
        else:
            # Successful / non-rate-limited cycle — clear the
            # throttle so the next timer tick isn't suppressed.
            self._nexus_rate_limited_until = 0
        # Bug #12 / #18 fix: surface auth failures to the user, and
        # reset the "already shown" flag on successful cycles so a
        # LATER failure re-shows the banner. The old one-shot gate
        # was sticky forever — a user who fixed their key and then
        # had it fail again never saw a second warning.
        had_auth_error = bool(
            getattr(self, "_pending_nexus_auth_error", False))
        self._pending_nexus_auth_error = False
        shown_previously = bool(
            getattr(self, "_nexus_auth_banner_shown", False))
        show_banner, new_flag = _decide_auth_banner(
            had_auth_error, shown_previously)
        self._nexus_auth_banner_shown = new_flag
        if show_banner:
            try:
                # Bug #32: stash the InfoBar so a later "valid key
                # saved" event can close it instead of waiting 30
                # minutes for the next auto-check confirmation.
                self._nexus_auth_banner = InfoBar.error(
                    title=tr("settings.nexus_auth_rejected_title"),
                    content=tr("settings.nexus_auth_rejected_body"),
                    duration=-1, position=InfoBarPosition.TOP,
                    parent=self)
            except Exception as e:
                logger.debug("auth-rejected InfoBar failed: %s", e)
                self._nexus_auth_banner = None
        # Persist the last-checked timestamps on the GUI thread — the
        # worker hit SQLite's "connection bound to originating thread"
        # constraint. Only row ids that returned a valid file list are
        # in checked_ids; transient failures are NOT persisted so they
        # get retried on the next cycle.
        checked_ids = getattr(self, "_pending_nexus_checked_ids", [])
        now_ts = getattr(self, "_pending_nexus_checked_ts", 0)
        if self._db and checked_ids and now_ts:
            try:
                placeholders = ",".join("?" * len(checked_ids))
                self._db.connection.execute(
                    f"UPDATE mods SET nexus_last_checked_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [now_ts, *checked_ids])
                self._db.connection.commit()
            except Exception as e:
                logger.debug("nexus_last_checked_at persist failed: %s", e)

        # Backfill nexus_real_file_id for rows that got resolved via
        # the name-match path this cycle. Once populated, the next
        # check walks the file_updates chain for these rows instead
        # of guessing by name. Self-correction also routes through
        # this path — when an earlier backfill latched onto the
        # wrong file_id, the engine emits a corrected value here and
        # the helper overwrites the wrong value freely. Engine-level
        # dedup ensures we never queue a same-value rewrite, so the
        # overwrite is safe. Bug from systematic-debugging review
        # 2026-04-26.
        backfill = getattr(self, "_pending_nexus_backfill", {}) or {}
        if self._db and backfill:
            from cdumm.engine.nexus_api import persist_backfill_file_ids
            persisted = persist_backfill_file_ids(
                self._db.connection, backfill)
            if persisted:
                logger.info(
                    "nexus_real_file_id: backfilled %d row(s)",
                    persisted)
        self._nexus_updates = getattr(self, "_pending_nexus_updates", {})
        if hasattr(self, 'paz_mods_page'):
            self.paz_mods_page.set_nexus_updates(self._nexus_updates)
        if hasattr(self, 'asi_plugins_page'):
            try:
                self.asi_plugins_page.set_nexus_updates(self._nexus_updates)
            except AttributeError:
                pass  # ASI page may not implement it in older builds
        # ``_nexus_updates`` now carries both outdated and confirmed-
        # current entries (three-state pill support). Split the counts
        # in the log so it's clear how many are actually actionable.
        from cdumm.engine.nexus_api import filter_outdated
        outdated_ct = len(filter_outdated(self._nexus_updates.values()))
        logger.info(
            "NexusMods update check: %d outdated, %d confirmed current",
            outdated_ct, len(self._nexus_updates) - outdated_ct)

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
            self._check_duplicate_mods()

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
                    # v3.2: unified Recovery Flow trigger (Codex
                    # review finding 10). Both startup paths surface
                    # the same Recovery button instead of two
                    # different MessageBoxes.
                    self._offer_recovery_flow(
                        title="Game files changed — recovery available",
                        body=(
                            "Your game files have changed since the "
                            "last snapshot (likely a Steam patch or "
                            "Verify Integrity). Click Start Recovery "
                            "to run the full recovery flow."))
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
        # Defensive: the QThread C++ object can be destroyed by Qt
        # (e.g. parent widget teardown) before our cleanup lambda
        # resets self._update_thread to None. The Python wrapper
        # then holds a stale reference and isRunning() raises
        # RuntimeError 'Internal C++ object already deleted'.
        # Bug from priston201 issue #47 (2026-04-25). Treat the
        # exception as 'thread is dead, ok to start a new one'.
        existing = getattr(self, "_update_thread", None)
        if existing is not None:
            try:
                still_running = existing.isRunning()
            except RuntimeError:
                still_running = False
                self._update_thread = None
            if still_running:
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

        # Honour the user's per-version dismissal — if they previously
        # closed the banner for this exact tag we skip the banner this
        # launch. A NEWER tag clears the skip and shows fresh. Without
        # this, users who dismissed the banner kept getting it back on
        # every relaunch (DeathZxZ on Nexus reported it blocked the
        # button text at the top of the window with no way to close).
        try:
            from cdumm.storage.config import Config
            dismissed = (Config(self._db).get("update_banner_dismissed_for")
                         if self._db else None)
        except Exception as e:
            logger.debug("dismissal check failed: %s", e)
            dismissed = None
        if dismissed != tag:
            # 1. Show persistent update banner on all pages (except about)
            self._show_update_banner(tag, url)

        # 2. Add badge to About nav item in sidebar (always — it's small)
        try:
            self.navigationInterface.widget("AboutPage").setShowBadge(True)
        except Exception:
            pass

        # 3. Update the About page with update info (always)
        if hasattr(self, 'about_page'):
            self.about_page.set_update_status(tag, url, info.get("body", ""))

    def _on_dismiss_update_banner(self) -> None:
        """Hide the update banner this session AND remember which tag
        the user dismissed so it doesn't reappear next launch — but a
        newer tag will show fresh. The About-page badge stays so the
        user can still find the update if they want it later."""
        tag = getattr(self, "_update_banner_tag", None)
        try:
            from cdumm.storage.config import Config
            if self._db and tag:
                Config(self._db).set("update_banner_dismissed_for", tag)
        except Exception as e:
            logger.debug("dismissal save failed: %s", e)
        if hasattr(self, "_update_banner") and self._update_banner:
            self._update_banner.deleteLater()
            self._update_banner = None

    def _show_update_banner(self, tag: str, url: str) -> None:
        """Show a persistent update banner at the top of the window."""
        from PySide6.QtWidgets import QHBoxLayout, QWidget
        from PySide6.QtGui import QFont, QDesktopServices
        from PySide6.QtCore import QUrl
        from qfluentwidgets import (BodyLabel, PushButton, TransparentToolButton,
                                    FluentIcon)

        if hasattr(self, '_update_banner') and self._update_banner:
            self._update_banner.deleteLater()

        banner = QWidget(self)
        banner.setObjectName("updateBanner")
        banner.setFixedHeight(36)

        layout = QHBoxLayout(banner)
        layout.setContentsMargins(16, 0, 8, 0)
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

        # Close (X) button — dismisses the banner this session AND
        # remembers the tag so it won't reappear next launch (until
        # a newer tag arrives). Fixes DeathZxZ's "non-closable" report.
        close_btn = TransparentToolButton(FluentIcon.CLOSE, banner)
        close_btn.setFixedSize(26, 26)
        close_btn.setToolTip(tr("main.dismiss_update_banner"))
        close_btn.clicked.connect(self._on_dismiss_update_banner)
        layout.addWidget(close_btn)

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
    # NXM (nxm:// URL) download + pending-queue handling
    # ------------------------------------------------------------------

    def _process_pending_nxm(self) -> None:
        """Drain queued ``nxm://`` URLs left by ``--nxm`` launches.

        Each line in ``pending_nxm.txt`` is one URL. For each: parse,
        look up the API key, call ``download_link.json`` (with the
        ``key`` + ``expires`` query params that came from the URL when
        present — that's what lets free-tier downloads succeed), fetch
        the file to a temp path, and dispatch through the existing drop
        handler so import follows the same path as a dragged file.
        """
        pending = self._app_data_dir / "pending_nxm.txt"
        if not pending.exists():
            return
        # Atomic handoff: rename the queue file aside before reading it,
        # so a concurrent ``--nxm`` launcher that appends in the window
        # between our read and unlink doesn't have its URL dropped on
        # the floor. ``os.replace`` is atomic on both Windows and POSIX.
        import os, time as _time
        processing = self._app_data_dir / f".pending_nxm.processing.{os.getpid()}.{int(_time.time() * 1000)}"
        try:
            os.replace(pending, processing)
        except FileNotFoundError:
            return  # another cycle picked it up first
        except Exception as e:
            logger.warning("Failed to rename pending_nxm.txt: %s", e)
            return
        try:
            lines = [
                ln.strip() for ln in
                processing.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
        except Exception as e:
            logger.warning("Failed to read %s: %s", processing, e)
            lines = []
        finally:
            try:
                processing.unlink()
            except Exception:
                pass
        if not lines:
            return
        # De-dupe nxm:// URLs across a 10-second window. Many Chromium
        # builds fire registered protocol handlers TWICE for a single
        # click (one from the page, one from the navigation hand-off),
        # which produces two duplicate downloads and two duplicate mod
        # rows. The atomic queue handoff above keeps appends from being
        # dropped, but it doesn't filter same-URL re-fires. Real-world
        # hit: a single 'Mod Manager Download' click on Better
        # Inventory And Trade UI created mod_id=1490 AND 1491 in one
        # drain batch.
        recent: dict = getattr(self, "_recent_nxm_urls", {})
        now_ts = _time.time()
        # Forget anything older than 30s so the dict can't grow
        # unbounded across a long session.
        recent = {u: ts for u, ts in recent.items() if now_ts - ts < 30}
        for url in lines:
            prior = recent.get(url)
            if prior is not None and (now_ts - prior) < 10:
                logger.warning(
                    "nxm: ignoring duplicate URL fired within 10s: %s",
                    url)
                continue
            recent[url] = now_ts
            try:
                self._handle_nxm_url(url)
            except Exception as e:
                logger.error("nxm URL %s failed: %s", url, e, exc_info=True)
                InfoBar.error(
                    title="Download Failed",
                    content=f"Could not handle {url}\n\n{e}",
                    duration=-1, position=InfoBarPosition.TOP, parent=self)
        self._recent_nxm_urls = recent

    def _handle_direct_update(self, local_mod_id: int, nexus_mod_id: int,
                               file_id: int, fallback_url: str) -> None:
        """Premium direct download for the click-to-update pill.

        Synthesises an ``nxm://`` URL without ``key``/``expires`` query
        params (those are only required for free tier). The standard
        :meth:`_handle_nxm_url` worker handles the rest — it'll get
        a ``download_link.json`` response for premium users and fall
        back to the browser via :class:`NexusPremiumRequired` for free
        users, which opens ``fallback_url`` (the mod's Files tab).

        ``local_mod_id`` is passed through to :meth:`_handle_nxm_url`
        as ``intended_mod_id`` so the binding decision in the
        completion handler can short-circuit heuristics that fail
        for renamed mods (e.g. "Horse X" → "Legendary Horse Body
        Size Increase") or genuine version updates with a different
        file_id than the stored one. Bug from Faisal 2026-04-27.
        """
        if not nexus_mod_id or not file_id:
            # We don't have enough info to synthesise the URL (probably
            # an outdated update payload that pre-dates latest_file_id).
            # Open the browser so the user can still update.
            if fallback_url:
                import webbrowser
                webbrowser.open(fallback_url)
            return
        # Use the same scheme/path Nexus's website would emit so the
        # downstream code path is byte-identical to a real nxm:// click.
        synth_url = f"nxm://crimsondesert/mods/{nexus_mod_id}/files/{file_id}"
        logger.info(
            "direct update: local_mod_id=%d nexus_mod_id=%d file_id=%d "
            "(skipping browser handover)",
            local_mod_id, nexus_mod_id, file_id)
        self._handle_nxm_url(synth_url, intended_mod_id=local_mod_id)

    def _handle_nxm_url(self, url: str,
                         intended_mod_id: int | None = None) -> None:
        """Resolve an ``nxm://`` URL into a downloaded file + import queue.

        Runs the :func:`get_download_link` API call AND the CDN download
        on a background thread so the GUI stays responsive. Results are
        marshalled back to the main thread via
        :meth:`PySide6.QtCore.QMetaObject.invokeMethod` and fed into the
        standard drop flow.

        ``intended_mod_id`` (Path-explicit-intent fix, 2026-04-27): when
        the caller knows which existing local mod row this download is
        meant to update (e.g. the click-to-update pill), pass it here.
        It threads through the completion queue and short-circuits the
        binding heuristic in :meth:`_finish_nxm_download`. Without it
        (a fresh nxm:// click from Nexus website with no local intent),
        the existing heuristic disambiguates sibling-mod-on-same-page
        vs update-of-existing-mod from the URL alone.
        """
        from cdumm.engine.nxm_handler import parse_nxm_url, NxmUrlError
        from cdumm.storage.config import Config

        try:
            parsed = parse_nxm_url(url)
        except NxmUrlError as e:
            # Bug 38: give the user a specific message rather than
            # letting the generic handler say "Could not handle".
            logger.warning("Malformed nxm:// URL: %s — %s", url, e)
            InfoBar.error(
                title="Malformed NXM URL",
                content=f"Could not parse {url!r}: {e}",
                duration=-1, position=InfoBarPosition.TOP, parent=self)
            return
        api_key = Config(self._db).get("nexus_api_key") if self._db else None
        if not api_key:
            InfoBar.warning(
                title="Nexus API key missing",
                content="Add your API key in Settings → NexusMods Integration first.",
                duration=-1, position=InfoBarPosition.TOP, parent=self)
            return

        # Show progress toast so the reviewer doesn't think the click
        # was a no-op while the CDN warms up. Bug #17: sticky until
        # the download actually finishes (closed in
        # _finish_nxm_download). Fixed 15s was too short for slow
        # networks / large mods — the toast disappeared before
        # completion and users thought nothing was happening.
        #
        # Bug 39: close any prior banner before overwriting the
        # attribute — rapid NXM clicks otherwise pile up orphaned
        # InfoBars that linger until window destruction.
        prior = getattr(self, "_nxm_download_banner", None)
        if prior is not None:
            try:
                prior.close()
            except Exception:
                pass
        try:
            self._nxm_download_banner = InfoBar.info(
                title="Downloading from Nexus…",
                content=f"Mod {parsed.mod_id}, file {parsed.file_id}",
                duration=-1, position=InfoBarPosition.TOP, parent=self)
        except Exception as e:
            logger.debug("NXM download banner create failed: %s", e)
            self._nxm_download_banner = None
        try:
            self._log_activity(
                "import",
                _format_nxm_download_activity(parsed.mod_id, parsed.file_id))
        except Exception:
            pass

        # Thread-safe producer-consumer queue so overlapping nxm clicks
        # don't race over shared instance attributes. Each worker pushes
        # exactly one (result, error) record; _finish_nxm_download
        # drains whatever's available on the main thread.
        if not hasattr(self, "_nxm_completion_queue"):
            import queue as _queue
            self._nxm_completion_queue = _queue.Queue()

        import threading

        def _worker():
            import tempfile, urllib.request, urllib.parse
            from cdumm.engine.nexus_api import (
                get_download_link, NexusPremiumRequired,
                NexusAuthError, NexusRateLimited,
                mod_page_files_url,
            )
            result = None
            error = None
            try:
                try:
                    download_url = get_download_link(
                        parsed.mod_id, parsed.file_id, api_key,
                        nxm_key=parsed.key, nxm_expires=parsed.expires)
                except NexusPremiumRequired:
                    error = ("premium_required", mod_page_files_url(parsed.mod_id))
                    return
                except NexusAuthError as e:
                    # Bug #21: route auth failure through the auth-
                    # banner path (same one the auto-check uses) so
                    # the user gets a persistent "re-enter your key"
                    # message, not a bland toast that vanishes.
                    error = ("auth", str(e))
                    return
                except NexusRateLimited as e:
                    error = ("rate_limited", getattr(e, "reset_at", 0))
                    return
                if not download_url:
                    error = ("no_url", "Nexus didn't hand back a CDN link. "
                             "Try again in a moment.")
                    return
                split = urllib.parse.urlsplit(download_url)
                # Bug #27: reject non-HTTPS scheme before request.
                try:
                    _assert_https_download_url(download_url)
                except ValueError as _scheme_err:
                    error = ("other", str(_scheme_err))
                    return
                safe_path = urllib.parse.quote(split.path, safe="/%")
                download_url = urllib.parse.urlunsplit((
                    split.scheme, split.netloc, safe_path,
                    split.query, split.fragment))

                tmp_dir = Path(tempfile.mkdtemp(prefix="cdumm_nxm_"))
                dest = tmp_dir / f"nxm_{parsed.mod_id}_{parsed.file_id}.bin"
                logger.info("nxm: downloading %s -> %s", download_url, dest)
                from cdumm import __version__
                req = urllib.request.Request(
                    download_url,
                    headers={
                        "User-Agent": f"CDUMM/{__version__}",
                        "Application-Name": "CDUMM",
                        "Application-Version": __version__,
                    })
                with urllib.request.urlopen(req, timeout=60) as resp, \
                        open(dest, "wb") as f:
                    # Bug #29: content-length sanity check up front.
                    cl_raw = resp.headers.get("Content-Length")
                    try:
                        cl = int(cl_raw) if cl_raw else None
                    except (TypeError, ValueError):
                        cl = None
                    try:
                        _validate_download_size(
                            cl, _NXM_MAX_DOWNLOAD_BYTES)
                    except ValueError as _size_err:
                        error = ("other", str(_size_err))
                        return
                    # Bug #28: streaming accumulator catches the
                    # Content-Length-absent case where the CDN
                    # trickles bytes indefinitely.
                    total = 0
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        try:
                            _check_download_progress(
                                total, _NXM_MAX_DOWNLOAD_BYTES)
                        except ValueError as _prog_err:
                            error = ("other", str(_prog_err))
                            return
                        f.write(chunk)
                logger.info("nxm: downloaded %d bytes to %s",
                            dest.stat().st_size, dest)

                # Rename to the ACTUAL filename Nexus served (not just
                # the extension). Without this the imported mod ends up
                # named "Nxm 774 3148" because the prettifier sees the
                # synthetic temp name. Using the real filename also
                # lets parse_nexus_filename extract the version + mod
                # id properly via the standard timestamped format
                # (ModName-id-version-uploaded.zip).
                final = dest
                tail = download_url.rsplit("/", 1)[-1].split("?")[0]
                if tail:
                    # Decode percent-encoded spaces back to real spaces
                    # so the final filename matches what a browser
                    # download would have produced.
                    real_name = urllib.parse.unquote(tail)
                    # Sanitise: strip path separators, keep only
                    # filename portion. Path() already drops directory
                    # components but we belt-and-suspenders here.
                    real_name = Path(real_name).name
                    if real_name and real_name != dest.name:
                        final = tmp_dir / real_name
                        dest.rename(final)
                result = final
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    body = ""
                logger.error(
                    "nxm: CDN download failed — HTTP %d: %s | URL: %s",
                    e.code, body, getattr(e, "url", "?"))
                error = ("http", f"HTTP {e.code}: {body or str(e)}")
            except Exception as e:
                logger.error("nxm: CDN download failed: %s", e, exc_info=True)
                error = ("other", str(e))
            finally:
                # Push (result, error, nexus_mod_id, nexus_file_id) —
                # queue is thread-safe. mod_id binds to an existing
                # local row; file_id gets stored on that row so the
                # next update check can walk the file_updates chain
                # to find which file supersedes this one.
                self._nxm_completion_queue.put(
                    (result, error, parsed.mod_id, parsed.file_id,
                     intended_mod_id))
                from PySide6.QtCore import QMetaObject, Qt as _Qt
                QMetaObject.invokeMethod(
                    self, "_finish_nxm_download",
                    _Qt.ConnectionType.QueuedConnection)

        threading.Thread(
            target=_worker, daemon=True, name="cdumm-nxm").start()

    @Slot()
    def _finish_nxm_download(self) -> None:
        """Main-thread completion slot for :meth:`_handle_nxm_url`.

        Drains every ``(result, error)`` record queued by a worker. The
        queue is the synchronization point — if two downloads finished
        back-to-back, both are processed even if QueuedConnection
        coalesced the slot invocations.
        """
        import queue as _queue
        # Bug #17: close the sticky "Downloading from Nexus…" banner
        # now that the worker has reported in.
        banner = getattr(self, "_nxm_download_banner", None)
        if banner is not None:
            try:
                banner.close()
            except Exception:
                pass
            self._nxm_download_banner = None
        q = getattr(self, "_nxm_completion_queue", None)
        if q is None:
            return
        while True:
            try:
                (result, err, nexus_mod_id, nexus_file_id,
                 intended_mod_id) = q.get_nowait()
            except _queue.Empty:
                return
            if err is not None:
                kind, detail = err
                # Bug #5 fix: every nxm:// error path now also logs an
                # Activity entry so a free-tier user clicking "Click To
                # Update" without a premium account gets a persistent
                # record of what happened, not just a toast that might
                # vanish or land offscreen.
                try:
                    if kind == "premium_required":
                        self._log_activity(
                            "import",
                            "Free-tier download: opening Nexus page "
                            "for manual 'Mod Manager Download' click")
                    else:
                        self._log_activity(
                            "import",
                            f"Nexus download failed ({kind}): {detail}")
                except Exception:
                    pass
                if kind == "premium_required":
                    import webbrowser
                    webbrowser.open(detail)
                    InfoBar.info(
                        title="Free-tier download — click 'Mod Manager Download' on Nexus",
                        content=(
                            "Opened the mod's Files tab in your browser. "
                            "Click 'Mod Manager Download' there and the "
                            "file will come back to CDUMM automatically."),
                        duration=15000,
                        position=InfoBarPosition.TOP, parent=self)
                elif kind == "auth":
                    # Bug #21: route through the same flag the auto-
                    # check uses so the user sees a single coherent
                    # "API key rejected — re-enter in Settings"
                    # banner. _apply_nexus_update_colors reads this
                    # flag + _decide_auth_banner to avoid spamming.
                    self._pending_nexus_auth_error = True
                    from PySide6.QtCore import QMetaObject, Qt as _Qt
                    QMetaObject.invokeMethod(
                        self, "_apply_nexus_update_colors",
                        _Qt.ConnectionType.QueuedConnection)
                elif kind == "rate_limited":
                    InfoBar.warning(
                        title="Nexus rate limit reached",
                        content=(
                            "You've used your hourly Nexus API quota. "
                            "Try again shortly; the limit resets on "
                            "the hour."),
                        duration=-1, position=InfoBarPosition.TOP,
                        parent=self)
                elif kind == "no_url":
                    InfoBar.error(
                        title="No download URL returned",
                        content=detail, duration=-1,
                        position=InfoBarPosition.TOP, parent=self)
                elif kind == "http":
                    InfoBar.error(
                        title="Download failed", content=detail,
                        duration=-1, position=InfoBarPosition.TOP, parent=self)
                else:
                    InfoBar.error(
                        title="Download failed", content=detail,
                        duration=-1, position=InfoBarPosition.TOP, parent=self)
                continue
            if result is not None:
                # Look up which existing local mod row points at this
                # Nexus mod_id and pass it to _queue_import so the
                # import REPLACES instead of duplicating.
                #
                # Multi-row case: backfill bugs can put the same
                # nexus_mod_id on multiple rows (e.g. mod 664 is shared
                # by Easier QTE + Easier Rodeo via filename inference).
                # If we silently pick one, the other row stays outdated
                # forever and re-clicking just downloads the same file
                # again. Surface the ambiguity to the user via an
                # InfoBar so they can de-dup before retrying.
                existing_id = None
                # Explicit-intent fast path: when the user clicked
                # "Click To Update" on a specific local mod card, the
                # binding target is unambiguous — skip both the multi-
                # row ambiguity warning AND the heuristic. Bug from
                # Faisal 2026-04-27: clicking Update on Horse X (row
                # 1424) was creating a duplicate "Legendary Horse Body
                # Size Increase" card because the heuristic name
                # comparison failed for renamed mods.
                if intended_mod_id and self._db:
                    from cdumm.engine.nxm_handler import (
                        should_bind_to_existing_row,
                    )
                    existing_id = should_bind_to_existing_row(
                        self._db.connection,
                        nexus_mod_id=int(nexus_mod_id or 0),
                        nexus_file_id=int(nexus_file_id or 0),
                        downloaded_zip=result,
                        intended_mod_id=intended_mod_id)
                    if existing_id is not None:
                        logger.info(
                            "nxm: explicit-intent bind to mod_id=%d "
                            "(skipping heuristic — user clicked Update "
                            "on this specific row)", existing_id)
                # Heuristic path: only runs when explicit intent
                # WASN'T provided. If intended_mod_id was set but
                # rejected by should_bind_to_existing_row (deleted
                # row or nexus_mod_id mismatch — see iterations 5/6),
                # we deliberately do NOT fall back to the heuristic
                # — that could bind to a sibling row sharing
                # nexus_mod_id and replace the wrong mod. User intent
                # was specific; missing/inconsistent → import as new.
                if (existing_id is None and not intended_mod_id
                        and self._db and nexus_mod_id):
                    # Multi-row warning still applies: if multiple rows
                    # share the same nexus_mod_id we surface it for the
                    # user to dedup. The single-row binding decision now
                    # routes through should_bind_to_existing_row which
                    # checks file_id match (or name peek for legacy
                    # rows). Bug from Faisal 2026-04-26: page 208 hosts
                    # both Better Subtitles and No Letterbox; clicking
                    # Mod Manager Download for one was replacing the
                    # other because nexus_mod_id alone isn't unique.
                    try:
                        rows = self._db.connection.execute(
                            "SELECT id, name FROM mods "
                            "WHERE nexus_mod_id = ? ORDER BY id ASC",
                            (int(nexus_mod_id),)).fetchall()
                    except Exception as e:
                        logger.debug("nxm: existing-mod lookup failed: %s", e)
                        rows = []
                    if len(rows) > 1:
                        names = ", ".join(r[1] for r in rows)
                        logger.warning(
                            "nxm: %d mod rows share nexus_mod_id=%d (%s) — "
                            "importing as new to avoid wrong-target replace",
                            len(rows), nexus_mod_id, names)
                        InfoBar.warning(
                            title="Multiple mods share this Nexus ID",
                            content=(
                                f"You have {len(rows)} installed mods linked to "
                                f"Nexus mod #{nexus_mod_id} ({names}). "
                                "CDUMM imported the new download as a separate "
                                "mod to avoid replacing the wrong one. "
                                "Right-click → Delete the duplicates if you "
                                "want a single entry."),
                            duration=-1, position=InfoBarPosition.TOP, parent=self)
                    elif len(rows) == 1:
                        from cdumm.engine.nxm_handler import (
                            should_bind_to_existing_row,
                        )
                        # No intent here (the if-guard above ensures
                        # intended_mod_id is falsy on this branch).
                        existing_id = should_bind_to_existing_row(
                            self._db.connection,
                            nexus_mod_id=int(nexus_mod_id),
                            nexus_file_id=int(nexus_file_id or 0),
                            downloaded_zip=result)
                        if existing_id is not None:
                            logger.info(
                                "nxm: binding download to existing mod_id=%d "
                                "(nexus_mod_id=%d)", existing_id, nexus_mod_id)
                        else:
                            logger.info(
                                "nxm: row %d has same nexus_mod_id=%d but "
                                "different file/name — importing as new mod "
                                "to avoid wrong-target replace",
                                rows[0][0], nexus_mod_id)
                # Fallback: when no row matches by nexus_mod_id (because
                # the existing row was imported as a local zip and never
                # got Nexus metadata stored), match by name instead so a
                # nxm:// download UPDATES the existing row instead of
                # creating a parallel one. Real-world hit: 'CD Inventory
                # Expander' was a local-zip import with NULL nexus_mod_id;
                # clicking 'Mod Manager Download' on Nexus created a
                # second row 1487 alongside the original 1406.
                #
                # Iteration 11 systematic-debugging: if intended_mod_id
                # was set but rejected, also skip this name-match
                # fallback — it could bind to a similarly-named sibling
                # and cause the same wrong-target replace this whole
                # branch was added to prevent.
                if existing_id is None and not intended_mod_id and self._db:
                    try:
                        existing_id = self._match_existing_by_name(result)
                        if existing_id is not None:
                            logger.info(
                                "nxm: name-match binding to existing "
                                "mod_id=%d (nexus_mod_id was NULL on that "
                                "row)", existing_id)
                    except Exception as e:
                        logger.debug("nxm: name-match fallback failed: %s", e)
                self._queue_import(
                    result,
                    existing_mod_id=existing_id,
                    nexus_real_file_id=nexus_file_id)

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
        _quiet_qprocess(proc)
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
        _quiet_qprocess(proc)
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

    def _queue_import(self, path: Path, existing_mod_id: int | None = None,
                       nexus_real_file_id: int | None = None) -> None:
        """Add a path to the import queue. Processes sequentially.

        ``existing_mod_id`` lets callers (notably the nxm:// download
        handler) bind the queued file to an already-imported mod row so
        the import REPLACES it instead of creating a duplicate.

        ``nexus_real_file_id`` is the actual numeric Nexus file_id when
        known (always present for nxm:// downloads). Stored on the mod
        row by the post-import handler so the next update check can
        walk the file_updates chain.
        """
        if not hasattr(self, '_import_queue'):
            self._import_queue: list[Path] = []
        if not hasattr(self, '_import_errors'):
            self._import_errors: list[str] = []
        if not hasattr(self, '_existing_mod_id_map'):
            self._existing_mod_id_map: dict[str, int] = {}
        if not hasattr(self, '_nexus_real_file_id_map'):
            self._nexus_real_file_id_map: dict[str, int] = {}
        self._import_queue.append(path)
        if existing_mod_id is not None:
            self._existing_mod_id_map[str(path)] = existing_mod_id
            logger.info(
                "Queued for import: %s (queue size: %d, worker active: %s, "
                "replacing mod_id=%d)",
                path.name, len(self._import_queue),
                self._active_worker is not None, existing_mod_id)
        else:
            logger.info("Queued for import: %s (queue size: %d, worker active: %s)",
                         path.name, len(self._import_queue), self._active_worker is not None)
        if nexus_real_file_id:
            self._nexus_real_file_id_map[str(path)] = int(nexus_real_file_id)
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
        # Files with a prebound existing_mod_id (nxm:// downloads that
        # need to REPLACE an existing mod, not import as new) also get
        # forced through the single-import path so the binding survives
        # — the batch worker doesn't accept per-file mod_ids.
        if len(self._import_queue) > 1:
            from cdumm.gui.preset_picker import find_json_presets, find_folder_variants
            prebound = getattr(self, "_existing_mod_id_map", {})
            batch = []
            deferred = []  # multi-preset mods that need dialog
            for p in self._import_queue:
                needs_dialog = False
                if str(p) in prebound:
                    # Skip batch — force single import to preserve mod_id binding
                    needs_dialog = True
                elif p.is_dir():
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

        # Skip exact-name+version duplicates silently. The single-import
        # path at _import_with_prechecks does this per file via
        # _find_existing_mod + _get_drop_version; without the same gate
        # here, dragging a folder of all-already-installed mods doubles
        # every row in the DB. Mirror the single-path predicate so
        # behaviour is consistent across drop modes.
        if self._mod_manager and paths:
            deduped: list = []
            skipped: list[tuple[str, str]] = []
            installed_by_name: dict[str, str] = {
                m["name"]: (m.get("version") or "")
                for m in self._mod_manager.list_mods()
            }
            for p in paths:
                existing = self._find_existing_mod(p)
                if existing:
                    _, mname, _ = existing
                    installed_ver = installed_by_name.get(mname, "")
                    drop_ver = self._get_drop_version(p)
                    if (installed_ver and drop_ver
                            and installed_ver == drop_ver):
                        logger.info(
                            "Batch dedup: skipping %s v%s (already installed)",
                            mname, drop_ver)
                        skipped.append((mname, drop_ver))
                        continue
                deduped.append(p)
            if skipped:
                head = "; ".join(f"{n} v{v}" for n, v in skipped[:5])
                tail = (f" (+{len(skipped) - 5} more)"
                        if len(skipped) > 5 else "")
                InfoBar.info(
                    title=tr("infobar.skipped"),
                    content=f"Skipped {len(skipped)} duplicate(s): {head}{tail}",
                    duration=5000, position=InfoBarPosition.TOP, parent=self)
            paths = deduped
            if not paths:
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
                        from cdumm.asi.asi_manager import _stem_from_installed
                        asi_count += sum(
                            1 for f in installed
                            if _stem_from_installed(f) is not None)
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
        _quiet_qprocess(proc)
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
                        # Stream the finished mod's card into the list.
                        # Lightweight single-row DB read (WAL-safe vs
                        # the worker's writes) + one card construct.
                        # An earlier full-refresh approach stalled the
                        # worker stdout pipe for ~400ms per mod (cost
                        # 50-77s per 40-mod batch). This costs <10ms.
                        mid = msg.get("mod_id")
                        if mid and hasattr(self, "paz_mods_page"):
                            try:
                                self.paz_mods_page.stream_add_mod(int(mid))
                            except Exception as _e:
                                logger.debug("stream_add_mod failed: %s", _e)
                elif mtype == "done":
                    pass  # handled in _on_finished

        def _on_finished(exit_code, exit_status):
            from PySide6.QtCore import QProcess as _QProcess
            crashed = exit_status == _QProcess.CrashExit
            if crashed:
                tip.setContent(
                    f"Batch import worker crashed (exit code {exit_code})"
                )
            else:
                tip.setContent(
                    f"Completed! {len(_batch_results)}/{total} imported"
                )
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
                            from cdumm.asi.asi_manager import _stem_from_installed
                            asi_total += sum(
                                1 for f in installed
                                if _stem_from_installed(f) is not None)
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

            # Surface non-fatal partial-skip warnings collected from
            # the batch (multi-file mods that imported but skipped
            # some files due to byte mismatch). Round-10 systematic-
            # debugging fix.
            _info_msgs = [r.get("info") for r in _batch_results
                          if r.get("info")]
            if _info_msgs:
                InfoBar.warning(
                    title=tr("main.import_complete"),
                    content=" • ".join(_info_msgs[:3]) + (
                        f" • +{len(_info_msgs) - 3} more"
                        if len(_info_msgs) > 3 else ""),
                    duration=12000, position=InfoBarPosition.TOP,
                    parent=self)

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
        # Pre-bound mod_id from _queue_import (e.g. nxm:// downloads
        # already know which existing mod they're updating). Falling
        # back to the dup-detection flow below if not bound.
        prebound_id = None
        if hasattr(self, "_existing_mod_id_map"):
            prebound_id = self._existing_mod_id_map.pop(str(path), None)
        existing_mod_id = prebound_id

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
        # Skip when caller pre-bound a mod_id (nxm:// downloads already
        # know which mod they're updating via nexus_mod_id lookup).
        if self._mod_manager and prebound_id is None:
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
                        # If the user clicked the red 'Click To Update'
                        # pill and the download turned out to be the same
                        # version they already have, the pill should
                        # still flip GREEN — they ARE on the latest. The
                        # update-check verdict was based on a stale
                        # local_file_id (filename-vs-API version drift
                        # is the usual cause). Clear in-memory so the
                        # pill renderer paints green on next paint.
                        try:
                            existing_nid = None
                            for m in self._mod_manager.list_mods():
                                if m["id"] == mid:
                                    existing_nid = m.get("nexus_mod_id")
                                    break
                            if (existing_nid
                                    and getattr(self, "_nexus_updates", None)):
                                from cdumm.engine.nexus_api import (
                                    clear_outdated_after_update,
                                )
                                self._nexus_updates = clear_outdated_after_update(
                                    self._nexus_updates,
                                    int(existing_nid),
                                    drop_version,
                                )
                                if hasattr(self, 'paz_mods_page'):
                                    self.paz_mods_page.set_nexus_updates(
                                        self._nexus_updates)
                                if hasattr(self, 'asi_plugins_page'):
                                    try:
                                        self.asi_plugins_page.set_nexus_updates(
                                            self._nexus_updates)
                                    except AttributeError:
                                        pass
                        except Exception as _e:
                            logger.debug(
                                "dedup-skip pill clear failed: %s", _e)
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
                        # Bug 34: snapshot the configurable mod's
                        # selected_labels BEFORE remove_mod cascades the
                        # mod_config row. _restore_selected_labels replays
                        # them onto the new row in the post-import hook
                        # so users don't lose preset picks on click-to-
                        # update.
                        self._pending_selected_labels = (
                            _snapshot_selected_labels(self._db, mid))
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
                        from cdumm.engine.import_handler import install_companion_asis
                        try:
                            game_dir = self._game_dir
                            mods_dir = game_dir / "CDMods" / "mods"
                            ticked_paths = {p for p, _d in selected}
                            # Pass EVERY detected preset so the cog can
                            # toggle the full set later.
                            result = import_multi_variant(
                                presets, path, game_dir, mods_dir, self._db,
                                existing_mod_id=existing_mod_id,
                                initial_selection=ticked_paths)
                            if result:
                                self._store_nexus_metadata_on_row(
                                    int(result["mod_id"]), path)
                            if result and hasattr(self, "_activity_log") and self._activity_log:
                                enabled_ct = sum(1 for v in result["variants"] if v["enabled"])
                                self._activity_log.log(
                                    "import",
                                    f"Imported variant mod: {result['mod_name']}",
                                    f"{len(result['variants'])} variants "
                                    f"({enabled_ct} enabled)")
                            # Mixed-zip companion ASIs: variant path
                            # bypasses the import worker, so any .asi
                            # files that came along in the archive
                            # never get installed. ZapZockt #49.
                            if tmp_extract:
                                from cdumm.asi.asi_manager import AsiManager
                                _amgr = AsiManager(self._game_dir / "bin64")
                                _asi_done = install_companion_asis(
                                    tmp_extract, _amgr)
                                if _asi_done:
                                    logger.info(
                                        "Mixed-zip companion ASIs installed: %s",
                                        _asi_done)
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
                    # but first, install any companion ASIs from the
                    # extracted archive (the worker only sees the JSON
                    # we forward, so it can't stage them itself).
                    # ZapZockt #49.
                    if tmp_extract:
                        try:
                            from cdumm.asi.asi_manager import AsiManager
                            _amgr_sg = AsiManager(self._game_dir / "bin64")
                            _asi_done_sg = install_companion_asis(
                                tmp_extract, _amgr_sg)
                            if _asi_done_sg:
                                logger.info(
                                    "Mixed-zip companion ASIs installed (single): %s",
                                    _asi_done_sg)
                        except Exception as _e_asi:
                            logger.warning(
                                "Single-select companion ASI install failed: %s",
                                _e_asi)
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
        # For archives (zip/7z/rar) with multiple NNNN-variant folders
        # inside, extract first, show the picker, then feed the chosen
        # subfolder to the worker. Otherwise the worker silently auto-
        # picks the alphabetically-last variant and the user never sees
        # the dialog (Vaxis LoD extra-shadows vs no-extra-shadows case).
        try:
            from cdumm.gui.preset_picker import find_folder_variants, FolderVariantDialog
            from cdumm.engine.import_handler import find_loose_file_variants

            scan_dir: Path | None = None
            _tmp_extract_for_picker: Path | None = None
            if path.is_dir():
                scan_dir = path
            elif path.suffix.lower() in (".zip", ".7z", ".rar"):
                import tempfile as _tmp
                _extract_dir = Path(_tmp.mkdtemp(prefix="cdumm_variant_"))
                try:
                    if path.suffix.lower() == ".zip":
                        import zipfile
                        with zipfile.ZipFile(path) as zf:
                            zf.extractall(_extract_dir)
                    elif path.suffix.lower() == ".7z":
                        import py7zr
                        with py7zr.SevenZipFile(path, "r") as zf:
                            zf.extractall(_extract_dir)
                    elif path.suffix.lower() == ".rar":
                        import subprocess
                        _seven = None
                        for t in ("7z", "7z.exe",
                                  r"C:\Program Files\7-Zip\7z.exe"):
                            try:
                                subprocess.run(
                                    [t, "--help"], capture_output=True,
                                    timeout=5,
                                    creationflags=getattr(
                                        subprocess, "CREATE_NO_WINDOW", 0))
                                _seven = t
                                break
                            except (FileNotFoundError,
                                    subprocess.TimeoutExpired):
                                continue
                        if _seven:
                            subprocess.run(
                                [_seven, "x", str(path),
                                 f"-o{_extract_dir}", "-y"],
                                capture_output=True, timeout=120,
                                creationflags=getattr(
                                    subprocess, "CREATE_NO_WINDOW", 0))
                    scan_dir = _extract_dir
                    _tmp_extract_for_picker = _extract_dir
                except Exception as _ee:
                    logger.debug(
                        "Pre-extract for variant picker failed: %s", _ee)
                    scan_dir = None

            if scan_dir is not None:
                # Archive-wide mutex check: if EVERY folder's JSONs
                # target overlapping shop slots (GildsGear pattern —
                # 10 category folders × N alt JSONs, all fighting for
                # the same 93 slots), skip the folder picker entirely
                # and bundle all JSONs into one variant mod. The cog
                # shows every option across every category, labelled
                # with the folder name, so the user can switch between
                # ANY variant without re-dropping the archive.
                try:
                    from cdumm.engine.mutex_json_folder import (
                        collect_archive_mutex_jsons)
                    from cdumm.engine.variant_handler import (
                        import_multi_variant)
                    archive_mutex = collect_archive_mutex_jsons(scan_dir)
                except Exception as _e:
                    logger.debug("archive-wide mutex check failed: %s", _e)
                    archive_mutex = None
                if archive_mutex:
                    logger.info(
                        "Archive-wide mutex detected: %d JSONs across "
                        "all folders — bundling into one variant mod",
                        len(archive_mutex))
                    try:
                        # Rewrite each JSON's name field to the folder-
                        # prefixed label so the cog distinguishes
                        # "AbyssGears / AbyssGear_1" from "Armors /
                        # AllArmor_1". Copy into a staging dir with
                        # disambiguated basenames so variants/ never
                        # gets two files named the same.
                        import tempfile as _tmp
                        import shutil as _sh
                        import re as _re
                        from cdumm.engine.temp_workspace import make_temp_dir
                        staging = make_temp_dir("cdumm_archive_mutex_")
                        prefixed_presets: list[tuple[Path, dict]] = []
                        for src_path, data, label in archive_mutex:
                            # Sanitize Windows-reserved chars too
                            # (< > : " | ? * / \). Prettified folder
                            # or file names can contain ':' (from
                            # "Level 1: Starter") which crashes the
                            # copy mid-loop on Windows. E3.
                            safe = label.replace(" / ", "__")
                            safe = _re.sub(r'[<>:"|?*\\/]', '_', safe)
                            safe = safe.replace(" ", "_")
                            dest = staging / f"{safe}.json"
                            _sh.copy2(src_path, dest)
                            # Tag the data so the cog label reads
                            # "Abyss Gears / AbyssGear_1" — name takes
                            # precedence over filename in the variant
                            # label builder.
                            d2 = dict(data)
                            d2["name"] = label
                            prefixed_presets.append((dest, d2))
                        mods_dir = self._game_dir / "CDMods" / "mods"
                        initial = {prefixed_presets[0][0]}
                        _original = getattr(
                            self, "_original_drop_path", None) or path
                        source_for_name = (
                            _original
                            if isinstance(_original, Path)
                            and _original.exists()
                            else path)
                        mv_result = import_multi_variant(
                            prefixed_presets, source_for_name,
                            self._game_dir, mods_dir, self._db,
                            existing_mod_id=existing_mod_id,
                            initial_selection=initial)
                        if mv_result:
                            self._store_nexus_metadata_on_row(
                                int(mv_result["mod_id"]),
                                source_for_name if isinstance(
                                    source_for_name, Path) else path)
                        if (mv_result and hasattr(self, "_activity_log")
                                and self._activity_log):
                            self._activity_log.log(
                                "import",
                                f"Imported variant mod: "
                                f"{mv_result['mod_name']}",
                                f"{len(mv_result['variants'])} "
                                f"alternatives (1 enabled)")
                        if mv_result:
                            self._refresh_all()
                        # Mixed-zip companion ASI install (archive-wide
                        # mutex branch). ZapZockt #49 case where the
                        # archive may carry both JSON variants and a
                        # companion .asi.
                        if _tmp_extract_for_picker is not None:
                            try:
                                from cdumm.engine.import_handler import (
                                    install_companion_asis)
                                from cdumm.asi.asi_manager import AsiManager
                                _amgr_aw = AsiManager(self._game_dir / "bin64")
                                _asi_aw = install_companion_asis(
                                    _tmp_extract_for_picker, _amgr_aw)
                                if _asi_aw:
                                    logger.info(
                                        "Mixed-zip companion ASIs (archive-mutex): %s",
                                        _asi_aw)
                            except Exception as _e_asi_aw:
                                logger.warning(
                                    "Archive-mutex companion ASI install failed: %s",
                                    _e_asi_aw)
                    except Exception as _amv_e:
                        logger.error(
                            "archive-wide import_multi_variant "
                            "failed: %s", _amv_e, exc_info=True)
                    # Clean up the pre-extract AND the archive-mutex
                    # staging temp. Both are needed only until
                    # import_multi_variant has copied the JSONs into
                    # CDMods/sources/<id>/variants/; leaking them
                    # until atexit/sweep was the C-M1 issue.
                    from cdumm.engine.temp_workspace import release_temp_dir
                    try:
                        release_temp_dir(staging)
                    except Exception:
                        pass
                    if _tmp_extract_for_picker is not None:
                        import shutil
                        shutil.rmtree(
                            _tmp_extract_for_picker,
                            ignore_errors=True)
                    self._process_next_import()
                    return

                # Walk the variant tree. Some mods nest variants (Character
                # Creator for example ships Female/Male at the top and each
                # has Goblin/Human/Orc inside). After the user picks a level,
                # descend and check if the chosen folder has its own
                # sub-variants. Cap at MAX_VARIANT_DEPTH to avoid infinite
                # loops on pathological inputs.
                MAX_VARIANT_DEPTH = 4
                _current = scan_dir
                _fired_any_picker = False
                _aborted = False
                for _depth in range(MAX_VARIANT_DEPTH):
                    folder_vars = find_folder_variants(_current)
                    if len(folder_vars) < 2:
                        _loose = find_loose_file_variants(_current)
                        if len(_loose) >= 2:
                            folder_vars = [c["_base_dir"] for c in _loose]
                    if len(folder_vars) < 2:
                        # Format 3 variant pack (e.g. CrimsonWings
                        # ships 5 .field.json levels in one ZIP). Same
                        # picker shape — surface the materialised
                        # variant dirs so the user picks one level.
                        from cdumm.engine.import_handler import (
                            find_format3_variants,
                        )
                        _f3 = find_format3_variants(_current)
                        if len(_f3) >= 2:
                            folder_vars = [c["_base_dir"] for c in _f3]
                    if len(folder_vars) < 2:
                        break  # leaf — no more variant choices to offer

                    fv_dialog = FolderVariantDialog(folder_vars, self)
                    result = fv_dialog.exec()
                    picks = list(getattr(fv_dialog, "selected_paths", []) or [])
                    if not (result and picks):
                        _aborted = True
                        break
                    # Multi-pick mega-pack (GildsGear / AIO pack with
                    # independent categories): queue the extra picks as
                    # additional imports so each category folder becomes
                    # its own set of mods. The first pick is imported
                    # now via the normal flow.
                    if len(picks) > 1:
                        for extra in picks[1:]:
                            try:
                                self._queue_import(extra)
                            except Exception as _qe:
                                logger.debug(
                                    "queue extra category folder failed "
                                    "(%s): %s", extra, _qe)
                        logger.info(
                            "Multi-pick category folders (depth %d): "
                            "first=%s, queued %d more",
                            _depth, picks[0].name, len(picks) - 1)
                    _current = picks[0]
                    _fired_any_picker = True
                    logger.info(
                        "User selected folder variant (depth %d): %s",
                        _depth, _current.name)

                if _aborted:
                    if _tmp_extract_for_picker:
                        import shutil
                        shutil.rmtree(
                            _tmp_extract_for_picker, ignore_errors=True)
                    self._process_next_import()
                    return

                if _fired_any_picker:
                    # Stash the variant's relative path from the scan
                    # root so post-import can record "<archive>||<rel>"
                    # into drop_name for cog active-state matching.
                    try:
                        self._variant_leaf_rel = _current.relative_to(
                            scan_dir).as_posix()
                    except (ValueError, AttributeError):
                        self._variant_leaf_rel = _current.name
                    # Collect any .asi plugins that ship alongside the
                    # variants (Character Creator bundles
                    # CharacterCreatorAsi/CharacterCreatorHead.asi at
                    # the top level). Install them separately after
                    # the variant import completes.
                    _asi_files = [
                        f for f in scan_dir.rglob("*")
                        if f.is_file() and f.suffix.lower() == ".asi"
                    ]
                    if _asi_files:
                        self._pending_asi_from_variant = _asi_files
                        logger.info(
                            "Variant archive bundles %d ASI file(s) — "
                            "will install after main import: %s",
                            len(_asi_files),
                            [a.name for a in _asi_files])
                    path = _current
                    # Variant picker fired and `_current` lives inside the
                    # pre-extract temp dir. Schedule the temp for cleanup
                    # AFTER the worker finishes (see _on_finished). Doing
                    # it now would rug-pull the path the worker is about
                    # to read.
                    if _tmp_extract_for_picker is not None:
                        self._pending_variant_cleanup = _tmp_extract_for_picker
                    # Remember the original archive/folder path so the
                    # cog-side panel can re-show FolderVariantDialog
                    # and swap variants without re-dropping the file.
                    # For archives we stash the archive path itself;
                    # for pre-extracted folders we stash the parent
                    # that contains the variant subfolders.
                    _original = getattr(
                        self, "_original_drop_path", None) or path
                    if (isinstance(_original, Path) and _original.exists()
                            and (_original.is_file() or _original.is_dir())):
                        self._configurable_source = str(_original)

                    # Mutex-JSON folder detection. GildsGear-style packs
                    # put N alternative JSONs (all patching the same
                    # shop slots, different items) inside each category
                    # folder. Treating each as its own mod creates N
                    # confusing rows where only one can be enabled at a
                    # time anyway. Route to import_multi_variant so the
                    # user gets ONE card with a cog radio picker,
                    # matching the author's 'install one, switch later'
                    # workflow. The original archive is passed as
                    # `source` so the mod name reflects the archive
                    # (e.g. 'Gild's Gear') not the picked folder.
                    try:
                        from cdumm.engine.mutex_json_folder import (
                            detect_mutex_folder_jsons)
                        from cdumm.engine.variant_handler import (
                            import_multi_variant)
                        mutex_presets = detect_mutex_folder_jsons(_current)
                    except Exception as _e:
                        logger.debug("mutex detection failed: %s", _e)
                        mutex_presets = None
                    if mutex_presets:
                        logger.info(
                            "Mutex-JSON folder detected at %s — routing "
                            "to import_multi_variant (%d alternatives)",
                            _current.name, len(mutex_presets))
                        try:
                            mods_dir = self._game_dir / "CDMods" / "mods"
                            # Default: first JSON enabled, rest disabled
                            # (they're mutex — enabling more makes no
                            # sense). User can swap via cog later.
                            initial = {mutex_presets[0][0]}
                            source_for_name = (
                                _original
                                if isinstance(_original, Path)
                                and _original.exists()
                                else _current)
                            mv_result = import_multi_variant(
                                mutex_presets, source_for_name,
                                self._game_dir, mods_dir, self._db,
                                existing_mod_id=existing_mod_id,
                                initial_selection=initial)
                            if mv_result:
                                self._store_nexus_metadata_on_row(
                                    int(mv_result["mod_id"]),
                                    source_for_name if isinstance(
                                        source_for_name, Path) else path)
                            if (mv_result and hasattr(self, "_activity_log")
                                    and self._activity_log):
                                self._activity_log.log(
                                    "import",
                                    f"Imported variant mod: "
                                    f"{mv_result['mod_name']}",
                                    f"{len(mv_result['variants'])} "
                                    f"alternatives (1 enabled)")
                            if mv_result:
                                self._refresh_all()
                            # Mixed-zip companion ASI install (mutex-folder
                            # branch). ZapZockt #49.
                            if _tmp_extract_for_picker is not None:
                                try:
                                    from cdumm.engine.import_handler import (
                                        install_companion_asis)
                                    from cdumm.asi.asi_manager import AsiManager
                                    _amgr_mx = AsiManager(self._game_dir / "bin64")
                                    _asi_mx = install_companion_asis(
                                        _tmp_extract_for_picker, _amgr_mx)
                                    if _asi_mx:
                                        logger.info(
                                            "Mixed-zip companion ASIs (mutex): %s",
                                            _asi_mx)
                                except Exception as _e_asi_mx:
                                    logger.warning(
                                        "Mutex companion ASI install failed: %s",
                                        _e_asi_mx)
                        except Exception as _mv_e:
                            logger.error(
                                "Mutex import_multi_variant failed: %s",
                                _mv_e, exc_info=True)
                        # Clean up the variant-picker pre-extract temp
                        # immediately, we're not going through the
                        # worker path that normally handles it.
                        if _tmp_extract_for_picker is not None:
                            import shutil
                            shutil.rmtree(
                                _tmp_extract_for_picker,
                                ignore_errors=True)
                        self._process_next_import()
                        return
                elif _tmp_extract_for_picker is not None:
                    # No variant picker fired. The pre-extract was a scan-
                    # only operation — throw it away and let the worker
                    # re-extract from the original archive. Reusing the
                    # temp path would leak the folder AND cause the mod to
                    # be named after the temp dir (cdumm_variant_XXXX)
                    # instead of the archive's filename.
                    import shutil
                    shutil.rmtree(
                        _tmp_extract_for_picker, ignore_errors=True)
        except Exception as e:
            logger.debug("Folder variant check failed: %s", e)

        # ── 6. Toggle picker ──────────────────────────────────────────
        try:
            from cdumm.engine.json_patch_handler import (
                detect_json_patch, detect_json_patches_all, has_labeled_changes)
            json_data = None
            if path.suffix.lower() == '.json':
                json_data = detect_json_patch(path)
            elif path.is_dir():
                # Archives with multiple JSON patches (Unlimited Dragon
                # Flying ships main + RegionDismountRemoval; user
                # naturally expects to configure each via its own cog
                # AFTER import, not via a single drop-time dialog that
                # arbitrarily picked one of them). Only show the toggle
                # picker at drop time when the archive has exactly ONE
                # configurable JSON — otherwise skip it and let each
                # imported mod's cog surface the toggles per-mod.
                all_json = detect_json_patches_all(path)
                if len(all_json) == 1:
                    json_data = all_json[0]
                else:
                    json_data = None

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

        _probe_console_state("before import QProcess(self)")
        proc = QProcess(self)
        _quiet_qprocess(proc)
        _probe_console_state("after import QProcess(self)")
        self._active_worker = proc  # reuse guard flag

        # Snapshot the per-import context attributes set by the caller
        # (usually mods_page swap branches) BEFORE any other code path
        # can clobber them. Concurrent swaps would otherwise race the
        # shared self._update_priority / self._configurable_source /
        # self._original_drop_path fields between launch and finished.
        # These snapshots are attached to `proc` and read from the
        # closure-captured proc in _on_finished, so each proc's handler
        # sees the context that belonged to ITS import.
        # NOTE: snapshot_and_clear_import_context is imported at module
        # top — see comment near the top-level import for why.
        proc._cdumm_ctx = snapshot_and_clear_import_context(self)

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
                    # Non-fatal info banner — set by importers when a
                    # mod imported successfully but with notable
                    # caveats (e.g., multi-file partial-skip with N
                    # files dropped due to byte mismatch). Surfaced
                    # post-import as a yellow InfoBar so users see
                    # the warning without it being treated as an
                    # error. Round-9 systematic-debugging fix.
                    self._import_result_info = msg.get("info")
                    # Capture the PRIMARY mod id so post-import updates
                    # (drop_name, nexus_id, game_version_hash, priority,
                    # enabled) land on the right row. Compound imports
                    # create sibling rows AFTER the primary; SELECT MAX(id)
                    # lands on the wrong one.
                    self._import_result_mod_id = msg.get("mod_id")
                elif msg.get("type") == "error":
                    self._import_result_name = path.stem
                    self._import_result_error = msg.get("msg", "Unknown error")
                    self._import_result_mod_id = None

        def _on_finished(exit_code, exit_status):
            tip.setContent(tr("progress.completed"))
            tip.setState(True)
            proc.deleteLater()
            self._active_worker = None
            self._active_progress = None

            # If the worker subprocess crashed (segfault / unhandled
            # native exception) without emitting a JSON `done`/`error`
            # line, the previous code silently flowed into the success
            # branch and showed "Import succeeded". Catch CrashExit
            # explicitly so the user sees the real failure mode.
            # Round 4 GUI/worker audit catch.
            from PySide6.QtCore import QProcess as _QProcess
            if exit_status == _QProcess.CrashExit:
                if not getattr(self, '_import_result_error', None):
                    self._import_result_error = (
                        f"Import worker crashed (exit code {exit_code}). "
                        f"This usually means a native extension hit a "
                        f"segfault or the worker was killed externally. "
                        f"Try the import again; if it persists, attach "
                        f"the bug report and the cdumm_worker.log file "
                        f"to a new GitHub issue."
                    )

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
            # Clean variant-picker pre-extract dir (when a picker fired
            # and the worker was fed a path inside this temp tree).
            vtmp = getattr(self, '_pending_variant_cleanup', None)
            if vtmp:
                import shutil
                shutil.rmtree(str(vtmp), ignore_errors=True)
                self._pending_variant_cleanup = None

            # Primary mod id captured from the worker's "done" message.
            # Compound imports (Lightsaber: CB + shop JSON siblings) write
            # MULTIPLE mod rows, so SELECT MAX(id) would land on the last
            # sibling, not the primary. Use the captured id when we have it;
            # only fall back to MAX(id) for legacy paths that don't plumb it.
            primary_mod_id = getattr(self, '_import_result_mod_id', None)

            def _primary_id():
                """Resolve the post-import target row via the shared
                _resolve_post_import_target_id helper — same prefer-
                list-over-MAX(id) logic the nxm:// flow needs."""
                max_row_id = None
                try:
                    row = self._db.connection.execute(
                        "SELECT MAX(id) FROM mods").fetchone()
                    max_row_id = row[0] if row else None
                except Exception:
                    pass
                return _resolve_post_import_target_id(
                    result_mod_id=primary_mod_id,
                    existing_mod_id=getattr(
                        self, '_last_existing_mod_id', None),
                    max_row_id=max_row_id,
                )

            # Post-import: game version stamp
            try:
                from cdumm.engine.version_detector import detect_game_version
                ver = detect_game_version(self._game_dir)
                _pid = _primary_id()
                if ver and _pid is not None:
                    self._db.connection.execute(
                        "UPDATE mods SET game_version_hash = ? WHERE id = ?",
                        (ver, _pid))
                    self._db.connection.commit()
            except Exception:
                pass

            # Per-launch context snapshot (see _launch_import_worker):
            # reads pull from this dict, not self.<attr>, so concurrent
            # swap/update launches can't clobber our values.
            _ctx = getattr(proc, "_cdumm_ctx", {}) or {}

            # Post-import: NexusMods mod ID from filename. Nested-variant
            # mods land at a leaf like 'Human' which never carries the
            # Nexus id — parse from the ORIGINAL dropped archive
            # (Character Creator-837-4-2-1776536785.zip) when available.
            _orig_for_nexus = _ctx.get("original_drop_path")
            if _orig_for_nexus is not None:
                _nexus_stem = (_orig_for_nexus.stem
                               if _orig_for_nexus.is_file()
                               else _orig_for_nexus.name)
            else:
                _nexus_stem = path.stem
            nexus_id, nexus_file_ver = _parse_nexus_filename(_nexus_stem)
            if nexus_id:
                try:
                    _pid = _primary_id()
                    if _pid is not None:
                        self._db.connection.execute(
                            "UPDATE mods SET nexus_mod_id = ?, nexus_file_id = ? "
                            "WHERE id = ?",
                            (nexus_id, nexus_file_ver, _pid))
                        self._db.connection.commit()
                        logger.info("Stored NexusMods ID: mod=%d file=%s",
                                    nexus_id, nexus_file_ver)
                except Exception:
                    pass

            # Persist the real numeric file_id when an nxm:// download
            # provided it. This is what enables the file_updates chain
            # walk on subsequent update checks (no more "downloaded
            # the wrong variant" bugs).
            real_file_id = None
            if hasattr(self, '_nexus_real_file_id_map'):
                real_file_id = self._nexus_real_file_id_map.pop(
                    str(path), None)
            if real_file_id:
                try:
                    _pid = _primary_id()
                    if _pid is not None:
                        self._db.connection.execute(
                            "UPDATE mods SET nexus_real_file_id = ? "
                            "WHERE id = ?",
                            (int(real_file_id), _pid))
                        self._db.connection.commit()
                        logger.info(
                            "Stored Nexus real file_id=%d on row %d",
                            int(real_file_id), _pid)
                except Exception as e:
                    logger.debug(
                        "Failed to store nexus_real_file_id: %s", e)

            # Invalidate the cached "outdated" entry for this mod's
            # nexus_mod_id so the red "Click To Update" pill turns
            # green immediately, instead of waiting up to 30 minutes
            # for the next background update check.
            if nexus_id and hasattr(self, "_nexus_updates") \
                    and self._nexus_updates:
                try:
                    from cdumm.engine.nexus_api import clear_outdated_after_update
                    # clear_outdated_after_update(updates, nexus_mod_id, new_version)
                    # — pass the just-imported version (parsed from the
                    # Nexus filename, falls back to whatever local row
                    # has) so the GREEN pill carries an accurate
                    # version label.
                    new_ver = (nexus_file_ver or "").strip()
                    if not new_ver:
                        try:
                            row = self._db.connection.execute(
                                "SELECT version FROM mods WHERE id = ?",
                                (_pid,)).fetchone()
                            new_ver = (row[0] if row and row[0] else "").strip()
                        except Exception:
                            new_ver = ""
                    self._nexus_updates = clear_outdated_after_update(
                        self._nexus_updates, int(nexus_id), new_ver)
                    if hasattr(self, 'paz_mods_page'):
                        self.paz_mods_page.set_nexus_updates(
                            self._nexus_updates)
                    if hasattr(self, 'asi_plugins_page'):
                        try:
                            self.asi_plugins_page.set_nexus_updates(
                                self._nexus_updates)
                        except AttributeError:
                            pass
                except Exception as e:
                    logger.debug("pill-clear failed: %s", e)

            # Post-import: store original drop name + extract version
            try:
                mod_id = _primary_id()
                orig = _ctx.get("original_drop_path")
                drop_name = orig.name if orig else path.name
                # Nested-variant mods: append "||<rel>" so the cog can
                # identify which leaf is active and rewrite the mod's
                # name to the pretty archive name (otherwise the card
                # would read just the leaf folder name like "Human").
                variant_rel = _ctx.get("variant_leaf_rel")
                if variant_rel:
                    drop_name = f"{drop_name}||{variant_rel}"
                    try:
                        from cdumm.engine.import_handler import (
                            prettify_mod_name)
                        pretty = prettify_mod_name(
                            orig.stem if orig and orig.is_file()
                            else (orig.name if orig else path.name))
                        if pretty:
                            self._db.connection.execute(
                                "UPDATE mods SET name = ? WHERE id = ?",
                                (pretty, mod_id))
                    except Exception as _e:
                        logger.debug("variant rename failed: %s", _e)
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
            cfg_src = _ctx.get("configurable_source")
            if cfg_src:
                try:
                    mod_id = _primary_id()
                    if mod_id is not None:
                        self._db.connection.execute(
                            "UPDATE mods SET configurable = 1, source_path = ? WHERE id = ?",
                            (cfg_src, mod_id))
                        self._db.connection.commit()
                        labels = _ctx.get("configurable_labels")
                        if labels:
                            # Use the upsert helper so any previously-
                            # saved custom_values on this mod_config row
                            # survive. INSERT OR REPLACE here would
                            # delete and recreate the row with only
                            # selected_labels — wiping custom_values.
                            from cdumm.engine.variant_handler import (
                                _persist_selected_labels)
                            _persist_selected_labels(
                                self._db, mod_id, labels)
                            self._db.connection.commit()
                except Exception:
                    pass

            # Post-import: restore update state (priority/enabled)
            upri = _ctx.get("update_priority")
            if upri is not None:
                try:
                    mod_id = _primary_id()
                    if mod_id is not None:
                        self._db.connection.execute(
                            "UPDATE mods SET priority = ?, enabled = ? WHERE id = ?",
                            (upri, _ctx.get("update_enabled") or 0, mod_id))
                        self._db.connection.commit()
                except Exception:
                    pass

            # Post-import: restore selected_labels (variant/preset picks)
            # that were snapshotted BEFORE the dup-remove ran. See Bug 34:
            # click-to-update would wipe users' picks because the old row
            # was cascade-deleted before the new one could inherit them.
            snap = getattr(self, '_pending_selected_labels', None)
            if snap:
                try:
                    _pid = _primary_id()
                    if _pid is not None:
                        import json as _json
                        cur = self._db.connection.execute(
                            "SELECT selected_labels FROM mod_config "
                            "WHERE mod_id = ?", (_pid,)).fetchone()
                        available = set()
                        if cur and cur[0]:
                            try:
                                available = set(
                                    _json.loads(cur[0]).keys())
                            except Exception:
                                available = set()
                        if not available:
                            available = set(snap.keys())
                        _restore_selected_labels(
                            self._db, _pid, snap, available)
                except Exception as _e:
                    logger.debug("restore preset labels failed: %s", _e)
                self._pending_selected_labels = None

            # Bug #14: clear any scratch state that didn't come through
            # import_context (nexus_real_file_id_map entry, etc.) so the
            # next import doesn't inherit stale values.
            _clear_pending_post_import_state(self, path)

            # Install staged ASI files from mixed ZIP import
            asi_staged = getattr(self, '_import_result_asi_staged', [])
            # Character Creator style: top-level .asi alongside variant
            # folders. The variant picker detects them and stashes them
            # here so they install alongside whichever gender/race was
            # picked.
            variant_asi = getattr(self, '_pending_asi_from_variant', None)
            if variant_asi:
                asi_staged = list(asi_staged) + [str(p) for p in variant_asi]
                self._pending_asi_from_variant = None
            asi_count = 0
            if asi_staged:
                try:
                    from cdumm.asi.asi_manager import AsiManager
                    _asi_mgr = AsiManager(self._game_dir / "bin64")
                    from pathlib import Path as _P
                    _staging_parents: set[_P] = set()
                    for asi_path in asi_staged:
                        p = _P(asi_path)
                        if p.exists() and p.suffix.lower() == ".asi":
                            import shutil
                            shutil.copy2(str(p), str(_asi_mgr._bin64 / p.name))
                            asi_count += 1
                            _staging_parents.add(p.parent)
                        elif p.exists() and p.suffix.lower() == ".ini":
                            import shutil
                            shutil.copy2(str(p), str(_asi_mgr._bin64 / p.name))
                            _staging_parents.add(p.parent)
                    logger.info("Installed %d ASI plugin(s) from mixed ZIP", asi_count)
                    # Clean up the per-import staging subdir(s) so they
                    # don't pile up under deltas_dir/_asi_staging/.
                    import shutil as _sh
                    for parent in _staging_parents:
                        if parent.exists() and parent.parent.name == "_asi_staging":
                            _sh.rmtree(parent, ignore_errors=True)
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
            # Surface non-fatal info banner from the importer so the
            # user sees partial-skip warnings (multi-file mods that
            # imported with some files dropped) without it being
            # treated as an error. Round-9 systematic-debugging fix.
            _info = getattr(self, '_import_result_info', None)
            if _info:
                InfoBar.warning(
                    title=tr("main.import_complete"),
                    content=_info, duration=10000,
                    position=InfoBarPosition.TOP, parent=self)
                self._import_result_info = None
            self._log_activity("import", tr("activity.msg_imported_mod", name=name))
            QTimer.singleShot(100, self._process_next_import)

        def _on_stderr():
            data = proc.readAllStandardError().data().decode("utf-8", errors="replace")
            if data.strip():
                logger.info("Import worker stderr: %s", data.strip()[:500])

        proc.readyReadStandardOutput.connect(_on_stdout)
        proc.readyReadStandardError.connect(_on_stderr)
        proc.finished.connect(_on_finished)

        # Stall watchdog mirrored from _run_qprocess. Without this the
        # import can hang forever (silent 7z deadlock, infinite-loop in
        # an inner archive scan, etc.) and the user sees a frozen
        # progress tip with no way out. Round 4 GUI/worker audit catch.
        # Threshold is more lenient than apply (5 min vs 3 min) because
        # large multi-variant archives can legitimately take a while
        # to extract + parse.
        import time as _time
        from PySide6.QtCore import QTimer as _QTimer
        IMPORT_STALL_THRESHOLD_S = 300
        _wd_state = {"last_activity": _time.time(), "killed": False}
        _wd_orig_on_stdout = _on_stdout

        def _on_stdout_with_wd():
            _wd_state["last_activity"] = _time.time()
            _wd_orig_on_stdout()
        try:
            proc.readyReadStandardOutput.disconnect(_on_stdout)
        except (RuntimeError, TypeError):
            pass
        proc.readyReadStandardOutput.connect(_on_stdout_with_wd)

        watchdog = _QTimer(self)
        watchdog.setInterval(5000)

        def _on_wd_tick():
            if _wd_state["killed"]:
                return
            elapsed = _time.time() - _wd_state["last_activity"]
            if elapsed > IMPORT_STALL_THRESHOLD_S and proc.state() != 0:
                _wd_state["killed"] = True
                watchdog.stop()
                logger.error(
                    "Import watchdog: no progress for %ss, killing PID %s",
                    int(elapsed), proc.processId())
                proc.kill()
                self._import_result_error = (
                    f"Import stalled for over {IMPORT_STALL_THRESHOLD_S}s "
                    f"with no progress and was stopped. Drop the mod "
                    f"again, or open it in 7-Zip first to verify the "
                    f"archive isn't corrupt."
                )
        watchdog.timeout.connect(_on_wd_tick)
        # Stop watchdog when proc finishes
        proc.finished.connect(lambda *_: watchdog.stop())

        _probe_console_state("before proc.start(exe, args)")
        proc.start(exe, args)
        _probe_console_state("after proc.start(exe, args)")
        watchdog.start()
        logger.info("Import QProcess started: PID %s exe=%s args=%s",
                     proc.processId(), exe, args)

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
                from cdumm.asi.asi_manager import _resolve_version_filename
                bin64 = self._game_dir / "bin64"
                for asi_name in installed:
                    ver_filename = _resolve_version_filename(asi_name)
                    if ver_filename is None:
                        continue
                    ver_file = bin64 / ver_filename
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
        # Bug 35: pick up the real numeric Nexus file_id if this install
        # came through the nxm:// flow. _nexus_real_file_id_map is
        # keyed on the original drop path; look it up and pop so a
        # later import doesn't inherit the value.
        real_file_id = 0
        try:
            nrf = getattr(self, "_nexus_real_file_id_map", None)
            if isinstance(nrf, dict):
                real_file_id = int(nrf.pop(str(source_path), 0) or 0)
        except Exception:
            real_file_id = 0
        if not version and not nexus_id and not real_file_id:
            return
        from cdumm.asi.asi_manager import _stem_from_installed
        for fname in installed_files:
            plugin_name = _stem_from_installed(fname)
            if plugin_name is None:
                continue
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
                if real_file_id:
                    # Bug 35: persist the actual Nexus file_id so the
                    # next update check walks the file_updates chain
                    # for this ASI plugin instead of the fragile name
                    # match.
                    self._db.connection.execute(
                        "UPDATE asi_plugin_state SET nexus_real_file_id = ? "
                        "WHERE name = ?",
                        (real_file_id, plugin_name))
                self._db.connection.commit()
                logger.info(
                    "Stored ASI metadata: %s version=%s nexus_mod_id=%s "
                    "real_file_id=%s",
                    plugin_name, version, nexus_id, real_file_id or None)

                # Author-renamed-asi cleanup: when the new file is named
                # differently from the prior version (e.g. the author
                # baked the version into the filename — EnhancedFlight.asi
                # -> EnhancedFlightv31.asi), the old .asi is still in
                # bin64/ AND its asi_plugin_state row still says
                # outdated. Game would load BOTH at startup. Delete the
                # stale prior file + DB row.
                if nexus_id:
                    try:
                        stale_rows = self._db.connection.execute(
                            "SELECT name FROM asi_plugin_state "
                            "WHERE nexus_mod_id = ? AND name != ?",
                            (nexus_id, plugin_name)).fetchall()
                        for (old_name,) in stale_rows:
                            bin64_dir = self._game_dir / "bin64"
                            # Sweep every variant the old plugin could
                            # have on disk: enabled (.asi), disabled
                            # (.asi.disabled — from the right-click
                            # Disable action), companion .ini, and
                            # companion .ini.disabled. Without this
                            # the page's scan() picks up the .disabled
                            # variant and renders a ghost card.
                            stale_paths = [
                                bin64_dir / f"{old_name}.asi",
                                bin64_dir / f"{old_name}.asi.disabled",
                                bin64_dir / f"{old_name}.ini",
                                bin64_dir / f"{old_name}.ini.disabled",
                                bin64_dir / f"{old_name}.version",
                            ]
                            for stale in stale_paths:
                                try:
                                    if stale.exists():
                                        stale.unlink()
                                        logger.info(
                                            "ASI rename cleanup: removed "
                                            "stale %s", stale.name)
                                except OSError as oe:
                                    logger.warning(
                                        "ASI rename cleanup: could not "
                                        "remove %s: %s", stale, oe)
                            self._db.connection.execute(
                                "DELETE FROM asi_plugin_state "
                                "WHERE name = ?",
                                (old_name,))
                            logger.info(
                                "ASI rename cleanup: removed stale "
                                "asi_plugin_state row %r", old_name)
                        if stale_rows:
                            self._db.connection.commit()
                    except Exception as cleanup_err:
                        logger.warning(
                            "ASI rename cleanup failed: %s", cleanup_err)
            except Exception as e:
                logger.warning("Failed to store ASI metadata: %s", e)

        # Mirror the PAZ post-import pill clear: when an ASI plugin
        # update completes the in-memory _nexus_updates dict still
        # marks the mod as outdated, so the pill stays red until the
        # next 30-min background check. Flip it to GREEN now using
        # the version we just stored.
        if nexus_id and getattr(self, "_nexus_updates", None):
            try:
                from cdumm.engine.nexus_api import clear_outdated_after_update
                new_ver = (version or "").strip()
                self._nexus_updates = clear_outdated_after_update(
                    self._nexus_updates, int(nexus_id), new_ver)
                if hasattr(self, 'asi_plugins_page'):
                    try:
                        self.asi_plugins_page.set_nexus_updates(
                            self._nexus_updates)
                    except AttributeError:
                        pass
                if hasattr(self, 'paz_mods_page'):
                    try:
                        self.paz_mods_page.set_nexus_updates(
                            self._nexus_updates)
                    except AttributeError:
                        pass
            except Exception as e:
                logger.debug("ASI pill-clear failed: %s", e)

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

    def _store_nexus_metadata_on_row(self, mod_id: int,
                                       drop_path: Path) -> None:
        """Persist `nexus_mod_id`, `nexus_file_id`, `nexus_real_file_id`
        on a mod row by parsing the drop filename and consuming the
        nxm:// real-file-id map.

        The QProcess-worker path does this inline in `_on_finished`.
        Variant imports run in-process (no worker) so they need an
        explicit metadata-storage step, otherwise the resulting row
        keeps `nexus_mod_id=NULL` and the pill shows grey/no entry on
        the next update check.
        """
        if not self._db or not mod_id:
            return
        try:
            nexus_id, nexus_file_ver = _parse_nexus_filename(drop_path.stem)
        except Exception:
            nexus_id, nexus_file_ver = None, ""
        real_file_id = None
        if hasattr(self, "_nexus_real_file_id_map"):
            real_file_id = self._nexus_real_file_id_map.pop(
                str(drop_path), None)
        try:
            if nexus_id:
                self._db.connection.execute(
                    "UPDATE mods SET nexus_mod_id = ?, nexus_file_id = ? "
                    "WHERE id = ?",
                    (int(nexus_id), nexus_file_ver, int(mod_id)))
            if real_file_id:
                self._db.connection.execute(
                    "UPDATE mods SET nexus_real_file_id = ? WHERE id = ?",
                    (int(real_file_id), int(mod_id)))
            if nexus_id or real_file_id:
                self._db.connection.commit()
                logger.info(
                    "Stored Nexus metadata on variant mod_id=%d: "
                    "nexus_mod_id=%s file_ver=%s real_file_id=%s",
                    mod_id, nexus_id, nexus_file_ver, real_file_id)
        except Exception as e:
            logger.debug(
                "Failed to store Nexus metadata for variant mod %d: %s",
                mod_id, e)

    def _match_existing_by_name(self, downloaded_path: str | Path) -> int | None:
        """Return ``mods.id`` of an existing row whose name matches the
        download stem (exact, prettified) — used by the nxm:// flow when
        the nexus_mod_id lookup misses because the existing row was
        imported as a local zip with no Nexus metadata.

        Only returns when there's exactly ONE exact-match row. Multiple
        matches mean the user already has duplicates; we don't try to
        guess which one to update. ``None`` means "no safe binding"
        and the caller imports as new.
        """
        if not self._mod_manager:
            return None
        from cdumm.engine.mod_matching import is_same_mod
        path = Path(downloaded_path) if not isinstance(
            downloaded_path, Path) else downloaded_path
        drop_name = path.stem
        # The drop is a Nexus filename like
        # 'CDInventoryExpander-56-2-7-1776xxx'. _find_existing_mod's
        # logic for stripping that prefix would be ideal, but the
        # parser handles it: prefer the parsed mod name, fall back
        # to the filename stem.
        try:
            from cdumm.engine.import_handler import _read_modinfo
            if path.is_dir():
                modinfo = _read_modinfo(path)
                if modinfo and modinfo.get("name"):
                    drop_name = modinfo["name"]
        except Exception:
            pass
        candidates = [
            m for m in self._mod_manager.list_mods()
            if is_same_mod(drop_name, m["name"])
        ]
        if len(candidates) != 1:
            return None
        return candidates[0]["id"]

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

        # D1: refuse to apply while a detected game update is
        # outstanding. Applying on a stale snapshot is the root
        # cause of most "stuck at 2%" reports — patches land on
        # the wrong bytes because our vanilla baseline no longer
        # matches the live game.
        from cdumm.gui.apply_watchdog import (
            is_apply_blocked_by_stale_snapshot,
        )
        if is_apply_blocked_by_stale_snapshot(self._startup_context):
            InfoBar.error(
                title="Rescan required",
                content=(
                    "Crimson Desert was updated since your last "
                    "snapshot. Apply is locked until you run Rescan "
                    "Game Files — otherwise mods land on the wrong "
                    "bytes and won't work. Use the Rescan panel in "
                    "the sidebar to unlock Apply."),
                duration=-1, position=InfoBarPosition.TOP, parent=self)
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
            # F3: successful apply → stamp every enabled mod with
            # the current game fingerprint. These mods just produced
            # patches that landed cleanly, so they're "known-good"
            # on this version. Clears the orange "outdated" badge
            # and stops the Post-Apply Verification dialog from
            # flagging them as "imported on a different version".
            try:
                from cdumm.engine.version_detector import (
                    stamp_enabled_mods_as_current,
                )
                stamp_enabled_mods_as_current(self._db, self._game_dir)
            except Exception as e:
                logger.debug("stamp after apply failed: %s", e)
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

        # F3: the version-hash comparison used to live here and
        # append "may be outdated" to every mod that was imported on
        # a prior game version. That's speculation, not verification
        # — after every Steam patch it drowned out real PAPGT/PAMT
        # failures with 20+ false-positive lines. The real signal for
        # "this mod might crash" is patch byte mismatches during
        # apply (already loudly logged by json_patch_handler and
        # surfaced as apply errors). Successful apply now auto-stamps
        # mods with the current fingerprint so the orange "outdated"
        # badge self-heals (see stamp_enabled_mods_as_current).

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
        if hasattr(self, 'reshade_page') and self.reshade_page:
            self.reshade_page.set_managers(db=self._db, game_dir=self._game_dir)

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
        from PySide6.QtCore import QProcess, QTimer
        import json as _json
        import time as _time
        from cdumm.gui.apply_watchdog import (
            APPLY_STALL_THRESHOLD_S, is_apply_stalled, build_stall_message,
        )

        self._active_progress = tip
        proc = QProcess(self)
        _quiet_qprocess(proc)
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

        # A1 watchdog state — reset on every progress message; checked
        # by a QTimer every 10s. If the gap exceeds the threshold,
        # kill the QProcess and surface a clear error.
        _wd = {
            "last_progress_ts": _time.monotonic(),
            "last_progress_msg": None,
            "killed_by_watchdog": False,
        }

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
                    _wd["last_progress_ts"] = _time.monotonic()
                    _wd["last_progress_msg"] = msg.get("msg", "")
                    try:
                        tip.setContent(f"{msg.get('msg', '')} ({msg.get('pct', 0)}%)")
                    except RuntimeError:
                        pass
                if on_msg:
                    on_msg(msg)

        watchdog = QTimer(self)
        watchdog.setInterval(10_000)  # check every 10s

        def _on_watchdog_tick():
            if proc.state() != QProcess.Running:
                watchdog.stop()
                return
            if is_apply_stalled(
                    now=_time.monotonic(),
                    last_progress_ts=_wd["last_progress_ts"],
                    threshold_s=APPLY_STALL_THRESHOLD_S):
                _wd["killed_by_watchdog"] = True
                watchdog.stop()
                logger.error(
                    "Watchdog: no progress for %ss, killing %s PID %s",
                    APPLY_STALL_THRESHOLD_S, worker_args[0],
                    proc.processId())
                proc.kill()
                stall_msg = build_stall_message(
                    phase=worker_args[0],
                    last_progress_msg=_wd["last_progress_msg"],
                    threshold_s=APPLY_STALL_THRESHOLD_S)
                try:
                    tip.setContent(tr("progress.failed") if False else
                                   f"{worker_args[0]} stopped (stalled)")
                    tip.setState(True)
                except RuntimeError:
                    pass
                InfoBar.error(
                    title=f"{worker_args[0].capitalize()} stopped",
                    content=stall_msg, duration=-1,
                    position=InfoBarPosition.TOP, parent=self)

        watchdog.timeout.connect(_on_watchdog_tick)

        def _on_finished(exit_code, exit_status):
            watchdog.stop()
            if _wd["killed_by_watchdog"]:
                # Error was already shown by the watchdog. Still
                # clean up the process + callback state.
                proc.deleteLater()
                self._active_worker = None
                self._active_progress = None
                self._resume_timers()
                # Feed a synthetic error message so on_done branches
                # that expect an error list see it.
                _msgs.append({
                    "type": "error",
                    "msg": build_stall_message(
                        phase=worker_args[0],
                        last_progress_msg=_wd["last_progress_msg"],
                        threshold_s=APPLY_STALL_THRESHOLD_S)})
                on_done(_msgs)
                return
            # Detect native worker crash (segfault / unhandled C
            # exception). When the worker dies without emitting any
            # `done`/`error` JSON, _msgs may be empty and downstream
            # handlers would silently report success. Synthesize an
            # error entry so on_done sees the failure. Round 4
            # GUI/worker audit catch.
            from PySide6.QtCore import QProcess as _QProcess
            if exit_status == _QProcess.CrashExit:
                _msgs.append({
                    "type": "error",
                    "msg": (
                        f"{worker_args[0].capitalize()} worker crashed "
                        f"(exit code {exit_code}). Likely a native "
                        f"extension hit a segfault. Try again; if it "
                        f"persists, attach the bug report and "
                        f"cdumm_worker.log to a new GitHub issue."
                    )
                })
            # Guard against the case where the user closed the main
            # window while apply was still running. By the time this
            # finished-handler fires, Qt may have already deleted the
            # tooltip's underlying C++ object — calling setContent
            # then crashes with "Internal C++ object already deleted."
            try:
                tip.setContent(tr("progress.completed"))
                tip.setState(True)
            except RuntimeError:
                pass
            try:
                proc.deleteLater()
            except RuntimeError:
                pass
            self._active_worker = None
            self._active_progress = None
            self._resume_timers()
            try:
                on_done(_msgs)
            except RuntimeError:
                pass

        def _on_stderr():
            data = proc.readAllStandardError().data().decode("utf-8", errors="replace")
            if data.strip():
                logger.debug("Worker stderr: %s", data.strip()[:500])

        proc.readyReadStandardOutput.connect(_on_stdout)
        proc.readyReadStandardError.connect(_on_stderr)
        proc.finished.connect(_on_finished)
        proc.start(exe, args)
        watchdog.start()
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

    def _check_duplicate_mods(self) -> None:
        """Surface a one-click cleanup InfoBar when the mods table holds
        multiple rows for the same name.

        The pre-v3.2 batch-import path skipped the dedup gate, so users
        who dragged a folder of all their mods back into CDUMM ended up
        with two rows for every re-imported mod — old enabled+applied
        rows still active in the engine, plus new disabled rows the
        user thought were the only ones. This method runs at startup,
        finds the dupes, and pops a banner with a "Clean up" button.

        Idempotent: silent when there are no duplicates.
        """
        if not self._mod_manager or not self._db:
            return
        try:
            from cdumm.engine.mod_dedup import find_duplicate_groups
            groups = find_duplicate_groups(self._db.connection)
        except Exception as e:
            logger.debug("duplicate-mod check failed: %s", e)
            return
        if not groups:
            return
        stale_count = sum(len(rows) - 1 for rows in groups.values())
        names = list(groups.keys())
        head = ", ".join(names[:3])
        tail = (f" +{len(names) - 3}"
                if len(names) > 3 else "")
        bar = InfoBar.warning(
            title=f"{stale_count} duplicate mod row(s) detected",
            content=(
                f"Click Clean up to merge and remove them. ({head}{tail})"),
            duration=-1,
            position=InfoBarPosition.TOP,
            parent=self,
        )
        from qfluentwidgets import PushButton
        btn = PushButton("Clean up")
        btn.setMinimumWidth(120)
        btn.clicked.connect(self._on_cleanup_duplicates_clicked)
        bar.addWidget(btn)
        self._dup_cleanup_bar = bar

    def _on_cleanup_duplicates_clicked(self) -> None:
        """Invoke the dedup cleanup and dismiss the banner."""
        if not self._mod_manager:
            return
        try:
            from cdumm.engine.mod_dedup import apply_cleanup
            results = apply_cleanup(self._mod_manager)
        except Exception as e:
            logger.warning("dedup cleanup failed: %s", e)
            InfoBar.error(
                title="Cleanup failed",
                content=str(e),
                duration=8000, position=InfoBarPosition.TOP, parent=self)
            return
        removed = sum(len(d) for _, d in results)
        bar = getattr(self, "_dup_cleanup_bar", None)
        if bar is not None:
            try:
                bar.close()
            except Exception:
                pass
            self._dup_cleanup_bar = None
        InfoBar.success(
            title="Duplicates cleaned",
            content=f"Removed {removed} stale mod row(s).",
            duration=5000, position=InfoBarPosition.TOP, parent=self)
        self._sync_db()
        self._refresh_all()

    def _check_game_updated(self) -> bool:
        """Check if the game was updated and offer the Recovery Flow.

        v3.2: replaces the plain "Rescan now?" MessageBox with a
        sticky Recovery InfoBar. Button fires RecoveryFlow which runs
        the full Fix Everything → rescan → reimport → apply chain.
        The old "Apply locked" banner from v3.1.7 stays as a second
        InfoBar so users who dismiss Recovery still see why Apply is
        disabled.

        Two trigger paths feed this same banner:

        1. ``startup_context["game_updated"]`` — set in main.py when
           Steam buildid + exe-hash fingerprint changed since last
           launch. Catches Steam patches.
        2. Snapshot drift — `detect_snapshot_drift` reads the
           snapshots table and compares live PAZ file sizes against
           what CDUMM expects. Catches manual file edits, antivirus
           rewrites, and partial Steam Verify runs that didn't bump
           the buildid.
        """
        triggered = False
        body = ""
        if self._startup_context.get("game_updated"):
            triggered = True
            body = ("Click Start Recovery to verify, rescan, and "
                    "reapply your mods.")
        elif self._db and self._game_dir:
            try:
                from cdumm.engine.snapshot_manager import detect_snapshot_drift
                drift, mismatches = detect_snapshot_drift(
                    self._db, self._game_dir)
                if drift:
                    triggered = True
                    sample = ", ".join(mismatches[:3])
                    extra = (f" (+{len(mismatches) - 3} more)"
                             if len(mismatches) > 3 else "")
                    body = (
                        f"{len(mismatches)} game file(s) drifted from "
                        f"the snapshot ({sample}{extra}). Click Start "
                        "Recovery to re-sync.")
                    logger.info(
                        "Snapshot drift detected — %d file(s): %s",
                        len(mismatches),
                        ", ".join(mismatches[:10]))
            except Exception as e:
                logger.debug("snapshot drift check failed: %s", e)

        if not triggered:
            return False

        self._offer_recovery_flow(
            title="Game files don't match the snapshot",
            body=body)
        # Also show the D1 "Apply locked" banner so the reason is
        # visible even if the user dismisses the Recovery InfoBar.
        InfoBar.error(
            title="Apply locked",
            content=(
                "Game files no longer match CDUMM's snapshot. Apply "
                "stays locked until a fresh rescan completes."),
            duration=-1, position=InfoBarPosition.TOP, parent=self)
        return True

    def _offer_recovery_flow(self, title: str, body: str) -> None:
        """Show the Recovery InfoBar with a Start Recovery button.

        Called from both :meth:`_check_game_updated` (startup
        ``game_updated`` flag) and from the deferred-startup
        fingerprint-mismatch branch. Single UX for both paths
        (Codex review finding 10 — unified recovery trigger).
        """
        # Guard against duplicate InfoBars when called twice.
        prior = getattr(self, "_recovery_infobar", None)
        if prior is not None:
            try:
                prior.close()
            except Exception:
                pass

        bar = InfoBar.warning(
            title=title,
            content=body,
            duration=-1, position=InfoBarPosition.TOP, parent=self)

        # Add a Start Recovery button.
        from qfluentwidgets import PrimaryPushButton
        btn = PrimaryPushButton("Start Recovery")
        btn.setMinimumWidth(160)

        def _on_start_recovery() -> None:
            # Close the banner so we don't stack another one while
            # the flow runs.
            try:
                bar.close()
            except Exception:
                pass
            self._recovery_infobar = None
            try:
                from cdumm.gui.recovery_flow import RecoveryFlow
                flow = RecoveryFlow(self)
                self._recovery_flow = flow  # keep ref alive
                flow.start()
            except Exception as e:
                logger.exception("RecoveryFlow failed to start: %s", e)
                InfoBar.error(
                    title="Recovery failed to start",
                    content=str(e),
                    duration=-1, position=InfoBarPosition.TOP,
                    parent=self)

        btn.clicked.connect(_on_start_recovery)
        bar.addWidget(btn)
        self._recovery_infobar = bar

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
        """Warn if game is installed under Program Files (admin restrictions).

        Two-tier UX:
        * First-ever launch with the game here → one-time modal with
          the full explanation of what's wrong and how to fix it.
        * Every launch after that → sticky InfoBar.warning at the top
          of the window (dismissable per session) so the user keeps
          seeing it until they move the library. Quiet one-time
          modals were getting forgotten by the time stuck-apply
          reports came in (issue #30, kai481).
        """
        try:
            if not self._game_dir:
                return
            from cdumm.gui.apply_watchdog import is_game_in_program_files
            if not is_game_in_program_files(self._game_dir):
                return

            from cdumm.storage.config import Config
            config = Config(self._db)

            # Sticky per-session banner — fires EVERY launch while
            # the game is still in Program Files.
            InfoBar.warning(
                title="Game location warning",
                content=(
                    "Crimson Desert is installed under Program Files. "
                    "Windows restricts writes here, so mods can "
                    "silently fail or get stuck during apply. Move "
                    "your Steam library to a different drive (e.g. "
                    "D:\\SteamLibrary) to fix this for good."),
                duration=-1, position=InfoBarPosition.TOP, parent=self)

            # One-time modal with the full explanation (only first
            # time we ever detect this).
            if config.get("program_files_warned"):
                return
            MessageBox(
                "Game Location Warning",
                "Your game is installed under Program Files, which has\n"
                "restricted write permissions on Windows.\n\n"
                "This can cause issues with mod backups and configuration.\n"
                "If you experience problems, move your Steam library to\n"
                "a different drive (e.g. D:\\SteamLibrary). Steam only\n"
                "allows one library per drive, so a second folder on C:\n"
                "will be rejected with \"This drive already has a library\n"
                "folder.\" You need a separate drive.\n\n"
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
        """Show patch notes dialog when CDUMM was upgraded since the
        last run. Stamps the new version even when the dialog isn't
        shown so a fresh install doesn't fire on every subsequent
        launch.

        Skipped on a true fresh install (``last_seen_version`` is
        unset) — a brand-new user has no reason to see "what's new in
        v3.2" for a version they just installed for the first time.
        """
        from cdumm import __version__
        from cdumm.storage.config import Config

        config = Config(self._db)
        last_ver = config.get("last_seen_version")
        if last_ver == __version__:
            return
        config.set("last_seen_version", __version__)
        if not last_ver:
            # Fresh install — stamp the version, don't pop the dialog.
            return
        try:
            from cdumm.gui.changelog import PatchNotesDialog
            PatchNotesDialog(self, latest_only=True).exec()
        except Exception as e:
            logger.debug("Patch notes dialog failed to show: %s", e)

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

        # I1: snapshot-contamination guard. Before rescan touches
        # anything, sample-check live game files against stored
        # vanilla backups. If live disk differs, rescanning would
        # bake modded bytes into the snapshot and every future
        # revert would restore the wrong state. Refuse, tell the
        # user exactly what to do next.
        if self._vanilla_dir and self._vanilla_dir.exists():
            try:
                from cdumm.engine.snapshot_manager import (
                    verify_live_disk_matches_backups,
                )
                is_clean, problem_files = verify_live_disk_matches_backups(
                    self._game_dir, self._vanilla_dir)
            except Exception as e:
                logger.warning(
                    "Rescan guard check failed (%s) — proceeding", e)
                is_clean, problem_files = True, []
            if not is_clean:
                shown = problem_files[:5]
                more = len(problem_files) - len(shown)
                block = "\n".join(f"  - {p}" for p in shown)
                if more > 0:
                    block += f"\n  - ...and {more} more"
                MessageBox(
                    "Rescan Blocked — Disk Looks Modded",
                    "CDUMM detected live game files that don't match "
                    "the vanilla backups it has on disk. Rescanning "
                    "now would capture modded bytes as vanilla and "
                    "break every future Revert.\n\n"
                    f"Files that differ from their backups:\n\n"
                    f"{block}\n\n"
                    "Fix before rescan:\n"
                    "  1. Revert to Vanilla (if your backups are "
                    "valid), OR\n"
                    "  2. Run Steam's Verify Integrity, then click "
                    "Fix Everything with the Steam-verified option.",
                    self,
                ).exec()
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

        # D1: rescan succeeded — clear the stale-snapshot flag so
        # Apply unlocks without requiring a restart.
        if self._startup_context.get("game_updated"):
            self._startup_context["game_updated"] = False
            logger.info(
                "Cleared game_updated flag after rescan; Apply unlocked")

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
        # Kill any active worker QProcess so it doesn't outlive the GUI
        # and continue writing to the DB. Earlier closeEvent only
        # stopped QThreads, leaving QProcess children running until
        # they finished naturally — orphan processes could collide
        # with the next instance's worker. Round 4 GUI/worker audit.
        active_worker = getattr(self, "_active_worker", None)
        if active_worker is not None:
            try:
                from PySide6.QtCore import QProcess as _QProcess
                if isinstance(active_worker, _QProcess):
                    if active_worker.state() != _QProcess.NotRunning:
                        active_worker.kill()
                        active_worker.waitForFinished(3000)
            except (RuntimeError, Exception) as _e:
                logger.debug("Active worker kill failed: %s", _e)
        # Stop the SystemThemeListener FIRST — it's a background thread
        # that can hold references during Qt teardown if left running.
        listener = getattr(self, "_theme_listener", None)
        if listener is not None:
            try:
                listener.terminate()
                listener.deleteLater()
            except (RuntimeError, Exception) as _e:
                logger.debug("Theme listener teardown failed: %s", _e)
        # Stop timers (guard against already-deleted C++ objects — Qt may
        # have reaped children by the time closeEvent fires)
        for timer_name in (
            "_update_timer",
            "_db_poll_timer",
            "_db_change_timer",
            "_nxm_poll_timer",
            "_nexus_update_timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is None:
                continue
            try:
                timer.stop()
            except RuntimeError:
                pass
        # Stop worker threads. Wrap every access in try/except because
        # shiboken raises RuntimeError('Internal C++ object already
        # deleted') if Qt's normal parent-teardown has already reaped
        # the QThread before our closeEvent runs. Seen in the debug
        # build's log at line 4192 after a clean app exit.
        for thread_name in ("_worker_thread", "_update_thread"):
            thread = getattr(self, thread_name, None)
            if thread is None:
                continue
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(2000)
            except RuntimeError:
                pass
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
        # Mark clean shutdown so the atexit hook is allowed to remove
        # the running lock. Without this flag, a crash path would drop
        # through to atexit with clean_shutdown=False and the lock
        # stays, which is exactly what we want for crash detection.
        if hasattr(self, "_lock_state"):
            from cdumm.gui.running_lock import mark_clean_shutdown
            mark_clean_shutdown(self._lock_state)
        # Remove lock file now (atexit would also do this post-flag).
        if hasattr(self, "_lock_file") and self._lock_file.exists():
            try:
                self._lock_file.unlink()
            except Exception:
                pass
        super().closeEvent(event)
