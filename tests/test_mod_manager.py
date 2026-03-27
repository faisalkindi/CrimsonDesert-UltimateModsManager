from pathlib import Path

from cdumm.engine.mod_manager import ModManager
from cdumm.storage.database import Database


def _create_test_mod(db: Database, mod_id: int, name: str, enabled: bool = True) -> None:
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) VALUES (?, ?, ?, ?)",
        (mod_id, name, "paz", 1 if enabled else 0),
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
        "VALUES (?, ?, ?, ?, ?)",
        (mod_id, "0008/0.paz", f"/fake/{mod_id}.bsdiff", 100, 200),
    )
    db.connection.commit()


def test_list_mods(db: Database, tmp_path: Path) -> None:
    _create_test_mod(db, 1, "ModA")
    _create_test_mod(db, 2, "ModB")

    mgr = ModManager(db, tmp_path / "deltas")
    mods = mgr.list_mods()
    assert len(mods) == 2
    assert mods[0]["name"] == "ModA"
    assert mods[1]["name"] == "ModB"


def test_list_mods_by_type(db: Database, tmp_path: Path) -> None:
    _create_test_mod(db, 1, "PazMod")
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) VALUES (2, 'AsiMod', 'asi', 1)"
    )
    db.connection.commit()

    mgr = ModManager(db, tmp_path / "deltas")
    paz_mods = mgr.list_mods("paz")
    assert len(paz_mods) == 1
    assert paz_mods[0]["name"] == "PazMod"


def test_set_enabled(db: Database, tmp_path: Path) -> None:
    _create_test_mod(db, 1, "ModA", enabled=True)
    mgr = ModManager(db, tmp_path / "deltas")

    mgr.set_enabled(1, False)
    mods = mgr.list_mods()
    assert mods[0]["enabled"] is False

    mgr.set_enabled(1, True)
    mods = mgr.list_mods()
    assert mods[0]["enabled"] is True


def test_remove_mod(db: Database, tmp_path: Path) -> None:
    _create_test_mod(db, 1, "ModA")

    # Create fake delta directory
    delta_dir = tmp_path / "deltas" / "1"
    delta_dir.mkdir(parents=True)
    (delta_dir / "test.bsdiff").write_bytes(b"fake")

    mgr = ModManager(db, tmp_path / "deltas")
    mgr.remove_mod(1)

    assert mgr.get_mod_count() == 0
    assert not delta_dir.exists()

    # Verify cascading delete of mod_deltas
    cursor = db.connection.execute("SELECT COUNT(*) FROM mod_deltas WHERE mod_id = 1")
    assert cursor.fetchone()[0] == 0


def test_get_mod_details(db: Database, tmp_path: Path) -> None:
    _create_test_mod(db, 1, "ModA")

    mgr = ModManager(db, tmp_path / "deltas")
    details = mgr.get_mod_details(1)

    assert details is not None
    assert details["name"] == "ModA"
    assert details["mod_type"] == "paz"
    assert len(details["changed_files"]) == 1
    assert details["changed_files"][0]["file_path"] == "0008/0.paz"


def test_get_mod_details_nonexistent(db: Database, tmp_path: Path) -> None:
    mgr = ModManager(db, tmp_path / "deltas")
    assert mgr.get_mod_details(999) is None


def test_mod_count(db: Database, tmp_path: Path) -> None:
    mgr = ModManager(db, tmp_path / "deltas")
    assert mgr.get_mod_count() == 0

    _create_test_mod(db, 1, "ModA")
    assert mgr.get_mod_count() == 1
