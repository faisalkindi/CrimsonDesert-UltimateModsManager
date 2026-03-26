import sys
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import QApplication

from PySide6.QtWidgets import QDialog

from cdmm.gui.main_window import MainWindow
from cdmm.gui.setup_dialog import SetupDialog
from cdmm.storage.database import Database
from cdmm.storage.config import Config

APP_DATA_DIR = Path.home() / "AppData" / "Local" / "cdmm"


def setup_logging(app_data: Path) -> None:
    app_data.mkdir(parents=True, exist_ok=True)
    log_file = app_data / "cdmm.log"

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


def _global_exception_handler(exc_type, exc_value, exc_tb):
    """Catch unhandled exceptions and log them before the app dies."""
    logger = logging.getLogger("CRASH")
    logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main() -> int:
    setup_logging(APP_DATA_DIR)
    sys.excepthook = _global_exception_handler

    logger = logging.getLogger(__name__)
    logger.info("Starting Crimson Desert Mod Manager")

    app = QApplication(sys.argv)
    app.setApplicationName("Crimson Desert Ultimate Mods Manager (BETA)")

    db = Database(APP_DATA_DIR / "cdmm.db")
    db.initialize()
    logger.info("Database initialized at %s", db.db_path)

    config = Config(db)

    # First-run: game directory setup
    game_dir = config.get("game_directory")
    if game_dir is None:
        dialog = SetupDialog()
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.game_directory:
            config.set("game_directory", str(dialog.game_directory))
            game_dir = str(dialog.game_directory)
            logger.info("Game directory configured: %s", game_dir)
        else:
            logger.warning("No game directory selected, exiting")
            return 1

    window = MainWindow(db=db, game_dir=Path(game_dir), app_data_dir=APP_DATA_DIR)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
