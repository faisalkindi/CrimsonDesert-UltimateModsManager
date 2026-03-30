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

    # Migrate from old cdmm AppData if this is an upgrade
    old_app_data = Path.home() / "AppData" / "Local" / "cdmm"
    old_db = old_app_data / "cdmm.db"
    new_db = APP_DATA_DIR / "cdumm.db"
    if old_db.exists() and not new_db.exists():
        import shutil
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_db, new_db)
        logger.info("Migrated database from %s to %s", old_db, new_db)

    db = Database(new_db)
    db.initialize()
    logger.info("Database initialized at %s", db.db_path)

    config = Config(db)

    # First-run: game directory setup
    game_dir = config.get("game_directory")
    if game_dir is None:
        splash.close()
        from PySide6.QtWidgets import QDialog
        from cdumm.gui.setup_dialog import SetupDialog
        dialog = SetupDialog()
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.game_directory:
            config.set("game_directory", str(dialog.game_directory))
            game_dir = str(dialog.game_directory)
            logger.info("Game directory configured: %s", game_dir)
        else:
            logger.warning("No game directory selected, exiting")
            return 1
        splash = show_splash()
        app.processEvents()

    splash.showMessage("  Checking game state...", 0x0081)
    app.processEvents()

    # Run heavy startup checks DURING splash (before UI shows)
    # so the window is responsive immediately when it appears.
    game_path = Path(game_dir)
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
