"""GitHub #379: once a space-separated Nexus name links a mod, the
update check still has to find the mod's file on its Nexus page.

For a manual download with no modinfo.json, the display name the import
stores is the prettified archive name, which keeps the id, version,
timestamp and token. ``prettify_mod_name`` turns
'Better Inventory and Trade UI 452 3.0 2026-08-26T15-38Z mhw3QMq4Q'
into 'Better Inventory And Trade UI 452 3.0 2026 08 26t15 38z Mhw3q
Mq4q', whose token overlap with the page's 'Better Inventory and Trade
UI' is 0.38, under the 0.6 match threshold. So on a page with more than
one file, the only route to a match is the renamed-mod fallback that
reads drop_name, and that fallback only stripped the dash-separated
'-<id>-<version>-<timestamp>' suffix.
"""
from __future__ import annotations

from types import SimpleNamespace

import cdumm.engine.nexus_api as nx
from cdumm.engine.nexus_api import check_mod_updates

STORED_NAME = ("Better Inventory And Trade UI 452 3.0 2026 08 26t15 38z "
               "Mhw3q Mq4q")


def _nexus_file(file_id, name, version, ts=1700000000):
    return SimpleNamespace(
        file_id=file_id, name=name, version=version,
        uploaded_timestamp=ts, file_name=name, category_id=1)


def _two_file_page(monkeypatch):
    files = [
        _nexus_file(11, "Better Inventory and Trade UI", "3.1"),
        _nexus_file(12, "Controller Prompts Addon", "1.0"),
    ]
    monkeypatch.setattr(nx, "get_recently_updated", lambda *a, **k: {452})
    monkeypatch.setattr(nx, "get_mod_files", lambda mid, key: (files, []))


def _mod(drop_name):
    return {
        "id": 1, "name": STORED_NAME, "version": "3.0",
        "nexus_mod_id": 452, "nexus_real_file_id": None,
        "nexus_last_checked_at": 0, "drop_name": drop_name,
    }


def test_stored_display_name_does_not_match_on_its_own(monkeypatch):
    """Premise: without a usable drop_name the mod cannot be matched."""
    _two_file_page(monkeypatch)
    updates, _, _, _ = check_mod_updates([_mod(None)], "key")
    assert updates == []


def test_space_separated_drop_name_finds_the_file(monkeypatch):
    """#458's archive name: the file-name part matches file 11."""
    _two_file_page(monkeypatch)
    updates, _, _, backfill = check_mod_updates([_mod(
        "Better Inventory and Trade UI 452 3.0 2026-08-26T15-38Z "
        "mhw3QMq4Q.zip")], "key")
    assert len(updates) == 1
    assert updates[0].has_update is True
    assert updates[0].latest_version == "3.1"
    assert backfill == {1: 11}


def test_nested_variant_drop_name_finds_the_file(monkeypatch):
    """'||<variant>' after the archive name (as in #415) is ignored."""
    _two_file_page(monkeypatch)
    updates, _, _, _ = check_mod_updates([_mod(
        "Better Inventory and Trade UI 452 3.0 2026-08-26T15-38Z "
        "mhw3QMq4Q.zip||variant_a")], "key")
    assert len(updates) == 1
    assert updates[0].latest_version == "3.1"


def test_non_nexus_drop_name_still_stays_unmatched(monkeypatch):
    """A drop_name that is not a Nexus name gives nothing new to match
    on, so the mod stays un-flagged rather than guessed."""
    _two_file_page(monkeypatch)
    updates, _, _, _ = check_mod_updates(
        [_mod("my renamed copy.zip")], "key")
    assert updates == []
