"""Tests for the version-adaptive iteminfo layout selection
(detect_iteminfo_layout) added for CD 1.13, which relocated
prefab_data_list + gimmick_visual_prefab_data_list to the record tail.

The 1.13 game table isn't committed, so these exercise the mechanics on
the pre-1.13 fixture (must select the default layout and round-trip) and
the edge cases; the live-1.13 byte-exact round-trip is covered by
scripts/verify_113_parser.py against the installed game.
"""
from __future__ import annotations

import pytest

from tests.fixture_loaders import has_vanilla110, load_vanilla110

from cdumm.engine.iteminfo_native_parser import (  # noqa: E402
    _ITEM_FIELDS, _ITEM_FIELDS_CD113, detect_iteminfo_layout,
    parse_iteminfo_from_bytes, serialize_iteminfo, _record_roundtrips,
)


def test_cd113_fields_are_default_minus_relocated():
    removed = {"prefab_data_list", "gimmick_visual_prefab_data_list"}
    assert [f[0] for f in _ITEM_FIELDS_CD113] == \
           [f[0] for f in _ITEM_FIELDS if f[0] not in removed]
    # order of the surviving fields is preserved
    assert len(_ITEM_FIELDS_CD113) == len(_ITEM_FIELDS) - 2


def test_detect_handles_empty_and_tiny_offsets():
    # regression: sample indices must not run off a short table
    assert detect_iteminfo_layout(b"", []) is None
    assert detect_iteminfo_layout(b"\x00" * 4, [0]) in (None, _ITEM_FIELDS_CD113)


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="1.10 iteminfo fixtures absent")
def test_pre113_fixture_selects_default_and_roundtrips():
    from cdumm.semantic.parser import parse_pabgh_index
    body = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    _, off = parse_pabgh_index(header, "iteminfo")
    offsets = sorted(off.values())

    fields = detect_iteminfo_layout(body, offsets)
    assert fields is None, "pre-1.13 fixture must select the default layout"

    items = parse_iteminfo_from_bytes(body, offsets, fields=fields)
    out = serialize_iteminfo(items, fields=fields)
    assert bytes(out) == bytes(body)


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="1.10 iteminfo fixtures absent")
def test_record_roundtrips_helper_on_fixture_record0():
    from cdumm.semantic.parser import parse_pabgh_index
    body = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    _, off = parse_pabgh_index(header, "iteminfo")
    starts = sorted(off.values())
    s, e = starts[0], starts[1]  # record 0 spans the first two offsets
    # default layout round-trips a pre-1.13 record; the relocated variant
    # does not (it would leave prefab bytes mid-record).
    assert _record_roundtrips(body, s, e, None) is True
    assert _record_roundtrips(body, s, e, _ITEM_FIELDS_CD113) is False
