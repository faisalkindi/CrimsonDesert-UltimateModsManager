"""Five fixes from the systematic-debugging review of the Nexus update
check code, 2026-04-26.

CRITICAL: persistence layer blocks self-correction overwrites
HIGH:     _resolve_latest_file returns archived chain heads as 'current'
HIGH:     self-correction over-fires when local file_id IS the chain head
MEDIUM:   version parser collapses '1.0.0a' with '1.0.0'
MEDIUM:   chain dict drops duplicate old_file_id entries non-deterministically
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from cdumm.engine.nexus_api import (
    _resolve_latest_file, _version_to_tuple,
    check_mod_updates, persist_backfill_file_ids,
    NexusFileInfo, NexusFileUpdate,
)


@dataclass
class _FakeFile:
    file_id: int
    name: str
    version: str
    category_id: int = 1  # MAIN by default


# ── CRITICAL: persistence allows overwrite when values differ ────────


def _make_db_with_one_mod(tmp_path: Path,
                           initial_real_file_id: int) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, "
        "nexus_real_file_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO mods (id, name, nexus_real_file_id) VALUES "
        "(1413, 'Fat Stacks', ?)", (initial_real_file_id,))
    conn.commit()
    return conn


def test_persist_overwrites_existing_wrong_value(tmp_path: Path) -> None:
    """Self-correction returned a corrected file_id 5037, but DB still
    has the wrong 6111. Persistence MUST overwrite — engine-level dedup
    already guards against same-value rewrites."""
    conn = _make_db_with_one_mod(tmp_path, initial_real_file_id=6111)
    persisted = persist_backfill_file_ids(conn, {1413: 5037})
    assert persisted == 1, f"expected 1 row updated, got {persisted}"
    cur = conn.execute(
        "SELECT nexus_real_file_id FROM mods WHERE id=1413")
    assert cur.fetchone()[0] == 5037, (
        "persistence failed to overwrite wrong value 6111 with "
        "corrected value 5037 — self-correction stuck in a loop")


def test_persist_skips_same_value_rewrites(tmp_path: Path) -> None:
    """When the backfill value equals the existing value, don't waste
    an UPDATE call (defensive — engine-level dedup already filters)."""
    conn = _make_db_with_one_mod(tmp_path, initial_real_file_id=6111)
    persisted = persist_backfill_file_ids(conn, {1413: 6111})
    assert persisted == 0, (
        f"persisting same value should not count as an update; "
        f"got {persisted}")


def test_persist_handles_null_existing(tmp_path: Path) -> None:
    """Rows with NULL nexus_real_file_id (never backfilled before)
    should be filled."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
        "nexus_real_file_id INTEGER)")
    conn.execute("INSERT INTO mods (id, name) VALUES (10, 'NewMod')")
    conn.commit()
    persisted = persist_backfill_file_ids(conn, {10: 9001})
    assert persisted == 1
    cur = conn.execute(
        "SELECT nexus_real_file_id FROM mods WHERE id=10")
    assert cur.fetchone()[0] == 9001


# ── HIGH: archived chain head → fall back to name match ──────────────


def test_resolve_latest_skips_archived_chain_head() -> None:
    """When the file_updates chain ends at an OLD_VERSION/ARCHIVED file
    (author archived without updating chain), don't return it as the
    'latest' — caller falls back to name match."""
    files = [
        NexusFileInfo(file_id=5037, name="Mod", version="1",
                      uploaded_timestamp=1, file_name="m1.zip",
                      category_id=1),
        # Chain head, but archived
        NexusFileInfo(file_id=6111, name="Mod", version="2",
                      uploaded_timestamp=2, file_name="m2.zip",
                      category_id=4),  # OLD_VERSION
    ]
    updates = [NexusFileUpdate(old_file_id=5037, new_file_id=6111)]
    result = _resolve_latest_file(5037, files, updates)
    assert result is None, (
        "chain walk landed on file 6111 which is archived "
        f"(category_id=4) — should return None so caller falls back "
        f"to name match. Got: {result}")


def test_resolve_latest_returns_main_chain_head() -> None:
    """Sanity: when chain head IS a MAIN file, return it normally."""
    files = [
        NexusFileInfo(file_id=5037, name="Mod", version="1",
                      uploaded_timestamp=1, file_name="m1.zip",
                      category_id=1),
        NexusFileInfo(file_id=6111, name="Mod", version="2",
                      uploaded_timestamp=2, file_name="m2.zip",
                      category_id=1),
    ]
    updates = [NexusFileUpdate(old_file_id=5037, new_file_id=6111)]
    result = _resolve_latest_file(5037, files, updates)
    assert result is not None and result.file_id == 6111


