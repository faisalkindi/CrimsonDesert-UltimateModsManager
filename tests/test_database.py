from pathlib import Path

from cdmm.storage.database import Database
from cdmm.storage.config import Config


EXPECTED_TABLES = ["config", "snapshots", "mods", "mod_deltas", "conflicts"]


def test_database_creates_all_tables(db: Database) -> None:
    for table in EXPECTED_TABLES:
        assert db.table_exists(table), f"Table '{table}' was not created"


def test_database_wal_mode(db: Database) -> None:
    cursor = db.connection.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    assert mode == "wal"


def test_database_foreign_keys_enabled(db: Database) -> None:
    cursor = db.connection.execute("PRAGMA foreign_keys")
    enabled = cursor.fetchone()[0]
    assert enabled == 1


def test_database_idempotent_initialize(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.initialize()  # Should not raise
    for table in EXPECTED_TABLES:
        assert db.table_exists(table)
    db.close()


def test_config_get_set(db: Database) -> None:
    config = Config(db)
    config.set("game_directory", "E:/SteamLibrary/steamapps/common/Crimson Desert")
    assert config.get("game_directory") == "E:/SteamLibrary/steamapps/common/Crimson Desert"


def test_config_get_missing_key(db: Database) -> None:
    config = Config(db)
    assert config.get("nonexistent_key") is None


def test_config_update_existing_key(db: Database) -> None:
    config = Config(db)
    config.set("game_directory", "/old/path")
    config.set("game_directory", "/new/path")
    assert config.get("game_directory") == "/new/path"


def test_database_connection_not_initialized() -> None:
    db = Database(Path("/fake/path.db"))
    try:
        _ = db.connection
        assert False, "Should have raised RuntimeError"
    except RuntimeError:
        pass


def test_mods_table_constraints(db: Database) -> None:
    db.connection.execute(
        "INSERT INTO mods (name, mod_type) VALUES (?, ?)",
        ("TestMod", "paz"),
    )
    db.connection.commit()
    cursor = db.connection.execute("SELECT name, mod_type, enabled FROM mods WHERE name = ?", ("TestMod",))
    row = cursor.fetchone()
    assert row == ("TestMod", "paz", 1)


def test_mods_table_rejects_invalid_type(db: Database) -> None:
    try:
        db.connection.execute(
            "INSERT INTO mods (name, mod_type) VALUES (?, ?)",
            ("BadMod", "invalid"),
        )
        db.connection.commit()
        assert False, "Should have raised IntegrityError"
    except Exception:
        db.connection.rollback()


def test_mod_deltas_cascade_delete(db: Database) -> None:
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type) VALUES (?, ?, ?)",
        (1, "TestMod", "paz"),
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path) VALUES (?, ?, ?)",
        (1, "0008/0.paz", "/deltas/1/0008_0.paz.bsdiff"),
    )
    db.connection.commit()
    db.connection.execute("DELETE FROM mods WHERE id = 1")
    db.connection.commit()
    cursor = db.connection.execute("SELECT COUNT(*) FROM mod_deltas WHERE mod_id = 1")
    assert cursor.fetchone()[0] == 0
