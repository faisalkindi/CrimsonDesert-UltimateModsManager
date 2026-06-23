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

Game build 23693656 (Steam patch 2026-06-12) additionally:
  - added one u8 between apply_drop_stat_type and drop_default_data
    (schema name unk_flag_b23693656, value 1 on 6332 of 6333 records).
    iteminfo.pabgb grew 5,532,062 -> 5,543,339, records 6325 -> 6333.
    The committed tests/fixtures/vanilla110/ extract is now this
    build, so these round-trip tests pin the field's presence.

Trust anchor (same as the original post-1.0.4.1 fix): parse +
serialize on the extracted vanilla iteminfo.pabgb must be
byte-identical. The committed fixture (loaded via has_vanilla110 /
load_vanilla110) tracks the current game build.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixture_loaders import has_vanilla110, load_vanilla110

_BASE = Path(__file__).resolve().parents[1] / "issue_repro" / "182"
_V109 = _BASE / "versions" / "iteminfo - 1.09.pabgb"


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="extracted CD 1.10 iteminfo fixture not present")
def test_cd110_full_file_round_trips_byte_exact():
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes, serialize_iteminfo)
    body = load_vanilla110("iteminfo.pabgb")
    items = parse_iteminfo_from_bytes(body)
    assert len(items) > 6000
    assert serialize_iteminfo(items) == body, (
        "CD 1.10 iteminfo must round-trip byte-identically or Format 3 "
        "iteminfo mods corrupt the file")


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="extracted CD 1.10 iteminfo fixture not present")
def test_cd110_full_file_round_trips_with_index_framing():
    """Index-framed parse (what the writer uses): must see every
    index entry as its own record, including the large-key record the
    sniff walk swallows (Delesyian_Flag, audit M12), and still
    round-trip byte-exactly."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes, serialize_iteminfo)
    from cdumm.semantic.parser import parse_pabgh_index
    body = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    _, offsets = parse_pabgh_index(header, "iteminfo")
    items = parse_iteminfo_from_bytes(
        body, record_offsets=list(offsets.values()))
    assert len(items) == len(offsets)
    assert serialize_iteminfo(items) == body


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"),
                    reason="extracted CD 1.10 iteminfo fixture not present")
def test_cd110_first_record_size_matches_pabgh_index():
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size
    from cdumm.semantic.parser import parse_pabgh_index
    body = load_vanilla110("iteminfo.pabgb")
    header = load_vanilla110("iteminfo.pabgh")
    _, offsets = parse_pabgh_index(header, "iteminfo")
    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])
    expected = sorted_offs[1][1] - sorted_offs[0][1]
    assert parse_first_record_size(body) == expected


@pytest.mark.skipif(not _V109.exists(),
                    reason="CD 1.09 iteminfo fixture not present")
def test_cd109_not_genuinely_decoded_by_current_schema():
    """The 1.09 fixture must NOT be genuinely DECODED by the current
    (CD 1.12) schema, pinning that the schema tracks the live game and is
    not an accidental superset that decodes old layouts too.

    Note: as of GitHub #219 the parser carries records it cannot decode
    as opaque raw bytes (so the whole-table round-trip stays byte-exact
    even with a few undecodable records). That makes EVERY file
    round-trip, so the old "must not round-trip" check no longer
    distinguishes versions. The version-specificity invariant is now:
    under a foreign-version layout the records come back OPAQUE, not
    decoded into editable field dicts."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes)
    body = _V109.read_bytes()
    try:
        items = parse_iteminfo_from_bytes(body)
    except Exception:
        return  # refusing to parse the old layout is acceptable
    opaque = sum(1 for it in items if it.get("_opaque_record"))
    # The 1.09 layout differs from 1.12 in every record, so essentially
    # all of them should fail to decode and be carried opaque. If most
    # decoded cleanly, the schema is accidentally matching 1.09.
    assert opaque >= len(items) // 2, (
        f"1.09 should not be genuinely decoded by the 1.12 schema, but "
        f"only {opaque}/{len(items)} records came back opaque; a layout "
        f"assumption is wrong somewhere")