# ── HIGH: self-correction doesn't fire when user IS chain head ───────


def _files_user_on_latest_with_stale_local_ver(
        mod_id: int, api_key: str):
    """User is on file 7000 (the latest chain head). Their local
    version is '1' (stale metadata, parsed from old filename). The
    Nexus file 7000 has version '2'. Versions disagree but the user
    IS on the latest — self-correction should NOT fire."""
    if mod_id != 100:
        return ([], [])
    files = [
        _FakeFile(file_id=7000, name="Stale Mod", version="2"),
    ]
    updates = []  # No file_updates — 7000 IS the chain head
    return (files, updates)


def test_self_correction_does_not_fire_when_user_is_chain_head(
) -> None:
    """User has nexus_real_file_id=7000. Chain walk returns 7000
    (no entry from 7000). Local version '1' vs file version '2'
    disagree, BUT 7000 has no successor declared. The disagreement
    is metadata drift, not a wrong file_id. Self-correction must
    NOT fire — has_update should be False."""
    mods = [
        {"id": 50, "nexus_mod_id": 100, "name": "Stale Mod",
         "version": "1",
         "nexus_real_file_id": 7000,
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={100: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_files_user_on_latest_with_stale_local_ver):
        results, _checked, _now, backfill = check_mod_updates(
            mods, "key")

    assert results
    r = results[0]
    assert r.has_update is False, (
        "user is on the chain head (no successor declared) — version "
        "drift is metadata, not an update. Forcing red pill here is "
        "a false positive that triggers a wasteful re-download.")
    # Also: backfill should not queue an unnecessary write
    assert 50 not in backfill


# ── MEDIUM: version parser handles letter-suffix hotfixes ────────────


def test_version_letter_suffix_is_greater_than_base() -> None:
    """'1.0.0a' is conventionally a hotfix of '1.0.0'. Parser must
    NOT collapse them to equal — the hotfix should sort strictly
    greater so the user gets the red pill."""
    base = _version_to_tuple("1.0.0")
    hotfix = _version_to_tuple("1.0.0a")
    assert base is not None
    assert hotfix is not None
    assert hotfix > base, (
        f"'1.0.0a' should be > '1.0.0' (hotfix). "
        f"got base={base}, hotfix={hotfix}")


def test_version_letter_suffix_less_than_next_patch() -> None:
    """'1.0.0a' < '1.0.1' — hotfix to 1.0.0 is below the next patch."""
    hotfix = _version_to_tuple("1.0.0a")
    next_patch = _version_to_tuple("1.0.1")
    assert hotfix is not None
    assert next_patch is not None
    assert hotfix < next_patch, (
        f"'1.0.0a' should be < '1.0.1'. "
        f"got hotfix={hotfix}, next_patch={next_patch}")


def test_version_double_letter_suffix_compares_alphabetically() -> None:
    """'1.0.0b' > '1.0.0a' — second hotfix beats first."""
    a = _version_to_tuple("1.0.0a")
    b = _version_to_tuple("1.0.0b")
    assert a is not None and b is not None
    assert b > a, f"'1.0.0b' should be > '1.0.0a'. got a={a}, b={b}"


# ── MEDIUM: chain dict resolves duplicate old_file_id by recency ─────


def test_chain_with_duplicate_old_file_id_picks_most_recent() -> None:
    """Author re-linked file 5037 → 7000 after originally linking
    5037 → 6000. The walk must follow the most recent re-link, not
    silently pick whichever entry happened to come last in iteration."""
    files = [
        NexusFileInfo(file_id=5037, name="M", version="1",
                      uploaded_timestamp=1, file_name="m1.zip"),
        NexusFileInfo(file_id=6000, name="M", version="2",
                      uploaded_timestamp=2, file_name="m2.zip",
                      category_id=4),  # archived intermediate
        NexusFileInfo(file_id=7000, name="M", version="3",
                      uploaded_timestamp=3, file_name="m3.zip"),
    ]
    # Two entries with the same old_file_id. The 7000 link is newer
    # (uploaded_timestamp=10 vs 5).
    updates = [
        NexusFileUpdate(old_file_id=5037, new_file_id=6000,
                        uploaded_timestamp=5),
        NexusFileUpdate(old_file_id=5037, new_file_id=7000,
                        uploaded_timestamp=10),
    ]
    result = _resolve_latest_file(5037, files, updates)
    assert result is not None
    assert result.file_id == 7000, (
        f"Two file_updates entries for same old_file_id: must pick "
        f"the most recent (7000), not whichever came last in list "
        f"order. Got file_id={result.file_id}")
