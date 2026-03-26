import logging
from typing import Optional

from cdmm.storage.database import Database

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
