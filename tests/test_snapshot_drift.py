"""Drift trigger: detect when game files were changed outside CDUMM.

Covers `detect_snapshot_drift` — the second Recovery Flow trigger
that complements the Steam-buildid fingerprint check. The buildid
catches normal Steam patches; this catches manual edits, antivirus
rewrites, half-finished Steam Verify runs that didn't bump the
buildid, and any other path where the live disk drifted from CDUMM's
snapshot without going through Apply.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.snapshot_manager import detect_snapshot_drift
from cdumm.storage.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    yield database
    database.close()


def _seed_snapshot(db, rows):
    """Insert (file_path, file_size) into the snapshots table."""
    for path, size in rows:
        db.connection.execute(
            "INSERT INTO snapshots (file_path, file_hash, file_size) "
            "VALUES (?, ?, ?)",
            (path, "x" * 32, size),
        )
    db.connection.commit()


def _make_live_file(game_dir: Path, rel: str, size: int) -> None:
    p = game_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"\0" * size)


def test_no_drift_when_live_sizes_match_snapshot(db, tmp_path: Path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    _seed_snapshot(db, [("0000/0.paz", 100), ("0001/0.pamt", 50)])
    _make_live_file(game_dir, "0000/0.paz", 100)
    _make_live_file(game_dir, "0001/0.pamt", 50)

    drift, mismatches = detect_snapshot_drift(db, game_dir)
    assert drift is False
    assert mismatches == []


def test_drift_detected_on_size_mismatch(db, tmp_path: Path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    _seed_snapshot(db, [("0000/0.paz", 100)])
    _make_live_file(game_dir, "0000/0.paz", 999)  # tampered

    drift, mismatches = detect_snapshot_drift(db, game_dir)
    assert drift is True
    assert "0000/0.paz" in mismatches


def test_no_drift_when_mod_is_applied_to_that_file(db, tmp_path: Path):
    """Applied mods *legitimately* change file sizes. The drift check
    must skip files referenced by mod_deltas where the mod is
    applied=1 — otherwise every applied mod looks like drift."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    _seed_snapshot(db, [("0000/0.paz", 100), ("0000/1.paz", 200)])
    _make_live_file(game_dir, "0000/0.paz", 555)  # mod-modified
    _make_live_file(game_dir, "0000/1.paz", 200)  # untouched

    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, applied) "
        "VALUES (?, ?, 1, 1)",
        ("ModA", "paz"))
    mod_id = db.connection.execute(
        "SELECT id FROM mods WHERE name = ?", ("ModA",)
    ).fetchone()[0]
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, byte_start, byte_end, "
        "delta_path) VALUES (?, ?, ?, ?, ?)",
        (mod_id, "0000/0.paz", 0, 100, "/tmp/delta"))
    db.connection.commit()

    drift, mismatches = detect_snapshot_drift(db, game_dir)
    assert drift is False, mismatches


def test_drift_detected_when_mod_delta_belongs_to_disabled_mod(db, tmp_path: Path):
    """Only `applied=1` mods exempt their files from drift checking.
    A mod that's disabled (or just-enabled-but-not-yet-applied) does
    NOT excuse a size change on its target file."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    _seed_snapshot(db, [("0000/0.paz", 100)])
    _make_live_file(game_dir, "0000/0.paz", 555)

    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, applied) "
        "VALUES (?, ?, 1, 0)",
        ("ModA", "paz"))
    mod_id = db.connection.execute(
        "SELECT id FROM mods WHERE name = ?", ("ModA",)
    ).fetchone()[0]
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, byte_start, byte_end, "
        "delta_path) VALUES (?, ?, ?, ?, ?)",
        (mod_id, "0000/0.paz", 0, 100, "/tmp/delta"))
    db.connection.commit()

    drift, mismatches = detect_snapshot_drift(db, game_dir)
    assert drift is True
    assert "0000/0.paz" in mismatches


def test_no_drift_on_empty_snapshots_table(db, tmp_path: Path):
    """First-time install: nothing to drift from. Don't false-fire."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    drift, mismatches = detect_snapshot_drift(db, game_dir)
    assert drift is False
    assert mismatches == []


def test_missing_live_file_is_not_flagged_as_drift(db, tmp_path: Path):
    """Renamed/removed PAZ files (rare but possible across game
    versions) shouldn't trip the trigger — that's a different
    diagnostic, not 'someone tampered with our snapshot'."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    _seed_snapshot(db, [("0099/missing.paz", 500)])
    drift, mismatches = detect_snapshot_drift(db, game_dir)
    assert drift is False


def test_max_reported_caps_the_mismatch_list(db, tmp_path: Path):
    """Don't return a 200-entry list when 5 is enough to call it drift."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    rows = [(f"0000/{i}.paz", 100) for i in range(50)]
    _seed_snapshot(db, rows)
    for path, _ in rows:
        _make_live_file(game_dir, path, 999)

    drift, mismatches = detect_snapshot_drift(db, game_dir, max_reported=5)
    assert drift is True
    assert len(mismatches) == 5
