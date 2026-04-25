"""Cover the duplicate-detection + canonical-pick + merge logic."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cdumm.engine.mod_dedup import (
    _Row,
    apply_cleanup,
    find_duplicate_groups,
    merge_into_canonical,
    pick_canonical_row,
    plan_cleanup,
)
from cdumm.engine.mod_manager import ModManager
from cdumm.storage.database import Database


def _row(**kw) -> _Row:
    """Build a _Row with sensible defaults; override with kwargs."""
    base = dict(
        id=1, name="Mod", enabled=1, applied=0, priority=10,
        import_date="2026-04-25 05:00:00", version=None, drop_name=None,
        nexus_mod_id=None, nexus_real_file_id=None,
    )
    base.update(kw)
    return _Row(**base)


# ── pick_canonical_row ───────────────────────────────────────────────


def test_canonical_prefers_applied():
    new = _row(id=2, applied=0, enabled=1, priority=99)
    old = _row(id=1, applied=1, enabled=1, priority=10)
    assert pick_canonical_row([new, old]) is old


def test_canonical_prefers_enabled_when_applied_tied():
    a = _row(id=1, applied=0, enabled=1)
    b = _row(id=2, applied=0, enabled=0, priority=100)
    assert pick_canonical_row([a, b]) is a


def test_canonical_prefers_more_nexus_metadata():
    bare = _row(id=1, applied=0, enabled=1, version=None,
                nexus_mod_id=None, nexus_real_file_id=None)
    rich = _row(id=2, applied=0, enabled=1, version="1.5",
                nexus_mod_id=618, nexus_real_file_id=5733,
                drop_name="X-618-1-5-1776942172")
    assert pick_canonical_row([bare, rich]) is rich


def test_canonical_falls_back_to_priority_then_import_date():
    older = _row(id=1, applied=0, enabled=0, priority=10,
                  import_date="2026-04-23 10:00:00")
    newer = _row(id=2, applied=0, enabled=0, priority=10,
                  import_date="2026-04-25 05:00:00")
    assert pick_canonical_row([older, newer]) is newer


# ── merge_into_canonical ─────────────────────────────────────────────


def test_merge_fills_only_missing_fields():
    canon = _row(id=1, version=None, drop_name=None,
                  nexus_real_file_id=None, nexus_mod_id=None)
    sib = _row(id=2, version="1.5", drop_name="X-618-1-5-1776942172",
                nexus_real_file_id=5733, nexus_mod_id=618)
    update = merge_into_canonical(canon, [sib])
    assert update == {
        "version": "1.5",
        "drop_name": "X-618-1-5-1776942172",
        "nexus_real_file_id": 5733,
        "nexus_mod_id": 618,
    }


def test_merge_does_not_overwrite_existing_fields():
    canon = _row(id=1, version="1.0", nexus_real_file_id=4000)
    sib = _row(id=2, version="2.0", nexus_real_file_id=5000)
    update = merge_into_canonical(canon, [sib])
    assert update == {}  # both kept fields already populated


def test_merge_with_no_siblings_returns_empty():
    canon = _row(id=1, version=None)
    assert merge_into_canonical(canon, []) == {}


# ── find_duplicate_groups + plan_cleanup (DB-level) ──────────────────


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    yield database
    database.close()


def _insert(conn, **kw):
    cols = list(kw.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT INTO mods ({col_names}) VALUES ({placeholders})",
        list(kw.values()),
    )
    conn.commit()


def test_find_groups_finds_only_multi_row_names(db):
    _insert(db.connection, name="Solo", mod_type="paz", enabled=1, priority=1)
    _insert(db.connection, name="Dup", mod_type="paz", enabled=1, priority=2)
    _insert(db.connection, name="Dup", mod_type="paz", enabled=0, priority=3)
    groups = find_duplicate_groups(db.connection)
    assert list(groups.keys()) == ["Dup"]
    assert len(groups["Dup"]) == 2


def test_plan_cleanup_uses_canonical_then_merge(db):
    _insert(db.connection,
             name="X", mod_type="paz", enabled=1, applied=1, priority=5,
             version=None, drop_name=None, nexus_real_file_id=5733,
             nexus_mod_id=618, import_date="2026-04-23 10:00:00")
    _insert(db.connection,
             name="X", mod_type="paz", enabled=0, applied=0, priority=40,
             version="1.5", drop_name="X-618-1-5-1776942172",
             nexus_real_file_id=None, nexus_mod_id=618,
             import_date="2026-04-25 05:00:00")
    plan = plan_cleanup(db.connection)
    assert len(plan) == 1
    canon, deleted, update = plan[0]
    # Canonical wins on applied=1 even though the new row has more
    # version-string metadata + higher priority.
    assert canon.applied == 1
    assert canon.priority == 5
    assert len(deleted) == 1
    assert deleted[0].priority == 40
    # Missing fields on canonical are filled from sibling.
    assert update == {
        "version": "1.5",
        "drop_name": "X-618-1-5-1776942172",
    }


# ── apply_cleanup integration with ModManager ────────────────────────


def test_apply_cleanup_persists_merge_and_removes_duplicates(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.initialize()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    (tmp_path / "sources").mkdir()

    _insert(db.connection,
             name="X", mod_type="paz", enabled=1, applied=1, priority=5,
             version=None, drop_name=None, nexus_real_file_id=5733,
             import_date="2026-04-23 10:00:00")
    _insert(db.connection,
             name="X", mod_type="paz", enabled=0, applied=0, priority=40,
             version="1.5", drop_name="X-618-1-5-1776942172",
             nexus_real_file_id=None, import_date="2026-04-25 05:00:00")

    mgr = ModManager(db, deltas_dir)
    results = apply_cleanup(mgr)

    assert len(results) == 1
    kept_id, deleted_ids = results[0]
    assert len(deleted_ids) == 1

    # The kept row's missing fields got filled from the deleted sibling.
    row = db.connection.execute(
        "SELECT version, drop_name, nexus_real_file_id "
        "FROM mods WHERE id = ?", (kept_id,)).fetchone()
    assert row[0] == "1.5"
    assert row[1] == "X-618-1-5-1776942172"
    # The kept row's pre-existing nexus_real_file_id is preserved.
    assert row[2] == 5733

    # The deleted row is gone.
    remaining = db.connection.execute(
        "SELECT COUNT(*) FROM mods").fetchone()[0]
    assert remaining == 1
    db.close()


def test_apply_cleanup_noop_when_no_duplicates(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.initialize()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    (tmp_path / "sources").mkdir()

    _insert(db.connection, name="A", mod_type="paz", enabled=1, priority=1)
    _insert(db.connection, name="B", mod_type="paz", enabled=1, priority=2)

    mgr = ModManager(db, deltas_dir)
    results = apply_cleanup(mgr)
    assert results == []
    assert db.connection.execute(
        "SELECT COUNT(*) FROM mods").fetchone()[0] == 2
    db.close()
