"""Round 2 systematic-debugging fixes for the Nexus update check.

Bugs found in second-pass review (2026-04-26) that were NEW issues
introduced by the round-1 fixes, plus latent bugs that survived:

1. Chain duplicate-key tiebreak regressed when uploaded_timestamp
   was missing/zero on all entries — last in iteration won, same
   as the original buggy behavior we tried to fix.
2. Log message says 'file appears deleted' even when the chain
   simply landed on an archived file. Misleading.
3. persist_backfill_file_ids silently swallowed all commit errors
   with bare `except Exception: pass` — disk-full, locked-DB,
   transaction rollback all reported as success.
4. nexus_mods filter uses Python truthiness on nexus_mod_id, so
   the string '0' (corrupted DB row) passes through and triggers
   a wasted /mods/0/files.json API call every cycle.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from cdumm.engine.nexus_api import (
    _resolve_latest_file, check_mod_updates,
    persist_backfill_file_ids,
    NexusFileInfo, NexusFileUpdate,
)


@dataclass
class _FakeFile:
    file_id: int
    name: str
    version: str
    category_id: int = 1


# ── Bug 1: Chain tiebreak with zero/missing timestamps ───────────────


def test_chain_duplicate_key_higher_new_id_wins_when_ts_tied(
) -> None:
    """When two file_updates entries share old_file_id AND have the
    same uploaded_timestamp (or both 0), the one with the higher
    new_file_id must win deterministically. Otherwise the last-
    iterated entry wins, which is non-deterministic across Nexus
    API responses."""
    files = [
        NexusFileInfo(file_id=5037, name="M", version="1",
                      uploaded_timestamp=0, file_name="m1.zip"),
        NexusFileInfo(file_id=6000, name="M", version="2",
                      uploaded_timestamp=0, file_name="m2.zip"),
        NexusFileInfo(file_id=7000, name="M", version="3",
                      uploaded_timestamp=0, file_name="m3.zip"),
    ]
    # Two entries, both with uploaded_timestamp=0 (the API may not
    # always populate this field). Iteration order is 5037→6000
    # FIRST, then 5037→7000. Naive tiebreak picks last (7000).
    # Reverse iteration order to verify we don't depend on it.
    updates_a = [
        NexusFileUpdate(old_file_id=5037, new_file_id=6000,
                        uploaded_timestamp=0),
        NexusFileUpdate(old_file_id=5037, new_file_id=7000,
                        uploaded_timestamp=0),
    ]
    updates_b = list(reversed(updates_a))
    result_a = _resolve_latest_file(5037, files, updates_a)
    result_b = _resolve_latest_file(5037, files, updates_b)
    assert result_a is not None and result_b is not None
    assert result_a.file_id == 7000, (
        f"forward order should pick higher new_id (7000); got "
        f"{result_a.file_id}")
    assert result_b.file_id == 7000, (
        f"reverse order MUST also pick 7000 — otherwise we're "
        f"non-deterministic across API responses; got {result_b.file_id}")


# ── Bug 2: distinguish 'deleted' from 'archived' in logs ─────────────


def test_log_says_deleted_only_when_file_truly_missing(
        caplog) -> None:
    """When the user's local_file_id is genuinely absent from the
    Nexus file list, log says 'deleted'. When chain walked TO an
    archived file (file still exists, just OLD_VERSION), log says
    'archived', not 'deleted'."""
    import logging
    caplog.set_level(logging.INFO, logger="cdumm.engine.nexus_api")
    # Case A: user's file genuinely missing.
    files_truly_deleted = [
        _FakeFile(file_id=9999, name="Other File", version="1"),
    ]
    mods_a = [
        {"id": 1, "nexus_mod_id": 100, "name": "M",
         "version": "1", "nexus_real_file_id": 5037,
         "nexus_last_checked_at": 0},
    ]

    def _files_a(mod_id, api_key):
        return (files_truly_deleted, [])

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={100: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_files_a):
        check_mod_updates(mods_a, "key")
    deleted_logs = [
        r for r in caplog.records
        if "appears deleted" in r.getMessage()
    ]
    assert deleted_logs, (
        "case A (file truly missing from Nexus) should emit a "
        "'file appears deleted' log line")
    caplog.clear()

    # Case B: chain walks to an archived file — file STILL EXISTS,
    # just marked OLD_VERSION. Log must NOT say 'deleted'.
    files_archived = [
        _FakeFile(file_id=5037, name="M", version="1",
                  category_id=1),
        _FakeFile(file_id=6111, name="M", version="2",
                  category_id=4),  # OLD_VERSION
    ]
    updates = [NexusFileUpdate(
        old_file_id=5037, new_file_id=6111)]
    mods_b = [
        {"id": 1, "nexus_mod_id": 100, "name": "M",
         "version": "1", "nexus_real_file_id": 5037,
         "nexus_last_checked_at": 0},
    ]

    def _files_b(mod_id, api_key):
        return (files_archived, updates)

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={100: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_files_b):
        check_mod_updates(mods_b, "key")
    deleted_logs_b = [
        r for r in caplog.records
        if "appears deleted" in r.getMessage()
    ]
    archived_logs_b = [
        r for r in caplog.records
        if "archived" in r.getMessage().lower()
    ]
    assert not deleted_logs_b, (
        "case B (chain walked to archived) must NOT log 'appears "
        "deleted' — the file still exists, just marked OLD_VERSION. "
        f"Got logs:\n" + "\n".join(
            r.getMessage() for r in caplog.records))
    assert archived_logs_b, (
        "case B should log 'archived' to point investigation at the "
        "right cause")


# ── Bug 3: persist_backfill_file_ids logs commit failures ────────────


def test_persist_logs_commit_failure(tmp_path: Path, caplog) -> None:
    """A commit() that raises must surface in logs at warning level,
    not get silently swallowed. Otherwise a failed persistence is
    indistinguishable from a successful one."""
    import logging
    caplog.set_level(logging.WARNING, logger="cdumm.engine.nexus_api")

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
            raise sqlite3.OperationalError("disk I/O error (mock)")

    conn = _ConnRaisesOnCommit()
    persist_backfill_file_ids(conn, {1: 9001})
    warned = [
        r for r in caplog.records
        if "commit" in r.getMessage().lower()
    ]
    assert warned, (
        "commit failure must produce a WARNING log so silent "
        "data-loss is detectable. Got log records:\n"
        + "\n".join(f"  [{r.levelname}] {r.getMessage()}"
                    for r in caplog.records))


# ── Bug 4: nexus_mod_id string '0' should be filtered out ────────────


def test_nexus_mod_id_string_zero_is_filtered() -> None:
    """A corrupted DB row with nexus_mod_id stored as string '0'
    must NOT trigger a wasted /mods/0/files.json API call. The
    mods filter should coerce + validate."""
    mods = [
        {"id": 1, "nexus_mod_id": "0", "name": "Bad Row",
         "version": "1", "nexus_real_file_id": 0,
         "nexus_last_checked_at": 0},
        {"id": 2, "nexus_mod_id": 100, "name": "Good Mod",
         "version": "1", "nexus_real_file_id": 0,
         "nexus_last_checked_at": 0},
    ]
    api_calls = []

    def _track(mod_id, api_key):
        api_calls.append(mod_id)
        return ([_FakeFile(file_id=1, name="Good Mod", version="1")],
                [])

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={100: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_track):
        check_mod_updates(mods, "key")

    assert 0 not in api_calls and "0" not in api_calls, (
        f"mods with nexus_mod_id=0 (or '0') must be filtered out "
        f"BEFORE the API call. Got api_calls={api_calls}")
    assert 100 in api_calls, (
        "valid mods should still be checked normally")


def test_nexus_mod_id_int_zero_is_filtered() -> None:
    """Integer 0 already filtered (Python falsy), but lock the
    behavior in a test."""
    mods = [
        {"id": 1, "nexus_mod_id": 0, "name": "Bad",
         "version": "1", "nexus_real_file_id": 0,
         "nexus_last_checked_at": 0},
    ]
    api_calls = []

    def _track(mod_id, api_key):
        api_calls.append(mod_id)
        return ([], [])

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_track):
        check_mod_updates(mods, "key")

    assert api_calls == [], (
        f"int 0 nexus_mod_id must not trigger API call; got "
        f"{api_calls}")
