import atexit
import faulthandler
import os
import sys
import logging
import threading
from pathlib import Path
from logging.handlers import RotatingFileHandler

APP_DATA_DIR = Path.home() / "AppData" / "Local" / "cdumm"

# Enable faulthandler to dump C-level stack trace on segfault.
# Defensive: if AppData is read-only / permissions fail (domolinixd1000
# report: CDUMM.exe closes in 2-3s, can't even produce a bug report),
# fall back to stderr and keep going. Previously an `open()` failure
# here would raise at module import time — before `sys.excepthook` is
# wired — and the user saw a silent exit with no log to inspect.
_fault_log = None
try:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _fault_log = open(APP_DATA_DIR / "crash_trace.txt", "w")
    faulthandler.enable(file=_fault_log)
except Exception:
    # Cascading fallbacks so the process keeps booting even if
    # AppData is unwritable, locked, or on a non-ASCII path that
    # breaks Python's stdlib open on some Windows configs.
    try:
        import tempfile as _tempfile
        _fault_log = open(
            Path(_tempfile.gettempdir()) / "cdumm_crash_trace.txt", "w")
        faulthandler.enable(file=_fault_log)
    except Exception:
        try:
            faulthandler.enable(file=sys.stderr)
        except Exception:
            pass  # give up on faulthandler; process still boots


def _emergency_crash_dump(exc: BaseException) -> None:
    """Last-resort crash writer for failures BEFORE logging is wired.

    Domolinixd1000 reports CDUMM.exe closes within 2-3 seconds with no
    bug report possible. This path fires when main() raises before the
    excepthook is installed, writing a plain-text traceback to
    %LOCALAPPDATA%\\cdumm\\crash-pre-qt.log (or %TEMP% as fallback) so
    the user has something to paste.
    """
    import traceback
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    payload = "".join(tb)
    for target in (
            APP_DATA_DIR / "crash-pre-qt.log",
            Path(os.environ.get("TEMP", ".")) / "cdumm-crash-pre-qt.log"):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
            return
        except Exception:
            continue

_lock_fh = None


def setup_logging(app_data: Path) -> None:
    app_data.mkdir(parents=True, exist_ok=True)
    log_file = app_data / "cdumm.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=1,
            encoding="utf-8", delay=True,
        )
        # Override rotation to handle locked files on Windows
        _orig_rotate = file_handler.doRollover
        def _safe_rollover():
            try:
                _orig_rotate()
            except OSError:
                pass  # file locked by another process — skip rotation
        file_handler.doRollover = _safe_rollover
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root_logger.addHandler(file_handler)
    except OSError:
        pass  # log file locked by another CDUMM instance — skip file logging

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)
    root_logger.addHandler(console_handler)


