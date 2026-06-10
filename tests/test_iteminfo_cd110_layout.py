"""GitHub #182 (IliyaBrook) / #191 (falobos76): every iteminfo Format 3
mod silently produced "0-byte changes" on CD 1.09/1.10 because the
native parser still walked the 1.05-1.08 record layout and failed with
IndexError on the first record of the current game's iteminfo.pabgb.

Pearl Abyss changed the record layout twice:

CD 1.09:
  - removed the u32 extract_additional_drop_set_info
  - added a u8 (zero so far) between is_housing_only and
    quick_slot_index (schema name unk_flag_109)
  - added a conditional u8 before sharpness_data, present only when
    default_sub_item is populated (type_id < 14) — found by perfect
    correlation: all 349 then-failing records had type_id 0, all
    passing had 15

CD 1.10 additionally:
  - removed the u32 material_match_info duplicate after material_key
    (verified on record 10044, the one record where the two values
    differed in 1.09)
  - added a u32 inside money UnitData after icon_path (verified on
    record 1's Copper/Silver entries)

Trust anchor (same as the original post-1.0.4.1 fix): parse +
serialize on the extracted vanilla iteminfo.pabgb must be
byte-identical. These tests use the extracted fixtures under
issue_repro/182/ and skip when absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[1] / "issue_repro" / "182"
_V110 = _BASE / "vanilla110" / "iteminfo.pabgb"
_V109 = _BASE / "versions" / "iteminfo - 1.09.pabgb"


@pytest.mark.skipif(not _V110.exists(),
                    reason="extracted CD 1.10 iteminfo fixture not present")
def test_cd110_full_file_round_trips_byte_exact():
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes, serialize_iteminfo)
    body = _V110.read_bytes()
    items = parse_iteminfo_from_bytes(body)
    assert len(items) > 6000
    assert serialize_iteminfo(items) == body, (
        "CD 1.10 iteminfo must round-trip byte-identically or Format 3 "
        "iteminfo mods corrupt the file")


@pytest.mark.skipif(not _V110.exists(),
                    reason="extracted CD 1.10 iteminfo fixture not present")
def test_cd110_first_record_size_matches_pabgh_index():
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size
    from cdumm.semantic.parser import parse_pabgh_index
    body = _V110.read_bytes()
    header = (_V110.parent / "iteminfo.pabgh").read_bytes()
    _, offsets = parse_pabgh_index(header, "iteminfo")
    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])
    expected = sorted_offs[1][1] - sorted_offs[0][1]
    assert parse_first_record_size(body) == expected


@pytest.mark.skipif(not _V109.exists(),
                    reason="CD 1.09 iteminfo fixture not present")
def test_cd109_differs_only_by_known_deltas():
    """The 1.09 fixture must NOT round-trip with the 1.10 schema (it
    still carries material_match_info and the old UnitData), pinning
    that the 1.09->1.10 deltas are real and the schema tracks the live
    game, not an accidental superset that matches both."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes, serialize_iteminfo)
    body = _V109.read_bytes()
    try:
        items = parse_iteminfo_from_bytes(body)
    except Exception:
        return  # refusing to parse the old layout is acceptable
    assert serialize_iteminfo(items) != body, (
        "1.09 should not round-trip under the 1.10 schema; if it does, "
        "a layout assumption is wrong somewhere")
