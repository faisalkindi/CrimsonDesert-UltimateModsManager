"""Round 5 systematic-debugging fix: defensive mod['name'] access.

The result emission paths used `mod["name"]` directly — a KeyError
if the caller passes a mod dict without a 'name' key. The earlier
code at line 563 already uses `mod.get("name") or ""` defensively;
the result sites should match.
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
    category_id: int = 1


def _files_one_main(mod_id, api_key):
    if mod_id != 100:
        return ([], [])
    return ([_FakeFile(file_id=999, name="X", version="2.0")], [])


def test_check_mod_updates_does_not_crash_on_missing_name() -> None:
    """A mod dict without a 'name' key must not raise KeyError.
    Construct a scenario that reaches the result emission path —
    set nexus_real_file_id so the chain-walk path fires and
    successfully resolves a file."""
    mods = [
        {"id": 1, "nexus_mod_id": 100,
         # NO "name" key intentionally
         "version": "1.0",
         "nexus_real_file_id": 999,  # matches mocked file
         "nexus_last_checked_at": 0},
    ]

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={100: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_files_one_main):
        try:
            results, _checked, _now, _backfill = check_mod_updates(
                mods, "key")
        except KeyError as e:
            raise AssertionError(
                f"check_mod_updates raised KeyError on mod with "
                f"missing 'name' key: {e}. Use mod.get('name', '') "
                f"defensively at result emission sites for "
                f"consistency with line 563.")

    assert isinstance(results, list)
    assert results, "result should still be emitted for the mod"


def test_check_mod_updates_does_not_crash_on_missing_name_emit_path(
) -> None:
    """Same defensive concern, but reaching the round-3 emit-on-
    name-match-fail block. Set nexus_real_file_id to a value
    that's deleted from Nexus + 2 unrelated files so name-match
    fails too — that triggers the new emit block."""
    def _files_unrelated(mod_id, api_key):
        return ([
            _FakeFile(file_id=8001, name="Other A", version="1.0"),
            _FakeFile(file_id=8002, name="Other B", version="1.0"),
        ], [])

    mods = [
        {"id": 2, "nexus_mod_id": 100,
         # No "name"
         "version": "1.0",
         "nexus_real_file_id": 4000,  # gone from Nexus
         "nexus_last_checked_at": 0},
    ]

    with patch("cdumm.engine.nexus_api.get_recently_updated",
               return_value={100: 0}), \
         patch("cdumm.engine.nexus_api.get_mod_files",
               side_effect=_files_unrelated):
        try:
            check_mod_updates(mods, "key")
        except KeyError as e:
            raise AssertionError(
                f"emit-on-name-match-fail block raised KeyError on "
                f"mod with missing 'name' key: {e}")
