"""Task 1 for the Recovery Flow plan (v3.1.9).

Tests for `engine/recovery_candidates.py` — the pure-logic helpers
that partition enabled PAZ mods into (reimportable, skipped) and
disable the skipped set. Codex review findings 1-3 resolved.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _seed_paz_mod(db, *, name: str, enabled: int = 1,
                  source_path: str | None = None) -> int:
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, source_path) "
        "VALUES (?, 'paz', ?, 0, ?)",
        (name, enabled, source_path))
    db.connection.commit()
    return cur.lastrowid


def _seed_asi_mod(db, *, name: str, enabled: int = 1,
                  source_path: str | None = None) -> int:
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, source_path) "
        "VALUES (?, 'asi', ?, 0, ?)",
        (name, enabled, source_path))
    db.connection.commit()
    return cur.lastrowid


def test_mod_with_valid_source_path_is_reimportable(tmp_path: Path):
    """Happy path: source_path points at an existing folder → reimportable."""
    from cdumm.engine.recovery_candidates import reimport_candidates
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    src_dir = tmp_path / "mod_source"
    src_dir.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()
    mod_id = _seed_paz_mod(db, name="ValidMod", source_path=str(src_dir))

    reimportable, skipped = reimport_candidates(db, game_dir)
    assert [m["id"] for m in reimportable] == [mod_id]
    assert skipped == []
    db.close()


def test_mod_with_null_source_path_but_cdmods_sources_dir_is_reimportable(tmp_path: Path):
    """Codex finding 2: the CDMods/sources/<mod_id> fallback must count
    as reimportable. resolve_mod_source_path already handles this; our
    helper must use it, not raw SQL."""
    from cdumm.engine.recovery_candidates import reimport_candidates
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    db = Database(tmp_path / "test.db")
    db.initialize()
    mod_id = _seed_paz_mod(db, name="FallbackMod", source_path=None)

    # Materialise the fallback dir the helper should find.
    fallback_dir = game_dir / "CDMods" / "sources" / str(mod_id)
    fallback_dir.mkdir(parents=True)

    reimportable, skipped = reimport_candidates(db, game_dir)
    assert [m["id"] for m in reimportable] == [mod_id]
    assert skipped == []
    db.close()


def test_mod_with_null_source_path_and_no_fallback_is_skipped(tmp_path: Path):
    """No source_path AND no CDMods/sources/<mod_id> dir → skipped."""
    from cdumm.engine.recovery_candidates import reimport_candidates
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    db = Database(tmp_path / "test.db")
    db.initialize()
    mod_id = _seed_paz_mod(db, name="OrphanMod", source_path=None)

    reimportable, skipped = reimport_candidates(db, game_dir)
    assert reimportable == []
    assert [m["id"] for m in skipped] == [mod_id]
    db.close()


def test_mod_with_source_path_pointing_to_deleted_folder_is_skipped(tmp_path: Path):
    """Codex finding 2: the raw SQL predicate `source_path IS NOT NULL`
    passes a row whose path no longer exists on disk. The helper must
    reject it."""
    from cdumm.engine.recovery_candidates import reimport_candidates
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    gone_dir = tmp_path / "deleted_source"  # NOT created

    db = Database(tmp_path / "test.db")
    db.initialize()
    mod_id = _seed_paz_mod(db, name="StaleMod", source_path=str(gone_dir))

    reimportable, skipped = reimport_candidates(db, game_dir)
    assert reimportable == []
    assert [m["id"] for m in skipped] == [mod_id]
    db.close()


def test_disabled_mods_are_not_candidates(tmp_path: Path):
    """Only enabled mods are reimport candidates. Disabled ones are
    neither reimportable nor skipped — they're invisible to this pass."""
    from cdumm.engine.recovery_candidates import reimport_candidates
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    src = tmp_path / "src"
    src.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()
    _seed_paz_mod(db, name="DisabledMod", enabled=0, source_path=str(src))

    reimportable, skipped = reimport_candidates(db, game_dir)
    assert reimportable == []
    assert skipped == []
    db.close()


def test_asi_plugins_are_not_candidates(tmp_path: Path):
    """ASI plugins don't go through batch reimport. Only mod_type='paz'."""
    from cdumm.engine.recovery_candidates import reimport_candidates
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    src = tmp_path / "src"
    src.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()
    _seed_asi_mod(db, name="SomeAsi", source_path=str(src))

    reimportable, skipped = reimport_candidates(db, game_dir)
    assert reimportable == []
    assert skipped == []
    db.close()


def test_mixed_partition(tmp_path: Path):
    """Four mods: one valid source, one fallback, one orphan, one
    deleted. Partition should be (2 reimportable, 2 skipped)."""
    from cdumm.engine.recovery_candidates import reimport_candidates
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    good = tmp_path / "good"; good.mkdir()
    gone = tmp_path / "gone"  # NOT created

    db = Database(tmp_path / "test.db")
    db.initialize()
    id_valid = _seed_paz_mod(db, name="Valid", source_path=str(good))
    id_fallback = _seed_paz_mod(db, name="Fallback", source_path=None)
    id_orphan = _seed_paz_mod(db, name="Orphan", source_path=None)
    id_stale = _seed_paz_mod(db, name="Stale", source_path=str(gone))

    # Materialise fallback for 'Fallback' only.
    (game_dir / "CDMods" / "sources" / str(id_fallback)).mkdir(parents=True)

    reimportable, skipped = reimport_candidates(db, game_dir)
    assert sorted(m["id"] for m in reimportable) == sorted([id_valid, id_fallback])
    assert sorted(m["id"] for m in skipped) == sorted([id_orphan, id_stale])
    db.close()


def test_disable_mods_updates_multiple_rows_and_commits(tmp_path: Path):
    """Codex finding 1: after reimport skips a set, they MUST be
    disabled before Apply. Helper flips enabled=0 and commits."""
    from cdumm.engine.recovery_candidates import disable_mods
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    id_a = _seed_paz_mod(db, name="A")
    id_b = _seed_paz_mod(db, name="B")
    id_c = _seed_paz_mod(db, name="C")  # stays enabled

    disable_mods(db, [id_a, id_b])

    rows = dict(db.connection.execute(
        "SELECT id, enabled FROM mods").fetchall())
    assert rows[id_a] == 0
    assert rows[id_b] == 0
    assert rows[id_c] == 1
    db.close()


def test_disable_mods_empty_list_is_noop(tmp_path: Path):
    """Empty input → no SQL, no crash."""
    from cdumm.engine.recovery_candidates import disable_mods
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    _seed_paz_mod(db, name="Untouched")

    disable_mods(db, [])  # must not raise

    row = db.connection.execute(
        "SELECT enabled FROM mods").fetchone()
    assert row[0] == 1
    db.close()
