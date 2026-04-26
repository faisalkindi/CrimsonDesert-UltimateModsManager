"""Round 3 systematic-debugging fixes for the Nexus update check.

Three bugs found in the third pass:

1. persist_backfill_file_ids returned `persisted` count even after
   commit() failed → caller logged "backfilled N rows" while the
   warning log said the commit failed. Two contradictory log lines
   for the same operation.
2. file_deleted_on_nexus was set on the result correctly, but if
   name-match also returned no candidates, the code 'continue'd
   without appending a result. UI lost the signal — showed
   'unknown' state instead of the special 'deleted' badge.
3. user has nexus_real_file_id pointing at an archived file (not
   deleted, just OLD_VERSION) AND no chain successor exists. Chain
   walk correctly returns None (Fix 2 round-1) and the log says
   'archived', but neither file_deleted_on_nexus nor any other
   signal was set. UI shows generic 'unknown'. Need a distinct
   'archived' signal.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from cdumm.engine.nexus_api import (
    check_mod_updates, persist_backfill_file_ids,
    NexusFileInfo, NexusFileUpdate,
)


@dataclass
class _FakeFile:
    file_id: int
    name: str
    version: str
    category_id: int = 1


# ── Bug 1: persist returns 0 on commit failure ───────────────────────


def test_persist_returns_zero_when_commit_fails(tmp_path: Path) -> None:
    """Commit failure must change the return value so the caller's
    'backfilled N rows' success log doesn't fire alongside the
    warning. Two contradictory log lines for one operation is a
    debugging trap."""

    class _ConnRaisesOnCommit:
        def __init__(self):
            self._real = sqlite3.connect(":memory:")
            self._real.execute(
                "CREATE TABLE mods (id INTEGER PRIMARY KEY, "
                "nexus_real_file_id INTEGER)")
            self._real.execute(
                "INSERT INTO mods (id, nexus_real_file_id) "
                "VALUES (1, 0)")
            self._real.commit()

        def execute(self, *args, **kwargs):
            return self._real.execute(*args, **kwargs)

        def commit(self):
            raise sqlite3.OperationalError("disk full (mock)")

    conn = _ConnRaisesOnCommit()
    persisted = persist_backfill_file_ids(conn, {1: 9001})
    assert persisted == 0, (
        f"commit failure must return 0 — caller relies on the return "
        f"value to decide whether to log success. Got {persisted}.")


def test_persist_rollback_on_commit_failure(tmp_path: Path) -> None:
    """When commit raises, the helper must call rollback to discard
    pending UPDATEs. Otherwise sqlite3 keeps them in the deferred
    transaction and the NEXT successful commit anywhere flushes
    them — return value would lie about persistence.
    Bug from round-4 systematic-debugging review."""
    rollback_called = {"n": 0}

    class _ConnRaisesOnCommit:
        def __init__(self):
            self._real = sqlite3.connect(":memory:")
            self._real.execute(
                "CREATE TABLE mods (id INTEGER PRIMARY KEY, "
                "nexus_real_file_id INTEGER)")
            self._real.execute(
                "INSERT INTO mods (id, nexus_real_file_id) "
                "VALUES (1, 0)")
            self._real.commit()

        def execute(self, *args, **kwargs):
            return self._real.execute(*args, **kwargs)

        def commit(self):
            raise sqlite3.OperationalError("locked")

        def rollback(self):
            rollback_called["n"] += 1
            self._real.rollback()

    conn = _ConnRaisesOnCommit()
    persist_backfill_file_ids(conn, {1: 9001})
    assert rollback_called["n"] == 1, (
        f"commit failure must trigger rollback to discard pending "
        f"UPDATEs. Got rollback_called={rollback_called['n']}.")


def test_persist_returns_count_on_success(tmp_path: Path) -> None:
    """Sanity: when commit succeeds, return value still reflects
    actual update count."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, "
        "nexus_real_file_id INTEGER)")
    conn.execute("INSERT INTO mods (id, nexus_real_file_id) "
                 "VALUES (1, 0), (2, 0)")
    conn.commit()
    persisted = persist_backfill_file_ids(conn, {1: 100, 2: 200})
    assert persisted == 2


# ── Bug 2: deleted-on-nexus signal preserved when name-match fails ───


def test_deleted_signal_emitted_even_when_name_match_fails() -> None:
    """User's file_id is deleted from Nexus AND name-match also fails
    (mod renamed completely). The result entry MUST still be
    emitted with file_deleted_on_nexus=True so the UI can render
    a 'source removed' badge instead of generic 'unknown'."""
    mods = [
        {"id": 50, "nexus_mod_id": 555, "name": "Old Mod Name",
         "version": "1.0",
         "nexus_real_file_id": 4000,  # deleted from Nexus
         "nexus_last_checked_at": 0},
    ]

    def _mod_files(mod_id, api_key):
        if mod_id != 555:
            return ([], [])
        # User's file 4000 is gone. Two unrelated files remain, BOTH
        # with totally different names — name-match fails (no exact
        # match, token overlap < 0.6, 2 files so no single-file
        # fallback).
        return ([_FakeFile(file_id=9999,
                            name="Completely Different Thing",
                            version="2.0"),
                 _FakeFile(file_id=9998,
                            name="Yet Another Unrelated Project",
                            version="3.0")], [])

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={555: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mod_files):
        results, _checked, _now, _backfill = check_mod_updates(
            mods, "key")

    # The result must be emitted with the deleted signal preserved
    matching = [r for r in results if r.mod_id == 555]
    assert matching, (
        "deleted-file case where name-match also fails must STILL "
        "emit a result entry — otherwise the UI loses the deleted "
        "signal and shows 'unknown'.")
    r = matching[0]
    assert r.file_deleted_on_nexus is True, (
        "the deleted signal must reach the UI even on name-match "
        "failure")


# ── Bug 3: archived-file-no-successor case gets a distinct signal ────


def test_archived_chain_head_no_successor_signals_unavailable(
) -> None:
    """User has nexus_real_file_id=5037 which still EXISTS on Nexus
    but is marked OLD_VERSION (category_id=4) with no chain
    successor. Need a signal so the UI knows the file is deprecated
    even if no replacement exists."""
    mods = [
        {"id": 60, "nexus_mod_id": 200, "name": "Old Mod",
         "version": "1",
         "nexus_real_file_id": 5037,
         "nexus_last_checked_at": 0},
    ]

    def _mod_files(mod_id, api_key):
        if mod_id != 200:
            return ([], [])
        # 5037 EXISTS but is OLD_VERSION. No chain successor.
        # No other MAIN file matches the local mod's name.
        return ([
            _FakeFile(file_id=5037, name="Old Mod", version="1",
                       category_id=4),  # OLD_VERSION
            _FakeFile(file_id=8888, name="Different Other Mod",
                       version="3", category_id=1),  # different mod
        ], [])

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={200: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mod_files):
        results, _checked, _now, _backfill = check_mod_updates(
            mods, "key")

    matching = [r for r in results if r.mod_id == 200]
    assert matching, (
        "archived-no-successor case must emit a result so the UI "
        "isn't left blank")
    r = matching[0]
    # Either file_deleted_on_nexus or a new file_archived_on_nexus
    # field must be True (we don't care which spelling — the SIGNAL
    # is the contract)
    has_signal = (
        getattr(r, "file_deleted_on_nexus", False)
        or getattr(r, "file_archived_on_nexus", False)
    )
    assert has_signal, (
        "user is on an archived file with no Nexus successor — "
        "either file_deleted_on_nexus or file_archived_on_nexus "
        "must be True so the UI can render a distinct badge")
