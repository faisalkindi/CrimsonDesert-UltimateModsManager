"""Multi-file Nexus pages: name-match backfill must prefer the file
whose version matches the local copy, not the highest version on the
page.

Bug from Faisal 2026-04-26: Fat Stacks All In One Mod (Nexus 1536)
hosts THREE files at version 1, 1, and 2. The user is on file_id
5037 (version 1). When CDUMM's backfill ran on a row that had no
nexus_real_file_id yet, name_match returned all 3 candidates and
the highest-version pick (file 6111, version 2) got backfilled —
even though the user is actually on file 5037. Subsequent update
checks then walk the chain from 6111 forward, find no successor
(6111 IS the chain head), and report 'up to date' forever — the
red 'click to update' pill never appears for that mod.

Fix: when backfilling, prefer a candidate whose parsed version
exactly matches the local version. Only fall back to the highest-
version pick when no version-match candidate exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

from cdumm.engine.nexus_api import check_mod_updates


@dataclass
class _FakeFile:
    file_id: int
    name: str
    version: str


def _fat_stacks_files(mod_id: int, api_key: str):
    """Mirror Fat Stacks Nexus state — 3 files, two version-1, one
    version-2. file_updates wires 5037 → 6111. Names use the local
    name so name-match deterministically finds all 3."""
    if mod_id != 1536:
        return ([], [])
    files = [
        _FakeFile(file_id=5037,
                  name="Fat Stacks All In One Mod", version="1"),
        _FakeFile(file_id=5268,
                  name="Fat Stacks All In One Mod", version="1"),
        _FakeFile(file_id=6111,
                  name="Fat Stacks All In One Mod", version="2"),
    ]

    @dataclass
    class _FileUpdate:
        old_file_id: int
        new_file_id: int
        old_file_name: str = ""
        new_file_name: str = ""

    updates = [_FileUpdate(old_file_id=5037, new_file_id=6111)]
    return (files, updates)


def test_backfill_prefers_version_matching_file_over_latest(
) -> None:
    """User is on version 1 (file 5037). Backfill must pick 5037,
    NOT the latest file 6111. Otherwise next cycle's chain walk
    starts from 6111 and reports 'current' forever."""
    mods = [
        {"id": 1413, "nexus_mod_id": 1536,
         "name": "Fat Stacks All In One Mod",
         "version": "1", "nexus_real_file_id": 0,
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={1536: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_fat_stacks_files):
        results, _checked, _now, backfill = check_mod_updates(
            mods, "key")

    assert backfill.get(1413) == 5037, (
        f"backfill picked {backfill.get(1413)} for a v1 user — "
        f"should be 5037 (the version-1 file). Picking the "
        f"highest-version file (6111) latches future cycles to "
        f"'current' even though the user is on an older file.")

    # Sanity: the same call should ALSO report has_update=True
    # because the chain walk from 5037 lands on 6111.
    assert results, "expected an update result for the v1 user"
    r = results[0]
    assert getattr(r, "has_update", False) is True, (
        "user on v1 with file 5037 should be reported outdated "
        "(file_updates chain says 5037 → 6111)")


def test_existing_wrong_backfill_self_corrects() -> None:
    """User's nexus_real_file_id is wrongly set to 6111 (latest)
    even though their local version is '1'. The next update check
    must DETECT the version mismatch and re-do name match — backfill
    a corrected file_id (5037) AND report has_update=True so the
    user gets the red pill."""
    mods = [
        {"id": 1413, "nexus_mod_id": 1536,
         "name": "Fat Stacks All In One Mod",
         "version": "1",
         # Intentionally wrong — bug from previous backfill cycle.
         "nexus_real_file_id": 6111,
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={1536: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_fat_stacks_files):
        results, _checked, _now, backfill = check_mod_updates(
            mods, "key")

    assert results, "expected an update result"
    r = results[0]
    assert getattr(r, "has_update", False) is True, (
        "even with wrongly-cached nexus_real_file_id=6111, the "
        "version mismatch (local v1 vs file v2) must trigger a "
        "re-resolve via name match and report outdated.")
    assert backfill.get(1413) == 5037, (
        f"the corrective re-resolve should backfill the right "
        f"file_id (5037, matching local v1). Got "
        f"{backfill.get(1413)}.")
