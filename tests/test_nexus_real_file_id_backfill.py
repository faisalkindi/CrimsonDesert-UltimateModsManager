"""Bug #B (follow-up to API fixes): backfill nexus_real_file_id.

Evidence: Faisal's local DB has 38 mods with ``nexus_mod_id`` set but
every single one has ``nexus_real_file_id = None``. The column is only
populated when the user downloads through the nxm:// handler (which is
still pending Nexus approval), so every update check has to fall back
to name-matching against the Nexus file list. Name matching is
unreliable (Berserk Dragon Slayer, Horse, Smaller Crow's Wing etc. all
failed in the 2026-04-22 log), so those mods go grey forever.

Fix: when ``check_mod_updates`` successfully resolves a Nexus file via
the name-match path, emit the matched ``file_id`` in a new return
channel so the caller can persist it to ``nexus_real_file_id``. Next
check uses the reliable ``file_updates`` chain walk for the same row
— and if the mod is renamed or variant-split, the chain walk still
points at the right successor.

Contract: ``check_mod_updates`` gains a 4th return value
``backfill_file_ids: dict[int, int]`` where keys are ``mod.id`` row
ids and values are the Nexus file_ids that should be written to
``nexus_real_file_id``. Only rows that had a None/0 real_file_id AND
got a successful name match contribute — never overwrite an existing
real_file_id, and never emit for rows where chain-walk was used
(those already know their file_id).
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


def _mock_files(mod_id: int, api_key: str):
    if mod_id == 100:  # name-match-succeeds, local has no real_file_id
        return ([_FakeFile(file_id=9001, name="Some Mod", version="1.0")], {})
    if mod_id == 200:  # single-file auto-match, non-matching name
        return ([_FakeFile(file_id=9002, name="Totally Other Name",
                            version="1.0")], {})
    if mod_id == 300:  # local already has nexus_real_file_id — no backfill
        return ([_FakeFile(file_id=9003, name="Known Mod", version="2.0")], {})
    return ([], {})


def test_backfill_returned_for_name_match_with_no_prior_file_id() -> None:
    mods = [
        {"id": 10, "nexus_mod_id": 100, "name": "Some Mod",
         "version": "1.0", "nexus_real_file_id": None,
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={100}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mock_files):
        _results, _checked, _now, backfill = check_mod_updates(mods, "key")
    assert backfill == {10: 9001}, (
        f"name-match success on a row missing real_file_id should emit "
        f"a backfill entry. Got: {backfill}")


def test_backfill_returned_for_single_file_auto_match() -> None:
    """Bug #4 single-file fallback should also backfill."""
    mods = [
        {"id": 20, "nexus_mod_id": 200, "name": "Berserk-Like Name",
         "version": "1.0", "nexus_real_file_id": 0,
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={200}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mock_files):
        _results, _checked, _now, backfill = check_mod_updates(mods, "key")
    assert backfill == {20: 9002}


def test_no_backfill_when_real_file_id_already_set() -> None:
    """Don't overwrite a reliable pre-existing real_file_id."""
    mods = [
        {"id": 30, "nexus_mod_id": 300, "name": "Known Mod",
         "version": "2.0", "nexus_real_file_id": 9003,
         "nexus_last_checked_at": 0},
    ]
    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={300}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_mock_files):
        _results, _checked, _now, backfill = check_mod_updates(mods, "key")
    assert backfill == {}, (
        f"row with existing real_file_id must not be backfilled. "
        f"Got: {backfill}")
