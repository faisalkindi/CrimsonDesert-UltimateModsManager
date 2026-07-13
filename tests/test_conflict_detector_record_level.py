"""Two mods editing the same TABLE are not two mods editing the same THING.

GitHub #292 (falobos76, via #191): every one of pinapana's socket mods was
listed as conflicting with every other, so only the top one "won" — while
in game all of them applied perfectly, to different items. The apply path
was right; the report was wrong, and it pushes people to disable mods that
were working.

The detector compared at file/entry granularity. It needs record + field.
"""
import json

import pytest

from cdumm.engine.conflict_detector import ConflictDetector
from cdumm.storage.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "cdumm.db")
    d.initialize()
    yield d
    d.close()


def _f3(tmp_path, name, intents, target="iteminfo.pabgb"):
    p = tmp_path / f"{name}.field.json"
    p.write_text(json.dumps({
        "format": 3, "target": target,
        "modinfo": {"title": name},
        "intents": [
            {"entry": "", "key": k, "field": f, "op": "set", "new": v}
            for k, f, v in intents
        ],
    }), encoding="utf-8")
    return p


def _add_mod(db, name, json_source, priority, entry="iteminfo.pabgb"):
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, json_source) "
        "VALUES (?, 'paz', 1, ?, ?)", (name, priority, str(json_source)))
    mod_id = cur.lastrowid
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, "
        "byte_end, entry_path) VALUES (?, '0008/0.paz', '', 100, 200, ?)",
        (mod_id, entry))
    db.connection.commit()
    return mod_id


def test_different_items_same_table_is_not_a_conflict(db, tmp_path):
    """The reported bug. Sockets on helmets + sockets on gloves = fine."""
    a = _add_mod(db, "Armor Five Sockets",
                 _f3(tmp_path, "a", [(1001, "use_socket", 1),
                                     (1002, "use_socket", 1)]), 1)
    b = _add_mod(db, "Weapon Five Sockets",
                 _f3(tmp_path, "b", [(2001, "use_socket", 1),
                                     (2002, "use_socket", 1)]), 2)

    conflicts = ConflictDetector(db).detect_all()

    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.level == "paz", "different items must not be a conflict"
    assert c.winner_id is None, "a compatible pair has no loser"
    assert "different items" in c.explanation
    assert "compatible" in c.explanation.lower()

    statuses = ConflictDetector(db).get_all_mod_statuses()
    assert statuses[a] == "clean"
    assert statuses[b] == "clean"


def test_same_item_different_fields_is_not_a_conflict(db, tmp_path):
    """pinapana ships 'All EQ Dyeable' explicitly to be used WITH a socket
    mod. Same items, different fields — apply merges them per field."""
    _add_mod(db, "5 Socket",
             _f3(tmp_path, "a", [(1001, "use_socket", 1)]), 1)
    _add_mod(db, "All EQ Dyeable",
             _f3(tmp_path, "b", [(1001, "is_dyeable", 1)]), 2)

    conflicts = ConflictDetector(db).detect_all()

    assert len(conflicts) == 1
    assert conflicts[0].level == "paz"
    assert conflicts[0].winner_id is None
    assert "different fields" in conflicts[0].explanation


def test_same_item_same_field_IS_a_conflict(db, tmp_path):
    """The guard must still fire. Two mods setting the same field on the
    same item genuinely disagree, and one of them will lose."""
    a = _add_mod(db, "Two Sockets",
                 _f3(tmp_path, "a", [(1001, "socket_count", 2)]), 1)
    b = _add_mod(db, "Five Sockets",
                 _f3(tmp_path, "b", [(1001, "socket_count", 5)]), 2)

    conflicts = ConflictDetector(db).detect_all()

    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.level == "semantic"
    assert c.winner_id == a, "lower priority number wins (applied last)"
    assert "socket_count" in c.explanation
    assert "1 of the same item" in c.explanation

    statuses = ConflictDetector(db).get_all_mod_statuses()
    assert statuses[a] == "resolved"
    assert statuses[b] == "resolved"


