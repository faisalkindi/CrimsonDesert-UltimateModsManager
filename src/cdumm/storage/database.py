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
    enabled INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    import_date TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    game_version_hash TEXT,
    source_path TEXT,
    author TEXT,
    version TEXT,
    description TEXT,
    configurable INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mod_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    delta_path TEXT NOT NULL,
    byte_start INTEGER,
    byte_end INTEGER,
    is_new INTEGER NOT NULL DEFAULT 0,
    vanilla_hash TEXT,
    entry_path TEXT,
    json_patches TEXT
);

CREATE TABLE IF NOT EXISTS mod_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
    selected_labels TEXT,
    UNIQUE(mod_id)
);

CREATE TABLE IF NOT EXISTS mod_vanilla_sizes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    vanilla_size INTEGER NOT NULL,
    UNIQUE(mod_id, file_path)
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

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile_mods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    UNIQUE(profile_id, mod_id)
);

CREATE INDEX IF NOT EXISTS idx_mod_deltas_mod_id ON mod_deltas(mod_id);
CREATE INDEX IF NOT EXISTS idx_mod_deltas_file_path ON mod_deltas(file_path);
CREATE INDEX IF NOT EXISTS idx_conflicts_mod_a_id ON conflicts(mod_a_id);
CREATE INDEX IF NOT EXISTS idx_conflicts_mod_b_id ON conflicts(mod_b_id);
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
        # Create indexes that depend on migrated columns (entry_path may not
        # exist in pre-migration databases, so this must run after _migrate)
        try:
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_mod_deltas_entry_path "
                "ON mod_deltas(entry_path)")
        except Exception:
            pass  # column doesn't exist yet on very old DBs
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

        # Add modinfo columns to mods if missing
        if "author" not in columns:
            self._connection.execute("ALTER TABLE mods ADD COLUMN author TEXT")
            self._connection.execute("ALTER TABLE mods ADD COLUMN version TEXT")
            self._connection.execute("ALTER TABLE mods ADD COLUMN description TEXT")
            logger.info("Migrated: added author/version/description columns to mods")

        # Add is_new column to mod_deltas if missing
        cursor = self._connection.execute("PRAGMA table_info(mod_deltas)")
        delta_cols = {row[1] for row in cursor.fetchall()}
        if "is_new" not in delta_cols:
            self._connection.execute(
                "ALTER TABLE mod_deltas ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("Migrated: added is_new column to mod_deltas")

        if "vanilla_hash" not in delta_cols:
            self._connection.execute(
                "ALTER TABLE mod_deltas ADD COLUMN vanilla_hash TEXT"
            )
            logger.info("Migrated: added vanilla_hash column to mod_deltas")

        if "configurable" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN configurable INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("Migrated: added configurable column to mods")

        # Add entry_path column to mod_deltas for entry-level deltas
        if "entry_path" not in delta_cols:
            self._connection.execute(
                "ALTER TABLE mod_deltas ADD COLUMN entry_path TEXT"
            )
            logger.info("Migrated: added entry_path column to mod_deltas")

        # Add json_patches column for JSON patch merge support
        if "json_patches" not in delta_cols:
            self._connection.execute(
                "ALTER TABLE mod_deltas ADD COLUMN json_patches TEXT"
            )
            logger.info("Migrated: added json_patches column to mod_deltas")

        # Add force_inplace column to mods for per-mod overlay bypass
        if "force_inplace" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN force_inplace INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("Migrated: added force_inplace column to mods")

        # Add notes column to mods for user notes per mod
        if "notes" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN notes TEXT"
            )
            logger.info("Migrated: added notes column to mods")

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
