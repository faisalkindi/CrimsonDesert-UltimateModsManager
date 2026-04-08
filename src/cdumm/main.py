import sys
import logging
import threading
from pathlib import Path
from logging.handlers import RotatingFileHandler

APP_DATA_DIR = Path.home() / "AppData" / "Local" / "cdumm"


def setup_logging(app_data: Path) -> None:
    app_data.mkdir(parents=True, exist_ok=True)
    log_file = app_data / "cdumm.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=1, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)

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

    # Initialize i18n (English default, reloads with user preference after DB is ready)
    from cdumm.i18n import load as load_i18n
    load_i18n("en")

    # Minimal import for QApplication — everything else is lazy
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName("Crimson Desert Ultimate Mods Manager")

    # Show splash immediately before heavy imports
    from cdumm.gui.splash import show_splash
    splash = show_splash()
    app.processEvents()

    # Apply theme
    from cdumm.gui.theme import STYLESHEET
    app.setStyleSheet(STYLESHEET)

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

    # Try to find game_dir: pointer file first, then old DBs, then auto-detect
    from cdumm.storage.game_finder import find_game_directories, validate_game_directory
    game_dir = None

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
        from PySide6.QtWidgets import QDialog
        from cdumm.gui.setup_dialog import SetupDialog
        dialog = SetupDialog()
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.game_directory:
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

    # Reload i18n with user's language preference
    user_lang = config.get("language") or "en"
    if user_lang != "en":
        load_i18n(user_lang)

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

        # Check game version fingerprint (fast — just reads a config value)
        from cdumm.engine.version_detector import detect_game_version
        current_fp = detect_game_version(game_path)
        stored_fp = config.get("game_version_fingerprint")
        if stored_fp and current_fp and stored_fp != current_fp:
            startup_context["game_updated"] = True

    splash.showMessage("  Building UI...", 0x0081)
    app.processEvents()

    from cdumm.gui.main_window import MainWindow
    window = MainWindow(db=db, game_dir=game_path, app_data_dir=APP_DATA_DIR,
                        startup_context=startup_context)
    window.show()
    splash.finish(window)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
