"""#477, the game-folder half: moving the game folder (with its default
CDMods inside) to another drive leaves every stored absolute path
naming the old root. The next launch's cleanup must re-root those
paths onto the CDMods folder it now runs from, not delete the rows.

AgentKush found it; v3.17.15's Settings-move rewrite didn't cover it.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.mod_manager import ModManager
from cdumm.storage.database import Database


def _library(tmp_path: Path):
    old_root = tmp_path / "C_drive" / "Crimson Desert" / "CDMods"
    new_root = tmp_path / "D_drive" / "Crimson Desert" / "CDMods"
    (new_root / "deltas" / "1").mkdir(parents=True)
    (new_root / "deltas" / "1" / "0008_0.paz.bsdiff").write_bytes(b"delta")
    (new_root / "mods").mkdir()
    (new_root / "mods" / "Json Mod.json").write_text("{}")
    (new_root / "sources" / "3").mkdir(parents=True)
    (new_root / "sources" / "3" / "mod.zip").write_bytes(b"zip")
    db = Database(new_root / "cdumm.db")
    db.initialize()
    c = db.connection
    c.execute("INSERT INTO mods (id, name, mod_type, enabled) "
              "VALUES (1, 'Byte Mod', 'paz', 0)")
    c.execute("INSERT INTO mod_deltas (mod_id, file_path, delta_path) "
              "VALUES (1, '0008/0.paz', ?)",
              (str(old_root / "deltas" / "1" / "0008_0.paz.bsdiff"),))
    c.execute("INSERT INTO mods (id, name, mod_type, enabled, json_source) "
              "VALUES (2, 'Json Mod', 'paz', 1, ?)",
              (str(old_root / "mods" / "Json Mod.json"),))
    c.execute("INSERT INTO mods (id, name, mod_type, enabled, source_path) "
              "VALUES (3, 'Zip Mod', 'paz', 1, ?)",
              (str(old_root / "sources" / "3" / "mod.zip"),))
    c.execute("INSERT INTO mods (id, name, mod_type, enabled) "
              "VALUES (4, 'Gone Mod', 'paz', 1)")
    c.execute("INSERT INTO mod_deltas (mod_id, file_path, delta_path) "
              "VALUES (4, '0009/0.paz', ?)",
              (str(old_root / "deltas" / "4" / "0009_0.paz.bsdiff"),))
    c.commit()
    return db, new_root


def test_launch_cleanup_reroots_paths_after_the_game_folder_moved(tmp_path):
    db, new_root = _library(tmp_path)

    ModManager(db, new_root / "deltas").cleanup_orphaned_deltas()

    c = db.connection
    assert c.execute("SELECT delta_path FROM mod_deltas WHERE mod_id = 1"
                     ).fetchone()[0] == str(
        new_root / "deltas" / "1" / "0008_0.paz.bsdiff")
    assert c.execute("SELECT json_source FROM mods WHERE id = 2"
                     ).fetchone()[0] == str(new_root / "mods" / "Json Mod.json")
    assert c.execute("SELECT source_path FROM mods WHERE id = 3"
                     ).fetchone()[0] == str(new_root / "sources" / "3" / "mod.zip")
    names = {n for (n,) in c.execute("SELECT name FROM mods")}
    assert "Byte Mod" in names, "the disabled byte mod was swept as a zombie"


def test_a_delta_missing_everywhere_is_still_cleaned_up(tmp_path):
    db, new_root = _library(tmp_path)

    ModManager(db, new_root / "deltas").cleanup_orphaned_deltas()

    assert db.connection.execute(
        "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = 4").fetchone()[0] == 0


def test_a_path_outside_cdmods_is_left_alone(tmp_path):
    db, new_root = _library(tmp_path)
    outside = str(tmp_path / "Downloads" / "mods" / "Json Mod.json")
    db.connection.execute(
        "UPDATE mods SET source_path = ? WHERE id = 2", (outside,))
    db.connection.commit()

    ModManager(db, new_root / "deltas").cleanup_orphaned_deltas()

    assert db.connection.execute(
        "SELECT source_path FROM mods WHERE id = 2").fetchone()[0] == outside