def _flush_logs():
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _global_exception_handler(exc_type, exc_value, exc_tb):
    logger = logging.getLogger("CRASH")
    logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
    _flush_logs()
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _thread_exception_handler(args):
    logger = logging.getLogger("CRASH")
    logger.critical(
        "Unhandled exception in thread %s",
        args.thread.name if args.thread else "unknown",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    _flush_logs()


def main() -> int:
    setup_logging(APP_DATA_DIR)
    sys.excepthook = _global_exception_handler
    threading.excepthook = _thread_exception_handler

    logger = logging.getLogger(__name__)
    logger.info("Starting Crimson Desert Ultimate Mods Manager")

    # Sweep stale extraction workspaces left over from prior runs that
    # crashed or were force-killed before atexit fired. Scoped strictly
    # to cdumm_* prefixes — never touches other apps' temp dirs. On
    # HDD-backed machines with a large %TEMP%, the sweep can take
    # several seconds of dir-stating — run on a background thread so
    # splash paints immediately. B3.
    try:
        from cdumm.engine.temp_workspace import sweep_stale
        import threading as _threading

        def _bg_sweep() -> None:
            try:
                sweep_stale(max_age_hours=48)
            except Exception as _e:
                logger.debug("temp_workspace bg sweep error: %s", _e)

        _sweep_thread = _threading.Thread(
            target=_bg_sweep, name="cdumm-temp-sweep", daemon=True)
        _sweep_thread.start()
    except Exception as e:
        logger.debug("temp_workspace startup sweep skipped: %s", e)

    # Single instance check — prevent two GUI windows
    global _lock_fh
    _lock_file = APP_DATA_DIR / ".gui_lock"
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # Try to acquire exclusive lock on the file (Windows only)
        if sys.platform == "win32":
            import msvcrt
            _lock_fh = open(_lock_file, "w")
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
            _lock_fh.write(str(os.getpid()))
            _lock_fh.flush()
            atexit.register(lambda: _lock_fh.close() if _lock_fh else None)
        else:
            # Unix: use fcntl file locking
            import fcntl
            _lock_fh = open(_lock_file, "w")
            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lock_fh.write(str(os.getpid()))
            _lock_fh.flush()
            atexit.register(lambda: _lock_fh.close() if _lock_fh else None)
    except (OSError, IOError, ImportError):
        # Another GUI instance holds the lock — bring it to front and exit
        logger.info("Another CDUMM instance is already running, exiting")
        if sys.platform == "win32":
            import ctypes
            from cdumm import __version__
            hwnd = ctypes.windll.user32.FindWindowW(None, f"CDUMM v{__version__}")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        return 0

    # Initialize i18n (English default, reloads with user preference after DB is ready)
    from cdumm.i18n import load as load_i18n
    load_i18n("en")

    # Set AppUserModelID so Windows taskbar shows our icon, not Python's
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("kindiboy.cdumm.modmanager.3")
    except Exception:
        pass

    # Reduce GIL switch interval from 5ms to 0.5ms so the GUI thread
    # gets more frequent time slices during worker Python execution.
    sys.setswitchinterval(0.0005)

    # Minimal import for QApplication — everything else is lazy
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    # Fix PySide6 6.7+ Win11 style causing double borders on menus/shadows
    app.setStyle("fusion")
    app.setApplicationName("Crimson Desert Ultimate Mods Manager")

    # Set application-level icon (shows in taskbar)
    from PySide6.QtGui import QIcon
    if getattr(sys, 'frozen', False):
        _app_ico = Path(sys._MEIPASS) / "cdumm.ico"
    else:
        _app_ico = Path(__file__).resolve().parents[2] / "cdumm.ico"
    if _app_ico.exists():
        app.setWindowIcon(QIcon(str(_app_ico)))

    # Load Oxanium font
    from PySide6.QtGui import QFontDatabase
    font_path = None
    if getattr(sys, 'frozen', False):
        font_path = Path(sys._MEIPASS) / "assets" / "fonts" / "Oxanium-VariableFont_wght.ttf"
    else:
        font_path = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "Oxanium-VariableFont_wght.ttf"
    if font_path and font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                from qfluentwidgets import setFontFamilies
                setFontFamilies([families[0], "Segoe UI"])

    # Set Fluent theme (default light, may be overridden by welcome wizard or saved pref)
    from qfluentwidgets import setTheme, Theme, setThemeColor
    setTheme(Theme.LIGHT)
    setThemeColor("#2878D0")

    # First-time welcome wizard: language, theme, and game folder
    # Uses its own marker file — independent of game_dir.txt which auto-detect writes
    _wizard_done_file = APP_DATA_DIR / ".wizard_done"
    _first_launch = not _wizard_done_file.exists()
    _wizard_lang = "en"
    _wizard_theme = "light"
    _wizard_game_dir = None
    if _first_launch:
        from cdumm.gui.welcome_wizard import WelcomeWizard
        wizard = WelcomeWizard()
        wizard.exec()  # Cannot be closed without completing (ALT+F4 blocked)
        _wizard_lang = wizard.chosen_language
        _wizard_theme = wizard.chosen_theme
        _wizard_game_dir = wizard.game_directory
        if _wizard_theme == "dark":
            setTheme(Theme.DARK)
        load_i18n(_wizard_lang)
        # Mark wizard as completed so it doesn't show again
        _wizard_done_file.write_text("done", encoding="utf-8")
        logger.info("Welcome wizard: lang=%s, theme=%s, game=%s",
                     _wizard_lang, _wizard_theme, _wizard_game_dir)

    # Show splash immediately before heavy imports
    from cdumm.gui.splash import show_splash
    splash = show_splash()
    app.processEvents()

    # Now do heavy imports
    splash.showMessage("  Loading database...", 0x0081)  # AlignLeft | AlignBottom
    app.processEvents()

    from cdumm.storage.database import Database
    from cdumm.storage.config import Config

    # Find game directory first — DB lives in CDMods/ inside game dir
    from cdumm.storage.config import Config as _TmpConfig

    # Persistent game_dir pointer in AppData (survives CDMods deletion)
    _game_dir_file = APP_DATA_DIR / "game_dir.txt"

    # Check for existing DB in AppData (pre-v1.7 installs)
    old_appdata_db = APP_DATA_DIR / "cdumm.db"
    old_cdmm_db = Path.home() / "AppData" / "Local" / "cdmm" / "cdumm.db"

    # Try to find game_dir: wizard result first, then pointer file, then old DBs, then auto-detect
    from cdumm.storage.game_finder import find_game_directories, validate_game_directory
    game_dir = None

    # Method 0: Use wizard result (first launch)
    if _wizard_game_dir and validate_game_directory(Path(_wizard_game_dir)):
        game_dir = _wizard_game_dir
        logger.info("Game directory from wizard: %s", game_dir)

    # Method 1: Read from persistent pointer file
    if _game_dir_file.exists():
        try:
            saved = _game_dir_file.read_text(encoding="utf-8").strip()
            if saved and validate_game_directory(Path(saved)):
                game_dir = saved
                logger.info("Game directory from pointer: %s", game_dir)
            elif saved:
                logger.info("Pointer path no longer valid: %s", saved)
        except Exception:
            pass

    # Method 2: Check old AppData DBs (pre-v1.7 migration)
    if game_dir is None:
        for old_db in [old_appdata_db, old_cdmm_db]:
            if old_db.exists():
                try:
                    tmp_db = Database(old_db)
                    tmp_db.initialize()
                    candidate = _TmpConfig(tmp_db).get("game_directory")
                    tmp_db.close()
                    if candidate and validate_game_directory(Path(candidate)):
                        game_dir = candidate
                except Exception:
                    pass
                if game_dir:
                    break

    # Method 3: Auto-detect if saved path is invalid (game was moved)
    if game_dir is None:
        detected = find_game_directories()
        if len(detected) == 1:
            game_dir = str(detected[0])
            logger.info("Auto-detected moved game: %s", game_dir)

    if game_dir is None:
        # First-run: game directory setup
        splash.close()
        from cdumm.gui.setup_dialog import SetupDialog
        dialog = SetupDialog()
        if dialog.exec() and dialog.game_directory:
            game_dir = str(dialog.game_directory)
            logger.info("Game directory configured: %s", game_dir)
        else:
            logger.warning("No game directory selected, exiting")
            return 1
        splash = show_splash()
        app.processEvents()

    game_path = Path(game_dir)
    cdmods_dir = game_path / "CDMods"
    cdmods_dir.mkdir(parents=True, exist_ok=True)
    new_db = cdmods_dir / "cdumm.db"

    # Migrate from old AppData location if needed.
    # Check if new DB is empty/fresh (small) vs already populated.
    import shutil
    new_db_is_fresh = not new_db.exists() or new_db.stat().st_size < 200_000
    if new_db_is_fresh:
        for old_db in [old_appdata_db, old_cdmm_db]:
            if old_db.exists() and old_db.stat().st_size > 200_000:
                if new_db.exists():
                    new_db.unlink()
                shutil.copy2(old_db, new_db)
                logger.info("Migrated database from %s to %s", old_db, new_db)
                break

    db = Database(new_db)
    db.initialize()
    logger.info("Database initialized at %s", db.db_path)

    config = Config(db)

    # Save welcome wizard choices (first launch only)
    if _first_launch and _wizard_lang != "en":
        config.set("language", _wizard_lang)
    if _first_launch and _wizard_theme != "light":
        config.set("theme", _wizard_theme)

    # Reload i18n with user's language preference (skip if wizard already set it)
    user_lang = config.get("language") or "en"
    if user_lang != "en" and not _first_launch:
        load_i18n(user_lang)

    # Apply saved theme preference (skip if wizard already set it)
    if not _first_launch:
        saved_theme = config.get("theme") or "light"
        if saved_theme == "auto":
            from qfluentwidgets import setTheme, Theme
            setTheme(Theme.AUTO)
        elif saved_theme == "dark":
            from qfluentwidgets import setTheme, Theme
            setTheme(Theme.DARK)

    # Set RTL layout direction for Arabic/Hebrew/etc.
    from cdumm.i18n import is_rtl
    if is_rtl():
        from PySide6.QtCore import Qt
        app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

    # Ensure game_dir is saved in the new DB and pointer file
    if config.get("game_directory") != game_dir:
        config.set("game_directory", game_dir)
    try:
        _game_dir_file.parent.mkdir(parents=True, exist_ok=True)
        _game_dir_file.write_text(game_dir, encoding="utf-8")
    except Exception:
        pass

    splash.showMessage("  Loading game schemas...", 0x0081)
    app.processEvents()

    # Load semantic schemas eagerly so they're available for all operations
    try:
        from cdumm.semantic.parser import init_schemas
        schema_count = init_schemas()
        logger.info("Semantic schemas: %d tables loaded", schema_count)
    except Exception as e:
        logger.debug("Semantic schemas unavailable: %s", e)

    splash.showMessage("  Checking game state...", 0x0081)
    app.processEvents()

    # Run heavy startup checks DURING splash (before UI shows)
    # so the window is responsive immediately when it appears.
    from cdumm.engine.snapshot_manager import SnapshotManager
    snapshot = SnapshotManager(db)

    startup_context = {"stale": False, "has_snapshot": snapshot.has_snapshot()}

    if startup_context["has_snapshot"]:
        splash.showMessage("  Verifying game files...", 0x0081)
        app.processEvents()

        # F2: one-time fingerprint backfill for installs that
        # predate the stable-fingerprint fix. No-ops after first
        # successful run. Must run BEFORE the stale-check below so
        # the comparison uses the new-algorithm values on both sides.
        from cdumm.engine.version_detector import (
            backfill_stored_fingerprints, detect_game_version,
        )
        backfill_stored_fingerprints(db, game_path)

        # Check game version fingerprint (fast — just reads a config value)
        current_fp = detect_game_version(game_path)
        stored_fp = config.get("game_version_fingerprint")
        if stored_fp and current_fp and stored_fp != current_fp:
            startup_context["game_updated"] = True

    splash.showMessage("  Building UI...", 0x0081)
    app.processEvents()

    from cdumm.gui.fluent_window import CdummWindow
    window = CdummWindow(db=db, game_dir=game_path, app_data_dir=APP_DATA_DIR,
                         startup_context=startup_context)
    window.show()
    splash.finish(window)

    # ── Frame stall profiler ─────────────────────────────────────────
    # Fires a 16ms timer and logs whenever the main thread stalls > 50ms.
    # Writes to frame_stalls.log so we can see EXACTLY what blocks the UI.
    import time as _time
    from PySide6.QtCore import QTimer as _QT

    _stall_log_path = APP_DATA_DIR / "frame_stalls.log"
    _stall_fh = open(_stall_log_path, "w")
    _stall_fh.write("Frame stall profiler started\n")
    _last_tick = [_time.perf_counter()]
    _stall_count = [0]

    def _frame_tick():
        now = _time.perf_counter()
        dt_ms = (now - _last_tick[0]) * 1000
        _last_tick[0] = now
        if dt_ms > 50:  # >50ms = dropped frames
            _stall_count[0] += 1
            _stall_fh.write(f"STALL {_stall_count[0]}: {dt_ms:.0f}ms at {_time.strftime('%H:%M:%S')}\n")
            _stall_fh.flush()

    _frame_timer = _QT()
    _frame_timer.setInterval(16)
    _frame_timer.timeout.connect(_frame_tick)
    _frame_timer.start()

    return app.exec()


if __name__ == "__main__":
    try:
        # Worker subprocess mode: headless, no GUI, JSON output on stdout
        if len(sys.argv) > 1 and sys.argv[1] == "--worker":
            from cdumm.worker_process import worker_main
            worker_main(sys.argv[2:])
        # CLI mode: if first arg is a known subcommand, skip GUI entirely
        elif len(sys.argv) > 1 and sys.argv[1] in {"list-mods", "set-enabled", "apply", "bisect"}:
            from cdumm.cli import main as cli_main
            cli_main()
        else:
            sys.exit(main())
    except BaseException as _bootstrap_exc:
        # Everything above went through setup_logging / excepthook; this
        # catches the nasty pre-logging failures (bad DLL load, missing
        # Qt plugin, OS-level I/O) that previously vanished silently.
        _emergency_crash_dump(_bootstrap_exc)
        raise
