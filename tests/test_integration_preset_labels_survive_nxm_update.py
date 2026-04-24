"""Integration test for Bug 24/34: a configurable mod whose selected
preset labels are captured BEFORE ``remove_mod`` must have those
labels restored to the new mod_config row after the nxm:// update
completes.

End-to-end shape:

    1. Seed a mod row + a mod_config row with selected_labels
       ``{"Preset A": True, "Preset B": False, "Preset C": True}``.
    2. Simulate the Update Mod? pre-check: call
       ``_snapshot_selected_labels(db, mod_id)`` and stash.
    3. Delete the mod row (simulates ``mod_manager.remove_mod``).
    4. Insert a new mod row for the post-import result. This is
       what a real reimport would do.
    5. Call ``_restore_selected_labels`` with the stashed snapshot +
       the new mod's available preset names.
    6. Verify the new mod_config row has the correct labels.
    7. Verify labels the new mod no longer exposes are DROPPED from
       the restored set (author renamed/removed them).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def db_with_configurable_mod(tmp_path):
    """Produce a real Database with a seeded configurable mod."""
    from cdumm.storage.database import Database
    db_path = tmp_path / "cdumm.db"
    db = Database(db_path)
    db.initialize()
    # Seed a mod row.
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, "
        "configurable) VALUES (?, ?, ?, ?, ?, ?)",
        (42, "Cool Mod", "paz", 1, 1, 1))
    db.connection.execute(
        "INSERT INTO mod_config (mod_id, selected_labels) "
        "VALUES (?, ?)",
        (42, json.dumps({
            "Preset A": True,
            "Preset B": False,
            "Preset C": True,
        })))
    db.connection.commit()
    return db


def test_snapshot_captures_labels_then_restore_replays_them(
    db_with_configurable_mod,
):
    from cdumm.gui.fluent_window import (
        _snapshot_selected_labels,
        _restore_selected_labels,
    )
    db = db_with_configurable_mod

    # Step 1: Pre-update snapshot (called BEFORE remove_mod).
    snap = _snapshot_selected_labels(db, 42)
    assert snap == {
        "Preset A": True, "Preset B": False, "Preset C": True,
    }

    # Step 2: Simulate remove_mod — drop mod + cascade config.
    db.connection.execute("DELETE FROM mod_config WHERE mod_id = ?", (42,))
    db.connection.execute("DELETE FROM mods WHERE id = ?", (42,))
    db.connection.commit()
    # Confirm it's gone.
    gone = db.connection.execute(
        "SELECT COUNT(*) FROM mod_config WHERE mod_id = 42"
    ).fetchone()[0]
    assert gone == 0

    # Step 3: New row from the reimport (could be same or different id).
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (?, ?, ?, ?, ?)",
        (77, "Cool Mod", "paz", 1, 1))
    db.connection.commit()

    # Step 4: Restore — the new mod exposes the same preset names.
    _restore_selected_labels(
        db, mod_id=77, snapshot=snap,
        available_preset_names={"Preset A", "Preset B", "Preset C"})

    # Step 5: New row's mod_config reflects the captured selections.
    row = db.connection.execute(
        "SELECT selected_labels FROM mod_config WHERE mod_id = 77"
    ).fetchone()
    assert row is not None
    restored = json.loads(row[0])
    assert restored == {
        "Preset A": True, "Preset B": False, "Preset C": True,
    }


def test_restore_drops_labels_the_new_mod_no_longer_exposes(
    db_with_configurable_mod,
):
    """If the author renamed ``"Preset B"`` to ``"Preset D"`` in the
    update, the stale name must be dropped silently — the user's
    intent for A+C survives while B's stale vote is forgotten."""
    from cdumm.gui.fluent_window import (
        _snapshot_selected_labels, _restore_selected_labels,
    )
    db = db_with_configurable_mod
    snap = _snapshot_selected_labels(db, 42)
    db.connection.execute("DELETE FROM mods WHERE id = 42")
    db.connection.commit()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (?, ?, ?, ?, ?)",
        (77, "Cool Mod", "paz", 1, 1))
    db.connection.commit()
    # New mod exposes A, C, D (renamed B).
    _restore_selected_labels(
        db, mod_id=77, snapshot=snap,
        available_preset_names={"Preset A", "Preset C", "Preset D"})
    row = db.connection.execute(
        "SELECT selected_labels FROM mod_config WHERE mod_id = 77"
    ).fetchone()
    restored = json.loads(row[0])
    assert "Preset B" not in restored
    assert restored == {"Preset A": True, "Preset C": True}


def test_restore_no_op_when_no_snapshot_captured():
    """A regular import (no prior configurable mod to snapshot) must
    leave the new row's mod_config alone — no spurious INSERT."""
    from cdumm.storage.database import Database
    from cdumm.gui.fluent_window import _restore_selected_labels
    import tempfile as _tempfile
    tmpdir = _tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "cdumm.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (?, ?, ?, ?, ?)",
        (77, "Fresh Mod", "paz", 1, 1))
    db.connection.commit()
    _restore_selected_labels(
        db, mod_id=77, snapshot=None,
        available_preset_names={"whatever"})
    # No mod_config row created.
    count = db.connection.execute(
        "SELECT COUNT(*) FROM mod_config WHERE mod_id = 77"
    ).fetchone()[0]
    assert count == 0
