import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(file_path)
);

CREATE TABLE IF NOT EXISTS mods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    mod_type TEXT NOT NULL CHECK(mod_type IN ('paz', 'asi')),
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    import_date TEXT NOT NULL DEFAULT (datetime('now')),
    game_version_hash TEXT,
    source_path TEXT
);

CREATE TABLE IF NOT EXISTS mod_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    delta_path TEXT NOT NULL,
    byte_start INTEGER,
    byte_end INTEGER
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_a_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
    mod_b_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('papgt', 'paz', 'byte_range')),
    byte_start INTEGER,
    byte_end INTEGER,
    explanation TEXT,
    winner_id INTEGER REFERENCES mods(id) ON DELETE SET NULL
);
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.db_path))
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(SCHEMA)
        self._migrate()
        self._connection.commit()
        logger.info("Database schema initialized")

    def _migrate(self) -> None:
        """Run schema migrations for existing databases."""
        # Add priority column if missing (v0 → v1)
        cursor = self._connection.execute("PRAGMA table_info(mods)")
        columns = {row[1] for row in cursor.fetchall()}
        if "priority" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
            )
            # Set priority based on existing id order
            self._connection.execute(
                "UPDATE mods SET priority = id WHERE priority = 0"
            )
            logger.info("Migrated: added priority column to mods")

        # Add winner_id column to conflicts if missing
        cursor = self._connection.execute("PRAGMA table_info(conflicts)")
        conflict_cols = {row[1] for row in cursor.fetchall()}
        if "winner_id" not in conflict_cols:
            self._connection.execute(
                "ALTER TABLE conflicts ADD COLUMN winner_id INTEGER REFERENCES mods(id) ON DELETE SET NULL"
            )
            logger.info("Migrated: added winner_id column to conflicts")

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def table_exists(self, table_name: str) -> bool:
        cursor = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cursor.fetchone() is not None
