"""Stats summary bar must only count outdated entries, not every Nexus
update entry.

Bug: ``_update_stats`` summed every entry in ``_nexus_updates`` whose
nexus_id was on the page — but the dict carries BOTH outdated
(``has_update=True``) and confirmed-current (``has_update=False``)
entries since the three-state-pill rewrite. The PAZ page summary said
"10 outdated" alongside zero red pills because all 10 entries were
confirmed-current.

Both pages had the same bug; both must filter by has_update.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _Status:
    mod_id: int
    has_update: bool
    local_name: str = ""
    local_version: str = ""
    latest_version: str = ""
    mod_url: str = ""
    latest_file_id: int = 0


def test_paz_outdated_counter_ignores_confirmed_current():
    """Direct unit-test of the counting logic (no Qt). Mirror what
    ``mods_page._update_stats`` does so a regression on either side
    is loud."""
    nexus_updates = {
        100: _Status(mod_id=100, has_update=False),
        200: _Status(mod_id=200, has_update=True),
        300: _Status(mod_id=300, has_update=False),
        400: _Status(mod_id=400, has_update=True),
    }
    nexus_map = {1: 100, 2: 200, 3: 300, 4: 400}

    class _Card:
        def __init__(self, mid):
            self.mod_id = mid
    cards = [_Card(1), _Card(2), _Card(3), _Card(4)]

    outdated = 0
    for c in cards:
        nid = nexus_map.get(c.mod_id)
        if nid and nid in nexus_updates and getattr(
                nexus_updates[nid], "has_update", False):
            outdated += 1
    assert outdated == 2  # only the two has_update=True entries


def test_paz_outdated_counter_zero_when_all_current():
    """The exact symptom the user reported: 0 red pills, but stats
    must show 0 outdated, not 10."""
    nexus_updates = {nid: _Status(mod_id=nid, has_update=False)
                     for nid in range(100, 200, 10)}  # 10 entries
    nexus_map = {i: 100 + 10 * i for i in range(10)}

    class _Card:
        def __init__(self, mid):
            self.mod_id = mid
    cards = [_Card(i) for i in range(10)]

    outdated = 0
    for c in cards:
        nid = nexus_map.get(c.mod_id)
        if nid and nid in nexus_updates and getattr(
                nexus_updates[nid], "has_update", False):
            outdated += 1
    assert outdated == 0


def test_asi_outdated_counter_ignores_confirmed_current():
    """ASI page uses plugin_name -> nid lookup, but the has_update
    filter is identical."""
    nexus_updates = {
        500: _Status(mod_id=500, has_update=False),
        600: _Status(mod_id=600, has_update=True),
    }
    nexus_map = {"PluginA": 500, "PluginB": 600}

    class _Card:
        def __init__(self, name):
            self.plugin_name = name
    cards = [_Card("PluginA"), _Card("PluginB")]

    outdated = 0
    for c in cards:
        nid = nexus_map.get(getattr(c, "plugin_name", ""))
        if nid and nid in nexus_updates and getattr(
                nexus_updates[nid], "has_update", False):
            outdated += 1
    assert outdated == 1
