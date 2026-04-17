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
    level TEXT NOT NULL CHECK(level IN ('papgt', 'paz', 'byte_range', 'semantic')),
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

    def _backfill_mod_versions(self) -> None:
        """Populate ``mods.version`` from ``drop_name`` for legacy imports.

        Runs exactly once per DB (guarded by the ``version_backfill_done``
        flag in the ``config`` table). Walks every ``mods`` row where
        ``version`` is NULL or empty but ``drop_name`` is present, parses
        the drop_name with the unified version extractor, and writes the
        result back. Also extracts ``nexus_mod_id`` when available so
        update-check linking works after switching to the API branch.
        """
        flag_row = self._connection.execute(
            "SELECT value FROM config WHERE key = 'version_backfill_done'"
        ).fetchone()
        if flag_row and flag_row[0] == "1":
            return

        try:
            from cdumm.engine.nexus_filename import (
                extract_version_from_filename, parse_nexus_filename,
            )
        except Exception as e:
            logger.warning("Version backfill: parser import failed (%s)", e)
            return

        rows = self._connection.execute(
            "SELECT id, drop_name FROM mods "
            "WHERE (version IS NULL OR version = '') "
            "AND drop_name IS NOT NULL AND drop_name != ''"
        ).fetchall()

        updated = 0
        linked = 0
        for mod_id, drop_name in rows:
            # drop_name is stored as the raw filename (with extension).
            # Strip common archive extensions before parsing.
            stem = drop_name
            for ext in (".zip", ".7z", ".rar", ".json", ".bsdiff"):
                if stem.lower().endswith(ext):
                    stem = stem[: -len(ext)]
                    break

            version_val = extract_version_from_filename(stem)
            if version_val:
                self._connection.execute(
                    "UPDATE mods SET version = ? WHERE id = ?",
                    (version_val, mod_id))
                updated += 1

            nexus_id, _ = parse_nexus_filename(stem)
            if nexus_id:
                cur = self._connection.execute(
                    "UPDATE mods SET nexus_mod_id = ? "
                    "WHERE id = ? AND nexus_mod_id IS NULL",
                    (nexus_id, mod_id))
                # Only count rows that actually changed; otherwise a
                # mod that already has nexus_mod_id inflates the log.
                if cur.rowcount:
                    linked += 1

        self._connection.execute(
            "INSERT OR REPLACE INTO config (key, value) "
            "VALUES ('version_backfill_done', '1')")
        self._connection.commit()
        if updated or linked:
            logger.info(
                "Version backfill: populated %d version(s), "
                "linked %d Nexus id(s) across %d rows",
                updated, linked, len(rows))
        else:
            logger.info(
                "Version backfill: nothing to update (%d rows scanned)",
                len(rows))

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

        # Add kind column to mod_deltas to discriminate delta type.
        # '' (empty/default) = byte-range delta (original behaviour).
        # 'xml_patch' = XPath patch file stored at delta_path, target = file_path.
        # 'xml_merge' = identity-key XML merge file stored at delta_path.
        if "kind" not in delta_cols:
            self._connection.execute(
                "ALTER TABLE mod_deltas ADD COLUMN kind TEXT NOT NULL DEFAULT ''"
            )
            logger.info("Migrated: added kind column to mod_deltas")

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

        # Create semantic_resolutions table for conflict resolution persistence
        if not self.table_exists("semantic_resolutions"):
            self._connection.execute("""
                CREATE TABLE semantic_resolutions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name TEXT NOT NULL,
                    record_key INTEGER NOT NULL,
                    field_name TEXT NOT NULL,
                    winning_mod TEXT NOT NULL,
                    UNIQUE(table_name, record_key, field_name)
                )
            """)
            logger.info("Created semantic_resolutions table")

        # Add group_id column to mods for folder groups
        if "group_id" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN group_id INTEGER"
            )
            logger.info("Migrated: added group_id column to mods")

        # Create mod_groups table for user folder groups
        if not self.table_exists("mod_groups"):
            self._connection.execute("""
                CREATE TABLE mod_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0
                )
            """)
            logger.info("Created mod_groups table")

        # Create asi_plugin_state table for ASI plugin folder/ordering state
        if not self.table_exists("asi_plugin_state"):
            self._connection.execute("""
                CREATE TABLE asi_plugin_state (
                    name TEXT PRIMARY KEY,
                    group_id INTEGER,
                    priority INTEGER NOT NULL DEFAULT 0,
                    install_date TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
            """)
            logger.info("Created asi_plugin_state table")

        # Add version + nexus_mod_id columns to asi_plugin_state if missing
        if self.table_exists("asi_plugin_state"):
            cursor = self._connection.execute("PRAGMA table_info(asi_plugin_state)")
            asi_cols = {row[1] for row in cursor.fetchall()}
            if "version" not in asi_cols:
                self._connection.execute(
                    "ALTER TABLE asi_plugin_state ADD COLUMN version TEXT"
                )
                logger.info("Migrated: added version column to asi_plugin_state")
            if "nexus_mod_id" not in asi_cols:
                self._connection.execute(
                    "ALTER TABLE asi_plugin_state ADD COLUMN nexus_mod_id INTEGER"
                )
                logger.info("Migrated: added nexus_mod_id column to asi_plugin_state")

        # Create asi_groups table — separate from mod_groups so PAZ and ASI
        # have independent folder structures
        if not self.table_exists("asi_groups"):
            self._connection.execute("""
                CREATE TABLE asi_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0
                )
            """)
            logger.info("Created asi_groups table")

        # Add disabled_patches column to mods for per-patch toggle
        if "disabled_patches" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN disabled_patches TEXT"
            )
            logger.info("Migrated: added disabled_patches column to mods")

        # Add variants column to mods for multi-variant JSON-patch mods.
        # Stores a JSON array of {label, filename, enabled, group} dicts; the
        # cog-opened config panel reads it to render radio groups (same group
        # id) or independent checkboxes (group = -1). json_source points at a
        # synthesized merged.json that reflects the currently-enabled subset.
        if "variants" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN variants TEXT"
            )
            logger.info("Migrated: added variants column to mods")

        # Add json_source column to mods for mount-time patching
        if "json_source" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN json_source TEXT"
            )
            logger.info("Migrated: added json_source column to mods")

        # Store original drop folder/file name for version extraction
        if "drop_name" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN drop_name TEXT"
            )
            logger.info("Migrated: added drop_name column to mods")

        # Add NexusMods tracking columns
        if "nexus_mod_id" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN nexus_mod_id INTEGER"
            )
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN nexus_file_id TEXT"
            )
            logger.info("Migrated: added nexus_mod_id, nexus_file_id columns to mods")

        if "applied" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN applied INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("Migrated: added applied column to mods")

        # Add conflict_mode column to mods for per-mod override declaration
        if "conflict_mode" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN conflict_mode TEXT NOT NULL DEFAULT 'normal'"
            )
            logger.info("Migrated: added conflict_mode column to mods")

        # Add target_language column to mods for language mod detection
        if "target_language" not in columns:
            self._connection.execute(
                "ALTER TABLE mods ADD COLUMN target_language TEXT"
            )
            logger.info("Migrated: added target_language column to mods")

        # Add custom_values column to mod_config for inline value editing
        cursor_mc = self._connection.execute("PRAGMA table_info(mod_config)")
        mc_cols = {row[1] for row in cursor_mc.fetchall()}
        if "custom_values" not in mc_cols:
            self._connection.execute(
                "ALTER TABLE mod_config ADD COLUMN custom_values TEXT"
            )
            logger.info("Migrated: added custom_values column to mod_config")

        # One-shot backfill: mods imported on master v3.0.1 or earlier have
        # empty mods.version because parse_nexus_filename was a stub. Now
        # that master ships the full parser, retroactively populate the
        # version column for every mod whose drop_name can be parsed.
        # Guarded by a config-table flag so it runs exactly once per DB.
        self._backfill_mod_versions()

        # Create crash_registry table for mod health tracking
        if not self.table_exists("crash_registry"):
            self._connection.execute("""
                CREATE TABLE crash_registry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mod_id INTEGER NOT NULL REFERENCES mods(id) ON DELETE CASCADE,
                    mod_name TEXT NOT NULL,
                    delta_hash TEXT NOT NULL,
                    flagged_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    flagged_by TEXT NOT NULL DEFAULT 'auto_bisect',
                    crashes_alone INTEGER NOT NULL DEFAULT 0,
                    context_mods TEXT,
                    game_version TEXT,
                    rounds_to_find INTEGER,
                    UNIQUE(mod_id, delta_hash)
                )
            """)
            logger.info("Created crash_registry table")

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
