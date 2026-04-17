import logging
from pathlib import Path
from typing import Optional

from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


class Config:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> Optional[str]:
        cursor = self._db.connection.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self._db.connection.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.connection.commit()
        logger.debug("Config set: %s = %s", key, value)


def default_export_dir(db: Optional[Database] = None) -> Path:
    """Return the directory CDUMM should default save dialogs to.

    Resolution order:

    1. User-configured ``export_dir`` in the ``config`` table (if a DB
       is provided and the value is set + exists).
    2. ``~/Documents/CDUMM/`` — created on first call.

    Never ``~/Downloads``. Users complained about bug reports and mod
    list exports landing in Downloads; Downloads is for downloaded
    files, not app-generated ones.
    """
    if db is not None:
        try:
            cfg_row = db.connection.execute(
                "SELECT value FROM config WHERE key = 'export_dir'"
            ).fetchone()
            if cfg_row and cfg_row[0]:
                configured = Path(cfg_row[0])
                if configured.is_dir():
                    return configured
        except Exception as e:
            logger.debug("default_export_dir: DB lookup failed: %s", e)
    fallback = Path.home() / "Documents" / "CDUMM"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.debug("default_export_dir: mkdir failed, using home (%s)", e)
        return Path.home()
    return fallback
