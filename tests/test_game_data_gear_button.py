"""The Game Data page's own gear-stat plumbing, not just the engine.

`_locate_gear_stats` is the function the preview worker actually calls. The
engine locator is tested in test_gear_stat_view.py; this pins the GUI-side
wiring, because "the engine works but the page never calls it" is a real way
to ship a dead feature — and is roughly what happened to gear stats before
(#281: the writer could apply them, the validator refused them, and nobody
tested the whole chain).
"""
from __future__ import annotations

import os

import pytest

from tests.fixture_loaders import load_vanilla113

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qfluentwidgets")

from cdumm.gui.pages.game_data_page import _locate_gear_stats  # noqa: E402

VANILLA_RECORDS_WITH_STATS = 3_319
VANILLA_STAT_ENTRIES = 28_081
HELM_KEY = 14510


@pytest.fixture(scope="module")
def iteminfo():
    return (load_vanilla113("iteminfo.pabgb"),
            load_vanilla113("iteminfo.pabgh"))


def test_the_page_locates_the_real_stats(iteminfo):
    body, header = iteminfo
    found = _locate_gear_stats("iteminfo", body, header)
    assert len(found) == VANILLA_RECORDS_WITH_STATS
    assert sum(len(v) for v in found.values()) == VANILLA_STAT_ENTRIES
    assert HELM_KEY in found


def test_the_helm_row_offers_named_tiers(iteminfo):
    body, header = iteminfo
    stats = _locate_gear_stats("iteminfo", body, header)[HELM_KEY]
    # base + every enhancement tier, each separately addressable
    assert stats[0].group == "Base"
    assert stats[0].where == "Base"
    tiers = {s.group for s in stats if s.group != "Base"}
    assert len(tiers) > 1
    assert all(s.path for s in stats)
    assert len({s.path for s in stats}) == len(stats)


def test_other_tables_get_no_gear_stats(iteminfo):
    """The button must never appear on a table that has no stats."""
    body, header = iteminfo
    assert _locate_gear_stats("skill", body, header) == {}
    assert _locate_gear_stats("dropsetinfo", body, header) == {}


def test_a_broken_table_does_not_take_the_preview_down_with_it():
    """A table view must render even if the stat locator chokes — the
    locator is a bonus pane, not a precondition for showing the grid."""
    assert _locate_gear_stats("iteminfo", b"", b"") == {}
    assert _locate_gear_stats("iteminfo", b"\x00" * 32, b"\x01\x00") == {}
