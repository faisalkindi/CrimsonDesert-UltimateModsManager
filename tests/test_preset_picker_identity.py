"""HIGH #13: preset_picker must identify selected changes by stable key.

id() is fragile — Python may recycle ids after GC, and any copy /
reserialisation of the change dicts breaks id-based matching. When a
user Apply's toggles, the filter must survive data round-trips.
"""
from __future__ import annotations

import copy

from cdumm.gui.preset_picker import _filter_patches_by_keys


def test_filter_by_patch_change_index_keys():
    data = {
        "patches": [
            {"changes": [
                {"offset": 0, "label": "A"},
                {"offset": 4, "label": "B"},
            ]},
            {"changes": [
                {"offset": 0, "label": "C"},
            ]},
        ]
    }
    # Keep patch 0 change 1, patch 1 change 0.
    keys = {(0, 1), (1, 0)}
    filtered = _filter_patches_by_keys(data, keys)
    assert len(filtered["patches"]) == 2
    assert filtered["patches"][0]["changes"] == [{"offset": 4, "label": "B"}]
    assert filtered["patches"][1]["changes"] == [{"offset": 0, "label": "C"}]


def test_filter_survives_deepcopy_of_data():
    """Deepcopy-ing data (which breaks id()) must not break index-based filter."""
    data = {"patches": [{"changes": [{"offset": 0, "label": "X"}]}]}
    copied = copy.deepcopy(data)
    filtered = _filter_patches_by_keys(copied, {(0, 0)})
    assert filtered["patches"][0]["changes"] == [{"offset": 0, "label": "X"}]


def test_empty_keys_produces_empty_patches():
    data = {"patches": [{"changes": [{"offset": 0}]}]}
    filtered = _filter_patches_by_keys(data, set())
    assert filtered["patches"] == []


def test_filter_does_not_mutate_input():
    data = {"patches": [{"changes": [{"offset": 0, "label": "A"},
                                     {"offset": 4, "label": "B"}]}]}
    before = copy.deepcopy(data)
    _filter_patches_by_keys(data, {(0, 0)})
    assert data == before
