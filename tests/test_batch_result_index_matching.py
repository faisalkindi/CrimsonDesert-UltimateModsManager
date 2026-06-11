"""Batch import results must be matched to source paths by the
worker-emitted ``index`` key.

The worker emits each ``batch_item`` with ``index`` = the position of
the source path in the submitted list. The GUI used to look up the
nonexistent key ``_batch_idx`` (always falling through to fuzzy name
matching) and, in the ASI post-install block, zip() results against
paths positionally, which shifts every pairing after the first failed
item (failures never reach the success list).
"""
from __future__ import annotations

from cdumm.gui.fluent_window import _batch_result_for_index


def test_exact_index_match():
    results = [
        {"index": 0, "name": "ModA", "mod_id": 10},
        {"index": 1, "name": "ModB", "mod_id": 11},
    ]
    assert _batch_result_for_index(results, 0)["mod_id"] == 10
    assert _batch_result_for_index(results, 1)["mod_id"] == 11


def test_failure_gap_does_not_shift_pairing():
    """Item at index 0 failed (absent from the success list). Index 1
    and 2 must still map to their own results; a positional zip would
    have paired path 0 with ModB and path 1 with ModC."""
    results = [
        {"index": 1, "name": "ModB", "mod_id": 11},
        {"index": 2, "name": "ModC", "mod_id": 12},
    ]
    assert _batch_result_for_index(results, 0) is None
    assert _batch_result_for_index(results, 1)["name"] == "ModB"
    assert _batch_result_for_index(results, 2)["name"] == "ModC"


def test_name_fallback_when_no_index_matches():
    results = [{"name": "Cool Mod Renamed", "mod_id": 5}]
    item = _batch_result_for_index(results, 3, path_stem="Cool Mod")
    assert item is not None
    assert item["mod_id"] == 5


def test_index_match_wins_over_name_fallback():
    results = [
        {"index": 1, "name": "Cool Mod", "mod_id": 5},
        {"index": 0, "name": "Other", "mod_id": 6},
    ]
    item = _batch_result_for_index(results, 0, path_stem="Cool Mod")
    assert item["mod_id"] == 6


def test_no_match_returns_none():
    assert _batch_result_for_index([], 0) is None
    assert _batch_result_for_index(
        [{"index": 5, "name": "X"}], 0, path_stem="Zzz") is None
