"""Dynamic allowlist: enabled JSON mods' target archives always get backed up.

JSON mods store their target PAZ archive in ``mod_deltas.file_path`` (e.g.
``"0008/0.paz"``). The scanner walks enabled JSON-source mods and returns the
set of distinct archive paths so the vanilla-backup refresh can keep them
on disk regardless of the size threshold.
"""
from __future__ import annotations

from cdumm.engine.json_target_scanner import enabled_json_target_archives


def _insert_json_mod(db, name: str, enabled: bool,
                     paz_files: list[str]) -> int:
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority, enabled, json_source) "
        "VALUES (?, 'paz', 0, ?, ?)",
        (name, 1 if enabled else 0, f"/tmp/{name}.json"))
    mod_id = cur.lastrowid
    for paz in paz_files:
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, entry_path) "
            "VALUES (?, ?, '', 0, 0, ?)",
            (mod_id, paz, f"gamedata/fake-{mod_id}.pabgb"))
    db.connection.commit()
    return mod_id


def _insert_non_json_mod(db, name: str, enabled: bool,
                        paz_files: list[str]) -> int:
    """Non-JSON paz mod (zip/folder import) — must be ignored by scanner."""
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority, enabled) "
        "VALUES (?, 'paz', 0, ?)",
        (name, 1 if enabled else 0))
    mod_id = cur.lastrowid
    for paz in paz_files:
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end) "
            "VALUES (?, ?, '/tmp/x', 0, 0)",
            (mod_id, paz))
    db.connection.commit()
    return mod_id


def test_no_json_mods_returns_empty_set(db):
    assert enabled_json_target_archives(db) == set()


def test_enabled_json_mod_yields_its_target_archive(db):
    _insert_json_mod(db, "InfiniteStamina", enabled=True,
                     paz_files=["0008/0.paz"])
    assert enabled_json_target_archives(db) == {"0008/0.paz"}


def test_multiple_enabled_json_mods_dedupe(db):
    _insert_json_mod(db, "A", enabled=True, paz_files=["0008/0.paz"])
    _insert_json_mod(db, "B", enabled=True,
                     paz_files=["0008/0.paz", "0009/0.paz"])
    assert enabled_json_target_archives(db) == {"0008/0.paz", "0009/0.paz"}


def test_disabled_json_mod_does_not_trigger_backup(db):
    _insert_json_mod(db, "Disabled", enabled=False,
                     paz_files=["0008/0.paz"])
    assert enabled_json_target_archives(db) == set()


def test_non_json_paz_mod_ignored(db):
    # Regular zip-imported paz mod that also patches 0008/0.paz. Not our
    # concern — the existing delta-based backup handles those.
    _insert_non_json_mod(db, "Zip", enabled=True,
                        paz_files=["0008/0.paz"])
    assert enabled_json_target_archives(db) == set()


def test_empty_file_path_ignored(db):
    mod_id = _insert_json_mod(db, "Odd", enabled=True, paz_files=[])
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end) VALUES (?, '', '', 0, 0)", (mod_id,))
    db.connection.commit()
    assert enabled_json_target_archives(db) == set()
