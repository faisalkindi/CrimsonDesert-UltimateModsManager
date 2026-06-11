"""Faisal, 2026-06-11 (on v3.3.21): some mods never showed an
up-to-date state. The feed-skip optimization (confirmed current
within the past week and absent from the 1-week updated feed) used
to `continue` without emitting a status; the old UI masked that by
wrongly painting every linked mod green, and once the grey-state fix
removed that, skipped mods rendered as never-checked grey pills.

The checker now emits a cached-current status (from_cache=True) for
skipped mods, and the GUI merge refuses to let a cached entry
overwrite a known has_update=True state, so updates older than the
feed window cannot flip a red pill green.
"""
from __future__ import annotations

import time

import pytest

import cdumm.engine.nexus_api as nx
from cdumm.engine.nexus_api import ModUpdateStatus, check_mod_updates
from cdumm.gui.fluent_window import _merge_nexus_updates


def _mod(row_id, nexus_id, last_checked, version="1.0"):
    return {
        "id": row_id, "name": f"Mod{nexus_id}", "version": version,
        "nexus_mod_id": nexus_id, "nexus_real_file_id": None,
        "nexus_last_checked_at": last_checked,
    }


def test_feed_skipped_mod_emits_cached_current(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(nx, "get_recently_updated", lambda *a, **k: set())
    fetched = []
    monkeypatch.setattr(
        nx, "get_mod_files",
        lambda mid, key: fetched.append(mid) or ([], {}))

    mods = [_mod(1, 100, now - 3600)]  # checked an hour ago, not in feed
    updates, checked_ids, _, _ = check_mod_updates(mods, "key")

    assert fetched == [], "feed-skipped mod must not hit the API"
    assert checked_ids == [], "cached entries must not refresh timestamps"
    assert len(updates) == 1, (
        "feed-skipped mod emitted no status; the UI renders that as "
        "never-checked grey (the v3.3.21 regression)")
    u = updates[0]
    assert u.mod_id == 100
    assert u.has_update is False
    assert u.from_cache is True
    assert u.latest_version == u.local_version


def test_stale_or_unchecked_mod_still_fetches(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(nx, "get_recently_updated", lambda *a, **k: set())
    fetched = []
    monkeypatch.setattr(
        nx, "get_mod_files",
        lambda mid, key: fetched.append(mid) or ([], {}))

    mods = [
        _mod(1, 100, 0),                              # never checked
        _mod(2, 200, now - 8 * 24 * 3600),            # checked 8 days ago
    ]
    check_mod_updates(mods, "key")
    assert sorted(fetched) == [100, 200], (
        "stale/never-checked mods must still be fetched live")


def test_feed_failure_ttl_skip_also_emits_cached(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(nx, "get_recently_updated", lambda *a, **k: None)
    fetched = []
    monkeypatch.setattr(
        nx, "get_mod_files",
        lambda mid, key: fetched.append(mid) or ([], {}))

    mods = [_mod(1, 100, now - 3600)]
    updates, _, _, _ = check_mod_updates(mods, "key")
    assert fetched == []
    assert len(updates) == 1 and updates[0].from_cache is True


def _status(nexus_id, has_update, from_cache=False):
    return ModUpdateStatus(
        mod_id=nexus_id, local_name=f"Mod{nexus_id}",
        local_version="1.0",
        latest_version="2.0" if has_update else "1.0",
        has_update=has_update, mod_url="u", from_cache=from_cache)


def test_merge_cached_never_clears_red_pill():
    prev = {100: _status(100, has_update=True)}
    new = {100: _status(100, has_update=False, from_cache=True)}
    merged = _merge_nexus_updates(prev, new)
    assert merged[100].has_update is True, (
        "a cached entry flipped a known-outdated mod back to green "
        "(updates older than the feed window would vanish)")


def test_merge_live_fetch_clears_red_pill():
    prev = {100: _status(100, has_update=True)}
    new = {100: _status(100, has_update=False, from_cache=False)}
    merged = _merge_nexus_updates(prev, new)
    assert merged[100].has_update is False, (
        "a LIVE confirmed-current fetch must clear the outdated flag "
        "(e.g. after the user updates the mod)")


def test_merge_cached_fills_unknown_state():
    prev = {}
    new = {100: _status(100, has_update=False, from_cache=True)}
    merged = _merge_nexus_updates(prev, new)
    assert merged[100].has_update is False
