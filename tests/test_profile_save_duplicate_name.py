"""Saving a profile under an existing name must replace, not crash.

``profiles.name`` is UNIQUE; the old ``save_profile`` ran a bare
INSERT, so re-using a name raised ``sqlite3.IntegrityError`` straight
through the GUI's save handler. The GUI treats the name as the
profile's identity (it ignores the returned id), so the contract is:
same name replaces the previous snapshot, all writes in one
transaction.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.profile_manager import ProfileManager
from cdumm.storage.database import Database


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "cdumm.db")
    database.initialize()
    database.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority) "
        "VALUES ('ModA', 'paz', 1, 1)")
    database.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority) "
        "VALUES ('ModB', 'paz', 1, 2)")
    database.connection.commit()
    yield database
    database.close()


def test_duplicate_name_does_not_raise(db: Database) -> None:
    pm = ProfileManager(db)
    pm.save_profile("Main")
    pm.save_profile("Main")  # must not raise IntegrityError


def test_duplicate_name_replaces_previous_snapshot(db: Database) -> None:
    pm = ProfileManager(db)
    first_id = pm.save_profile("Main")

    # User toggles everything off, saves under the same name again.
    db.connection.execute("UPDATE mods SET enabled = 0")
    db.connection.commit()
    second_id = pm.save_profile("Main")

    profiles = pm.list_profiles()
    assert len(profiles) == 1, "same name must replace, not duplicate"
    assert profiles[0]["name"] == "Main"

    states = pm.get_profile_mods(second_id)
    assert len(states) == 2
    assert all(s["enabled"] is False for s in states), (
        "replacement must capture the CURRENT mod states")

    # No orphaned rows from the replaced profile.
    orphans = db.connection.execute(
        "SELECT COUNT(*) FROM profile_mods WHERE profile_id = ?",
        (first_id,)).fetchone()[0]
    assert orphans == 0
    total = db.connection.execute(
        "SELECT COUNT(*) FROM profile_mods").fetchone()[0]
    assert total == 2


def test_failed_save_rolls_back_previous_profile(db: Database,
                                                 monkeypatch) -> None:
    """If the re-save fails midway, the previously saved profile must
    survive (the DELETE+INSERT pair is one transaction)."""
    import sqlite3

    pm = ProfileManager(db)
    profile_id = pm.save_profile("Main")

    real_conn = db.connection

    class FailingConn:
        """sqlite3.Connection proxy that fails the profile_mods write.
        (sqlite3.Connection is a C type; its methods can't be
        monkeypatched directly.)"""

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO profile_mods"):
                raise sqlite3.OperationalError(
                    "simulated mid-save failure")
            return real_conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    monkeypatch.setattr(db, "_connection", FailingConn())
    with pytest.raises(sqlite3.OperationalError):
        pm.save_profile("Main")
    monkeypatch.undo()

    profiles = pm.list_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "Main"
    states = pm.get_profile_mods(profiles[0]["id"])
    assert len(states) == 2, (
        "a failed re-save must leave the previous snapshot intact")
    assert profiles[0]["id"] == profile_id
