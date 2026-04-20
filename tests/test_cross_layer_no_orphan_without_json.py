"""#145 Bug A: cross-layer must NOT skip PAZ-dir staging when no
enabled JSON mod is going to patch the overridden file.

Scenario from user log 2026-04-20 17:22:55 (Fat Stacks PAZ variant
alone enabled, no JSON mods):
  - Override map correctly identified Fat Stacks' iteminfo.pabgb in
    0036/0.paz as an override candidate.
  - Phase 1 skipped staging 0036/0.paz (wrong — nothing else was
    going to fill that slot).
  - Phase 2 still staged 0036/0.pamt (the index), pointing to the
    non-existent 0036/0.paz.
  - Game loaded the orphan PAMT, tried to read iteminfo.pabgb from a
    file that doesn't exist → crash.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cdumm.engine.apply_engine import collect_enabled_json_targets


def _mk_db():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
                "mod_type TEXT, enabled INTEGER, priority INTEGER, "
                "json_source TEXT)")
    con.execute("CREATE TABLE mod_deltas (id INTEGER PRIMARY KEY, "
                "mod_id INTEGER, file_path TEXT, delta_path TEXT, "
                "entry_path TEXT)")

    class _DbShim:
        connection = con
    return _DbShim()


def test_collect_enabled_json_targets_empty_when_no_json_mods():
    db = _mk_db()
    db.connection.execute(
        "INSERT INTO mods VALUES (1, 'Fat Stacks', 'paz', 1, 1, NULL)")
    db.connection.commit()
    assert collect_enabled_json_targets(db) == set(), (
        "no JSON mods = no JSON targets; nothing should be in the set")


def test_collect_enabled_json_targets_reads_json_source():
    db = _mk_db()
    db.connection.execute(
        "INSERT INTO mods VALUES (1, 'ES', 'paz', 1, 1, ?)",
        ('{"patches":[{"game_file":"gamedata/iteminfo.pabgb","changes":[]}]}',))
    db.connection.commit()
    targets = collect_enabled_json_targets(db)
    assert "gamedata/iteminfo.pabgb" in targets


def test_collect_enabled_json_targets_skips_disabled():
    db = _mk_db()
    db.connection.execute(
        "INSERT INTO mods VALUES (1, 'ES', 'paz', 0, 1, ?)",
        ('{"patches":[{"game_file":"gamedata/iteminfo.pabgb","changes":[]}]}',))
    db.connection.commit()
    assert collect_enabled_json_targets(db) == set(), (
        "disabled mod should not contribute targets")


def test_collect_enabled_json_targets_includes_entr_deltas():
    """Older JSON mods are imported as ENTR deltas (json_source=NULL,
    entry_path set on mod_deltas rows). These still target files
    during apply and must be included in the JSON-targets set so
    cross-layer doesn't orphan-skip a PAZ-dir mod staging the same
    logical file."""
    db = _mk_db()
    db.connection.execute(
        "INSERT INTO mods VALUES (1, 'ES', 'paz', 1, 1, NULL)")
    db.connection.execute(
        "INSERT INTO mod_deltas VALUES (1, 1, '0008/0.paz', "
        "'/tmp/x.entr', 'gamedata/iteminfo.pabgb')")
    db.connection.commit()
    targets = collect_enabled_json_targets(db)
    assert "gamedata/iteminfo.pabgb" in targets, (
        "ENTR-delta mods must surface their entry_path as a target")
