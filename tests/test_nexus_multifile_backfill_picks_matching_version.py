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


# ── Issue 1: don't rewrite wrong backfill with same wrong value ───────


def _archived_v1_files(mod_id: int, api_key: str):
    """Real-world Fat Stacks state: v1 files are archived (excluded
    by name-match), only v2 MAIN remains visible."""
    if mod_id != 1536:
        return ([], [])
    files = [
        _FakeFile(file_id=5037,
                  name="Fat Stacks All In One Mod", version="1"),
        _FakeFile(file_id=6111,
                  name="Fat Stacks All In One Mod", version="2"),
    ]
    # Mark 5037 as OLD_VERSION (category_id=4) — gets excluded
    files[0].__dict__["category_id"] = 4

    @dataclass
    class _FU:
        old_file_id: int
        new_file_id: int
        old_file_name: str = ""
        new_file_name: str = ""

    updates = [_FU(old_file_id=5037, new_file_id=6111)]
    return (files, updates)


def test_self_correction_does_not_rewrite_same_wrong_value() -> None:
    """When self-correction fires AND the only available backfill
    target is the same wrong value already stored, don't add it to
    backfill_file_ids. Otherwise the DB gets the same wrong value
    rewritten on every check cycle."""
    mods = [
        {"id": 1413, "nexus_mod_id": 1536,
         "name": "Fat Stacks All In One Mod",
         "version": "1",
         "nexus_real_file_id": 6111,  # already wrong
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={1536: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_archived_v1_files):
        results, _checked, _now, backfill = check_mod_updates(
            mods, "key")

    # Has-update path still works
    assert results
    assert results[0].has_update is True
    # But we didn't queue another rewrite of the same wrong value
    assert 1413 not in backfill, (
        f"backfill should NOT rewrite nexus_real_file_id with the "
        f"same wrong value (6111). Got: {backfill}")


# ── Issue 2: self-correct even when local version doesn't parse ──────


def _files_v_unparseable(mod_id: int, api_key: str):
    if mod_id != 999:
        return ([], [])
    files = [
        _FakeFile(file_id=7000, name="Wonky Mod", version="alpha"),
        _FakeFile(file_id=7001, name="Wonky Mod", version="beta"),
    ]

    @dataclass
    class _FU:
        old_file_id: int
        new_file_id: int
        old_file_name: str = ""
        new_file_name: str = ""

    return (files, [_FU(old_file_id=7000, new_file_id=7001)])


def test_self_correction_fires_when_local_version_unparseable() -> None:
    """Local version 'alpha' doesn't parse to a tuple. But latest's
    version 'beta' is clearly different. The self-correction must
    fire on string-difference too, not only on tuple-difference."""
    mods = [
        {"id": 99, "nexus_mod_id": 999, "name": "Wonky Mod",
         "version": "alpha",
         "nexus_real_file_id": 7001,  # wrongly latched on latest
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={999: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_files_v_unparseable):
        results, _checked, _now, _backfill = check_mod_updates(
            mods, "key")

    assert results
    assert results[0].has_update is True, (
        "self-correction must fire for unparseable-but-different "
        "version strings too — local 'alpha' vs file 'beta' are "
        "clearly distinct even when the parser rejects them.")


# ── Issue 3: skip recent checks even when feed fetch fails ───────────


def test_recent_check_skipped_even_when_feed_unavailable() -> None:
    """When get_recently_updated returns None (feed call failed),
    the per-mod skip-if-checked-recently optimisation must STILL
    apply. Otherwise a user with 50 mods triggers 50 sequential
    API calls in one cycle → rate limit."""
    import time
    recent_check = int(time.time()) - 3600  # 1 hour ago, well within week
    mods = [
        {"id": i, "nexus_mod_id": 1000 + i, "name": f"Mod {i}",
         "version": "1.0", "nexus_real_file_id": 100 + i,
         "nexus_last_checked_at": recent_check}
        for i in range(5)
    ]
    call_count = {"n": 0}

    def _track_calls(mod_id, api_key):
        call_count["n"] += 1
        return ([_FakeFile(file_id=100 + (mod_id - 1000),
                            name=f"Mod {mod_id - 1000}",
                            version="1.0")], [])

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value=None), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_track_calls):
        check_mod_updates(mods, "key")

    assert call_count["n"] == 0, (
        f"feed unavailable + recently-checked mods should NOT hit "
        f"the per-mod API. Got {call_count['n']} calls (expected 0).")


# ── Issue 4: deleted-on-nexus signalled in the result ────────────────


def _user_file_deleted(mod_id: int, api_key: str):
    """User had file 4000, but Nexus now only has 4001 (other file)."""
    if mod_id != 555:
        return ([], [])
    return ([_FakeFile(file_id=4001, name="Some Other Mod",
                        version="2.0")], [])


def test_user_file_deleted_on_nexus_is_flagged_in_result() -> None:
    """When the user's nexus_real_file_id is no longer present in
    the Nexus file list (author deleted it, or mod taken down),
    the result must carry a clear signal so the UI can render
    differently than 'unknown'."""
    mods = [
        {"id": 50, "nexus_mod_id": 555, "name": "Some Mod",
         "version": "1.0", "nexus_real_file_id": 4000,
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={555: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_user_file_deleted):
        results, _checked, _now, _backfill = check_mod_updates(
            mods, "key")

    # Either the result is emitted with a "deleted" marker, or
    # explicitly absent. Spec: emit a result with file_deleted=True.
    matching = [r for r in results if getattr(r, "mod_id", 0) == 555]
    assert matching, (
        "deleted-file case should still emit a result so the UI "
        "knows the mod was checked (just not normally).")
    assert getattr(matching[0], "file_deleted_on_nexus", False) is True, (
        "result must carry file_deleted_on_nexus=True for the "
        "GUI to render a 'source removed' badge instead of "
        "leaving the mod looking unknown.")
