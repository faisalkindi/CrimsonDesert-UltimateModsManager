"""_next_paz_directory must consider directory numbers already claimed
by mods in the database, not just the filesystem and the in-memory
_assigned_dirs set.

Without this, the following sequence collides:
1. Import mod A (gets 0036) — recorded in mod_deltas as
   file_path='0036/0.paz' but NOT yet applied to disk.
2. Worker process exits (e.g. user imported in batch 1, closed CDUMM).
3. User reopens CDUMM, imports mod B in a fresh worker process.
   _assigned_dirs is empty (new process), game_dir has no 0036/
   yet (Apply hasn't run). _next_paz_directory returns 0036 again.
4. mod B's mod_deltas record file_path='0036/0.paz' too.
5. On Apply, second mod's archive overwrites the first's.

Surfaced by GitHub #59 (DoRoon, 2026-05-01) — same symptom as the
truly-new-dir collision, but reachable even after that fix when
imports cross process boundaries.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def test_next_paz_directory_skips_dirs_already_in_db(tmp_path: Path):
    """_next_paz_directory must skip dir numbers used by any
    mod_deltas.file_path even if no on-disk dir or _assigned_dirs
    entry exists yet."""
    from cdumm.engine.import_handler import (
        _next_paz_directory, clear_assigned_dirs)
    from cdumm.storage.database import Database

    clear_assigned_dirs()

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "0030").mkdir()  # arbitrary vanilla-ish dir

    # Simulate: a previous import claimed dir 0036 in the DB but
    # hasn't been applied to disk yet (game_dir/0036/ doesn't exist).
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) "
        "VALUES (1, 'PriorMod', 'paz', 1)"
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end, is_new) "
        "VALUES (1, '0036/0.paz', 'dummy', 0, 100, 1)"
    )
    db.connection.commit()

    # Fresh process simulation — _assigned_dirs is empty, game_dir
    # has no 0036/. Without DB awareness, this returns "0036".
    assigned = _next_paz_directory(game_dir, db=db)

    assert assigned != "0036", (
        f"_next_paz_directory returned {assigned!r} but mod_deltas "
        f"already claims 0036 in the DB. The next import will collide "
        f"with the prior unprapplied mod on Apply."
    )
    assert assigned == "0037", (
        f"Expected next free dir to be 0037 (skipping DB-claimed 0036), "
        f"got {assigned!r}"
    )
    db.close()


def test_next_paz_directory_works_without_db_arg(tmp_path: Path):
    """Backward compat: callers that don't pass `db` still get the
    filesystem+_assigned_dirs behavior (no DB lookup)."""
    from cdumm.engine.import_handler import (
        _next_paz_directory, clear_assigned_dirs)

    clear_assigned_dirs()

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "0030").mkdir()

    assigned = _next_paz_directory(game_dir)
    assert assigned.isdigit() and len(assigned) == 4
    assert int(assigned) >= 36


def test_next_paz_directory_skips_multiple_db_dirs(tmp_path: Path):
    """If the DB has several claimed dir numbers, all must be skipped."""
    from cdumm.engine.import_handler import (
        _next_paz_directory, clear_assigned_dirs)
    from cdumm.storage.database import Database

    clear_assigned_dirs()

    game_dir = tmp_path / "game"
    game_dir.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) "
        "VALUES (1, 'A', 'paz', 1)"
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end, is_new) "
        "VALUES (1, '0036/0.paz', 'da', 0, 1, 1)"
    )
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) "
        "VALUES (2, 'B', 'paz', 1)"
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end, is_new) "
        "VALUES (2, '0037/0.paz', 'db', 0, 1, 1)"
    )
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) "
        "VALUES (3, 'C', 'paz', 1)"
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end, is_new) "
        "VALUES (3, '0039/0.paz', 'dc', 0, 1, 1)"
    )
    db.connection.commit()

    # 0036, 0037, 0039 are taken. Next free is 0038.
    assigned = _next_paz_directory(game_dir, db=db)
    assert assigned == "0038", (
        f"Expected 0038 (skipping 0036, 0037, 0039), got {assigned!r}"
    )
    db.close()
