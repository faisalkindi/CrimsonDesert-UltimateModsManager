"""Failing TDD tests for the cdumm.db torn-write recovery path.

Backstory: unqltango (Nexus, 2026-05-10, CDUMM v3.2.15) reported that
Crimson Desert crashed mid-run. On the next CDUMM launch the manager
demanded a fresh vanilla snapshot, forgot every PAZ mod, and reset the
theme to light. ASI mods (which live in ``bin64/`` rather than the DB)
were untouched. Symptoms point at ``CDMods/cdumm.db`` having been
truncated or otherwise damaged by the crash.

Two failure modes were uncovered while investigating
``cdumm.storage.db_bootstrap.resolve_db_path``:

1. A real, healthy install with only a handful of mods is around
   140 KB on disk — well below the 200 KB ``FRESH_DB_THRESHOLD``.
   The bootstrap therefore classifies it as "empty" and either
   silently overwrites it with a stale AppData backup (when the
   user is a v1.7-migration veteran) or falls through to
   ``Database.initialize()`` with the truncated-but-recoverable
   file, where SQLite happily treats a 0-byte file as a fresh DB.

2. There is no rolling backup of the DB anywhere — once the live
   file is gone, the user's mod list / config is gone too.

These tests describe the DEFENSIVE behaviour we want to ship. They
are EXPECTED TO FAIL today; they pin the contract for the upcoming
fix:

* Every successful bootstrap should leave ``cdumm.db.bak1`` (and
  rotate the prior bak1 into ``cdumm.db.bak2``) next to the live DB.
* When a fresh-looking DB is found and a sibling backup exists with
  real data, the backup wins over both the live file and any legacy
  AppData migration source.

Production fix is intentionally NOT in this commit — see the
investigation report for details.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cdumm.storage.database import Database
from cdumm.storage.db_bootstrap import (
    FRESH_DB_THRESHOLD,
    resolve_db_path,
)


def _seed_real_db(path: Path, *, mods: int = 5, snapshots: int = 50) -> int:
    """Create a populated CDUMM database at ``path`` and return its size.

    The default ``mods=5`` / ``snapshots=50`` mirrors a small but
    legitimate install (a handful of PAZ mods, one snapshot row per
    real game file). On disk this is intentionally LARGER than the
    bare schema but typically still SMALLER than 200 KB — exactly the
    "looks empty, isn't" zone the bootstrap fumbles today.
    """
    db = Database(path)
    db.initialize()
    db.connection.execute(
        "INSERT INTO config (key, value) VALUES ('theme', 'dark')")
    db.connection.execute(
        "INSERT INTO config (key, value) VALUES ('game_directory', 'E:/CD')")
    for i in range(mods):
        db.connection.execute(
            "INSERT INTO mods (name, mod_type, enabled, priority) "
            "VALUES (?, 'paz', 1, ?)", (f"mod{i}", i))
    for i in range(snapshots):
        db.connection.execute(
            "INSERT INTO snapshots (file_path, file_hash, file_size) "
            "VALUES (?, ?, ?)", (f"/path/{i}.paz", "a" * 64, 100_000))
    db.connection.commit()
    db.close()
    return path.stat().st_size


def _truncate(path: Path, n_bytes: int) -> None:
    """Truncate ``path`` to ``n_bytes`` to simulate a torn write."""
    with open(path, "r+b") as fh:
        fh.truncate(n_bytes)


# ── Existing-behaviour pin tests (these PASS today) ────────────────────

def test_resolve_returns_canonical_path_when_db_missing(tmp_path):
    cdmods = tmp_path / "CDMods"
    appdata = tmp_path / "AppData"

    result = resolve_db_path(cdmods, appdata)

    assert result.db_path == cdmods / "cdumm.db"
    assert result.migrated_from is None
    assert cdmods.is_dir()


def test_resolve_pulls_legacy_appdata_db_forward_when_target_missing(tmp_path):
    cdmods = tmp_path / "CDMods"
    appdata = tmp_path / "AppData"
    appdata.mkdir()
    legacy = appdata / "cdumm.db"
    _seed_real_db(legacy, mods=100, snapshots=800)
    assert legacy.stat().st_size > FRESH_DB_THRESHOLD

    result = resolve_db_path(cdmods, appdata)

    assert result.db_path == cdmods / "cdumm.db"
    assert result.migrated_from == legacy
    # Migrated content is byte-identical to the legacy source.
    assert result.db_path.read_bytes() == legacy.read_bytes()


# ── Failing tests that capture the defensive contract ─────────────────

def test_bootstrap_writes_rolling_backup_on_each_startup(tmp_path):
    """After a successful bootstrap there should be a sibling
    ``cdumm.db.bak1`` next to the live DB whose row counts match
    the live DB. SQLite's online ``.backup()`` API creates a
    logically equivalent file with different page-change-counter
    bytes, so byte equality is not a useful contract here.
    """
    cdmods = tmp_path / "CDMods"
    appdata = tmp_path / "AppData"
    cdmods.mkdir()
    live = cdmods / "cdumm.db"
    _seed_real_db(live, mods=10, snapshots=100)

    resolve_db_path(cdmods, appdata)

    bak1 = cdmods / "cdumm.db.bak1"
    assert bak1.exists(), (
        "expected rolling backup cdumm.db.bak1 to be written on bootstrap")
    # Logical equivalence: same row counts.
    bak_conn = sqlite3.connect(bak1)
    try:
        n_mods = bak_conn.execute(
            "SELECT COUNT(*) FROM mods").fetchone()[0]
        n_snap = bak_conn.execute(
            "SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        bak_conn.close()
    assert n_mods == 10
    assert n_snap == 100


def test_truncated_db_is_recovered_from_sibling_backup(tmp_path):
    """The repro of unqltango's report.

    Steps:
    1. Build a healthy CDMods/cdumm.db (5 mods, 50 snapshots, ~140 KB).
    2. Save a known-good rolling backup beside it (cdumm.db.bak1).
    3. Truncate the live DB to 100 bytes (game crash mid-write).
    4. Run the bootstrap — it should restore from .bak1, NOT silently
       hand SQLite a torn file that gets re-initialised as empty.
    """
    cdmods = tmp_path / "CDMods"
    appdata = tmp_path / "AppData"
    cdmods.mkdir()
    live = cdmods / "cdumm.db"
    real_size = _seed_real_db(live, mods=5, snapshots=50)
    assert real_size < FRESH_DB_THRESHOLD, (
        "scenario assumption: a small real DB sits below the migration "
        "threshold; if this fires the threshold needs revisiting")
    good_bytes = live.read_bytes()

    # Rolling backup written on a prior healthy startup.
    bak1 = cdmods / "cdumm.db.bak1"
    bak1.write_bytes(good_bytes)

    # Game crashes mid-write; the live DB is now torn.
    _truncate(live, 100)
    assert live.stat().st_size == 100

    result = resolve_db_path(cdmods, appdata)

    assert result.recovered_from == bak1, (
        "bootstrap should report that recovery used the sibling backup")
    # The live DB has been healed back to the good copy.
    assert live.read_bytes() == good_bytes

    # And it actually opens as a real DB with the user's data intact —
    # config rows, mod rows, snapshot rows, the lot.
    db = Database(result.db_path)
    db.initialize()
    try:
        theme = db.connection.execute(
            "SELECT value FROM config WHERE key = 'theme'").fetchone()
        assert theme == ("dark",)
        n_mods = db.connection.execute(
            "SELECT COUNT(*) FROM mods").fetchone()[0]
        assert n_mods == 5
        n_snapshots = db.connection.execute(
            "SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert n_snapshots == 50
    finally:
        db.close()


def test_small_but_valid_live_db_is_not_clobbered_by_legacy_appdata(tmp_path):
    """A user with a small (< 200 KB) but VALID CDMods/cdumm.db should
    NOT have it silently overwritten by a stale AppData migration source.
    The current size-only heuristic does exactly that.
    """
    cdmods = tmp_path / "CDMods"
    appdata = tmp_path / "AppData"
    cdmods.mkdir()
    appdata.mkdir()

    # Live DB: small but real (a few mods, theme=dark, etc).
    live = cdmods / "cdumm.db"
    _seed_real_db(live, mods=3, snapshots=30)
    live_bytes_before = live.read_bytes()
    assert len(live_bytes_before) < FRESH_DB_THRESHOLD

    # Stale legacy DB still sitting in AppData from a v1.7 migration
    # years ago. Different content, deliberately bigger.
    legacy = appdata / "cdumm.db"
    _seed_real_db(legacy, mods=100, snapshots=800)
    assert legacy.stat().st_size > FRESH_DB_THRESHOLD

    result = resolve_db_path(cdmods, appdata)

    # The live DB must not have been replaced.
    assert result.migrated_from is None, (
        "bootstrap should not migrate over a live DB that is small but "
        "valid; it should validate openability before clobbering")
    assert live.read_bytes() == live_bytes_before


def test_zero_byte_db_does_not_become_a_silent_fresh_install(tmp_path):
    """A 0-byte cdumm.db plus a healthy sibling backup should recover
    the user's data, not be reborn as an empty DB.
    """
    cdmods = tmp_path / "CDMods"
    appdata = tmp_path / "AppData"
    cdmods.mkdir()
    live = cdmods / "cdumm.db"
    _seed_real_db(live, mods=4, snapshots=40)
    good_bytes = live.read_bytes()

    bak1 = cdmods / "cdumm.db.bak1"
    bak1.write_bytes(good_bytes)

    # Crash-induced 0-byte file.
    live.write_bytes(b"")
    assert live.stat().st_size == 0

    result = resolve_db_path(cdmods, appdata)

    assert result.recovered_from == bak1
    db = Database(result.db_path)
    db.initialize()
    try:
        n_mods = db.connection.execute(
            "SELECT COUNT(*) FROM mods").fetchone()[0]
        assert n_mods == 4
    finally:
        db.close()