def test_a_real_conflict_is_never_reported_as_clean(db, tmp_path):
    """The 'semantic' level existed but was never emitted (its input was a
    metadata key nothing in the codebase writes), so neither status
    function handled it. Making it fire without this would have turned a
    real conflict into a CLEAN badge."""
    a = _add_mod(db, "A", _f3(tmp_path, "a", [(1, "price", 10)]), 1)
    b = _add_mod(db, "B", _f3(tmp_path, "b", [(1, "price", 20)]), 2)

    det = ConflictDetector(db)
    det.detect_all()

    assert det.get_mod_status(a) != "clean"
    assert det.get_mod_status(b) != "clean"


def test_partial_overlap_reports_only_the_shared_records(db, tmp_path):
    a = _add_mod(db, "A", _f3(tmp_path, "a", [(1, "price", 10),
                                              (2, "price", 10),
                                              (3, "price", 10)]), 1)
    _add_mod(db, "B", _f3(tmp_path, "b", [(3, "price", 20),
                                          (4, "price", 20)]), 2)

    conflicts = ConflictDetector(db).detect_all()

    assert len(conflicts) == 1
    assert conflicts[0].level == "semantic"
    assert "1 of the same item" in conflicts[0].explanation
    assert conflicts[0].winner_id == a


def test_a_match_mod_falls_back_instead_of_guessing(db, tmp_path):
    """`match` selects records by a predicate, so which items it hits isn't
    knowable without the game files. Don't claim compatibility we can't
    prove — keep the conservative verdict."""
    p = tmp_path / "m.field.json"
    p.write_text(json.dumps({
        "format": 3, "target": "iteminfo.pabgb",
        "modinfo": {"title": "m"},
        "intents": [{"entry": "", "field": "price", "op": "match",
                     "match": {"item_tier": 5}, "new": 1}],
    }), encoding="utf-8")
    _add_mod(db, "Match Mod", p, 1)
    _add_mod(db, "Plain Mod",
             _f3(tmp_path, "b", [(9999, "price", 20)]), 2)

    conflicts = ConflictDetector(db).detect_all()

    assert len(conflicts) == 1
    assert conflicts[0].level == "byte_range", (
        "unknown records must NOT be reported as compatible")
    assert conflicts[0].winner_id is not None


def test_a_mod_with_no_intents_file_keeps_the_old_verdict(db, tmp_path):
    """Non-Format-3 mods have no json_source. Nothing to compare, so the
    conservative behaviour must survive untouched."""
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority) "
        "VALUES ('Old A', 'paz', 1, 1)")
    a = cur.lastrowid
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority) "
        "VALUES ('Old B', 'paz', 1, 2)")
    b = cur.lastrowid
    for mid in (a, b):
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, entry_path) "
            "VALUES (?, '0008/0.paz', '', 100, 200, 'iteminfo.pabgb')",
            (mid,))
    db.connection.commit()

    conflicts = ConflictDetector(db).detect_all()

    assert len(conflicts) == 1
    assert conflicts[0].level == "byte_range"
    assert conflicts[0].winner_id is not None


def test_the_target_may_be_a_path_or_a_bare_name(db, tmp_path):
    """Format 3 ships `iteminfo.pabgb`; an entry_path can be
    `gamedata/iteminfo.pabgb`. Comparing them raw never matches — the same
    trap that made `match` select 0 records (#275) and array_append a no-op
    (#278)."""
    _add_mod(db, "A", _f3(tmp_path, "a", [(1, "price", 10)]), 1,
             entry="gamedata/iteminfo.pabgb")
    _add_mod(db, "B", _f3(tmp_path, "b", [(2, "price", 20)]), 2,
             entry="gamedata/iteminfo.pabgb")

    conflicts = ConflictDetector(db).detect_all()

    assert len(conflicts) == 1
    assert conflicts[0].level == "paz", (
        "the path/bare-name mismatch must not defeat the comparison")
    assert conflicts[0].winner_id is None
