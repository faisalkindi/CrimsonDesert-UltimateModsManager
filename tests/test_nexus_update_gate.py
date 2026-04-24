"""check_mod_updates: 1-week feed is optimization, not gate.

Codex review caught the regression: mods whose latest file release is
older than 7 days (or whose user hasn't opened CDUMM for more than a
week) were being reported 'up to date' because the 1-week
recently-updated feed was the sole filter. After this fix, mods get
freshly checked whenever their stored nexus_last_checked_at is older
than 7 days, even if they're not in the feed.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from cdumm.engine import nexus_api


def _fake_files(version: str, name: str = "Foo"):
    """A single fake Nexus file. ``name`` defaults to ``Foo`` to match
    the local mod's name in the existing tests so the new
    name-matching filter accepts it (mod pages can host multiple
    distinct files; updates are now scoped to the matching one)."""
    return [SimpleNamespace(version=version, uploaded_timestamp=1700000000,
                             name=name, file_id=1)]


@pytest.fixture
def _stub_api(monkeypatch):
    """Stub the three HTTP calls so tests run offline."""
    state = {"get_mod_files_calls": []}

    def _fake_recent(api_key, period="1w"):
        return state.get("updated_ids", set())

    def _fake_files_fn(nexus_id, api_key):
        state["get_mod_files_calls"].append(nexus_id)
        files = state.get("files", {}).get(nexus_id, _fake_files("2.0"))
        # Real get_mod_files returns (files, file_updates) — match.
        return (files, state.get("file_updates", {}).get(nexus_id, []))

    monkeypatch.setattr(nexus_api, "get_recently_updated", _fake_recent)
    monkeypatch.setattr(nexus_api, "get_mod_files", _fake_files_fn)
    return state


def test_mod_in_feed_always_checked(_stub_api):
    _stub_api["updated_ids"] = {500}
    _stub_api["files"] = {500: _fake_files("2.0")}

    mods = [{"id": 1, "name": "Foo", "version": "1.0",
             "nexus_mod_id": 500, "nexus_last_checked_at": int(time.time())}]
    results, checked_ids, _now, _bf = nexus_api.check_mod_updates(mods, "key")

    assert _stub_api["get_mod_files_calls"] == [500]
    assert len(results) == 1
    assert results[0].local_version == "1.0"
    assert results[0].latest_version == "2.0"
    assert checked_ids == [1]


def test_mod_not_in_feed_but_stale_last_checked_is_fetched(_stub_api):
    _stub_api["updated_ids"] = set()  # empty feed
    _stub_api["files"] = {500: _fake_files("2.0")}

    ten_days_ago = int(time.time() - 10 * 86400)
    mods = [{"id": 1, "name": "Foo", "version": "1.0",
             "nexus_mod_id": 500, "nexus_last_checked_at": ten_days_ago}]
    results, _ids, _now, _bf = nexus_api.check_mod_updates(mods, "key")

    assert _stub_api["get_mod_files_calls"] == [500]
    assert len(results) == 1


def test_mod_not_in_feed_and_recently_checked_is_skipped(_stub_api):
    _stub_api["updated_ids"] = set()
    _stub_api["files"] = {500: _fake_files("2.0")}

    two_hours_ago = int(time.time() - 2 * 3600)
    mods = [{"id": 1, "name": "Foo", "version": "1.0",
             "nexus_mod_id": 500, "nexus_last_checked_at": two_hours_ago}]
    results, _ids, _now, _bf = nexus_api.check_mod_updates(mods, "key")

    assert _stub_api["get_mod_files_calls"] == []
    assert results == []


def test_mod_with_never_checked_fetches_regardless_of_feed(_stub_api):
    _stub_api["updated_ids"] = set()
    _stub_api["files"] = {500: _fake_files("1.0")}  # same version

    mods = [{"id": 1, "name": "Foo", "version": "1.0",
             "nexus_mod_id": 500, "nexus_last_checked_at": 0}]
    results, checked_ids, _now, _bf = nexus_api.check_mod_updates(mods, "key")

    assert _stub_api["get_mod_files_calls"] == [500]
    # Bug #1 fix: confirmed-current mods now appear in results with
    # has_update=False so the UI can paint them green. Previously
    # results was empty for this case and callers had no way to tell
    # "confirmed current" from "unchecked".
    assert len(results) == 1
    assert results[0].mod_id == 500
    assert results[0].has_update is False
    assert checked_ids == [1]  # persistence still happens on success


def test_failed_file_fetch_is_not_persisted(_stub_api):
    """A transient get_mod_files failure must NOT mark the mod checked."""
    _stub_api["updated_ids"] = {500}

    def _fail(nexus_id, api_key):
        _stub_api["get_mod_files_calls"].append(nexus_id)
        return None  # transport error sentinel

    import pytest
    pytest._monkeypatch = None  # noqa
    # Monkeypatch the fixture's handler directly
    from cdumm.engine import nexus_api as _na
    original = _na.get_mod_files
    _na.get_mod_files = _fail
    try:
        mods = [{"id": 1, "name": "Foo", "version": "1.0",
                 "nexus_mod_id": 500, "nexus_last_checked_at": 0}]
        results, checked_ids, _now, _bf = _na.check_mod_updates(mods, "key")
    finally:
        _na.get_mod_files = original

    assert _stub_api["get_mod_files_calls"] == [500]
    assert results == []
    # Transient failure must NOT mark the mod as checked — next run
    # must retry. Codex P1 fix.
    assert checked_ids == []


def test_returns_tuple_with_timestamp(_stub_api):
    _stub_api["updated_ids"] = {500}
    _stub_api["files"] = {500: _fake_files("2.0")}
    mods = [{"id": 1, "name": "Foo", "version": "1.0",
             "nexus_mod_id": 500, "nexus_last_checked_at": 0}]
    results, checked_ids, now_ts, _bf = nexus_api.check_mod_updates(mods, "key")
    assert len(results) == 1
    assert checked_ids == [1]
    assert now_ts > 0
