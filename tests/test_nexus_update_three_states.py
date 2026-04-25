"""Bug #1: Green pills lie.

Evidence: ``mods_page.set_nexus_updates`` paints a green "up to date" pill
for any mod card that has a ``nexus_mod_id`` and isn't in the ``updates``
dict. But ``check_mod_updates`` silently skips mods where the name match
failed, where the API call transiently errored, or where the feed-skip
heuristic fired with a recent last-check. None of those are "confirmed
current" — they're "unknown" — and painting them green misleads the user.

Real-world case from the 2026-04-22 log:

    update check: no Nexus file matches local mod 'Berserk The Dragon
    Slayer' (nexus_mod_id=1455, 1 files on page)
    set_nexus_updates: 1 updates, 38 mods with nexus_id, 49 cards

Berserk has one file on its Nexus page, the matcher failed, yet the UI
still showed it as green.

Fix contract:

- ``check_mod_updates`` now emits ``ModUpdateStatus`` for every mod it
  both fetched AND matched a file for, with ``has_update`` reflecting
  actual state. Mods that failed the name match no longer appear in the
  return at all.
- ``mods_page.set_nexus_updates`` uses three states: red (in updates,
  has_update=True), green (in updates, has_update=False), grey
  (not in updates — unknown).
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


def _mock_get_mod_files(mod_id: int, api_key: str):
    """Per-mod_id stub: return file list + empty update chain."""
    if mod_id == 100:  # up-to-date case: local 1.0 matches Nexus 1.0
        return ([_FakeFile(file_id=1001, name="Up To Date Mod", version="1.0")],
                {})
    if mod_id == 200:  # outdated case: local 1.0 vs Nexus 2.0
        return ([_FakeFile(file_id=2001, name="Outdated Mod", version="2.0")],
                {})
    if mod_id == 300:  # name match failure: 3 files, none matches local name
        return ([
            _FakeFile(file_id=3001, name="Totally Different Name", version="5.0"),
            _FakeFile(file_id=3002, name="Another Unrelated Thing", version="2.1"),
            _FakeFile(file_id=3003, name="Yet A Third Variant", version="1.7"),
        ], {})
    if mod_id == 400:  # single-file page, non-matching name — Berserk case
        return ([_FakeFile(file_id=4001, name="Author's Timestamped Name v1.0",
                            version="1.0")], {})
    return ([], {})


def test_up_to_date_mod_now_appears_in_results(monkeypatch) -> None:
    """Previously, a mod that was confirmed current was NOT in `results`,
    so the UI had no way to distinguish "up to date" from "unchecked".
    After the fix, it appears with ``has_update=False``."""
    mods = [
        {"id": 1, "nexus_mod_id": 100, "name": "Up To Date Mod",
         "version": "1.0", "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={100}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mock_get_mod_files):
        results, _checked, _now, _bf = check_mod_updates(mods, "key")
    confirmed_current = [r for r in results if not r.has_update
                         and r.mod_id == 100]
    assert len(confirmed_current) == 1, (
        "confirmed-current mod must appear in results with has_update=False. "
        f"Got results: {results}")


def test_outdated_mod_still_flagged(monkeypatch) -> None:
    mods = [
        {"id": 2, "nexus_mod_id": 200, "name": "Outdated Mod",
         "version": "1.0", "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={200}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mock_get_mod_files):
        results, _checked, _now, _bf = check_mod_updates(mods, "key")
    flagged = [r for r in results if r.has_update and r.mod_id == 200]
    assert len(flagged) == 1


def test_match_failure_not_in_results(monkeypatch) -> None:
    """The Berserk case: Nexus page has files but none match the local
    mod name. Such mods must NOT appear in the results dict — they're
    unknown, not confirmed-anything."""
    mods = [
        {"id": 3, "nexus_mod_id": 300, "name": "Local Specific Name",
         "version": "1.0", "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={300}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mock_get_mod_files):
        results, _checked, _now, _bf = check_mod_updates(mods, "key")
    assert not any(r.mod_id == 300 for r in results), (
        "name-match-failure mod must NOT appear in results. "
        f"Got results: {results}")


def test_single_file_on_page_auto_matches(monkeypatch) -> None:
    """Bug #4 subsidiary: when a Nexus page has exactly 1 file, we
    should accept it even if the name doesn't strictly token-overlap.
    Berserk The Dragon Slayer has 1 file on its page and still failed
    matching in real logs, which proves the strict matcher was wrong.

    Covered here because both Bug #1 and Bug #4 are validated by the
    same synthetic scenario (mod 300 has 1 file, non-matching name)."""
    mods = [
        {"id": 4, "nexus_mod_id": 400, "name": "Berserk The Dragon Slayer",
         "version": "1.0", "nexus_real_file_id": 0,
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={400}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mock_get_mod_files):
        results, _checked, _now, _bf = check_mod_updates(mods, "key")
    assert any(r.mod_id == 400 for r in results), (
        "single-file page should auto-match even with non-overlapping name. "
        f"Got results: {results}")
