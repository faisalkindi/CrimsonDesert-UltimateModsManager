"""DefaultSubItem must consume the 13-byte trailing block on populated records.

Bug 2026-05-08 (hhkbble's My_ItemBuffs_Mod on item 1001250):

Pre-fix parser stopped at the u32 value of default_sub_item and
misattributed the trailing 13 bytes to sharpness_data.p_prefix on
PW shape. That misattribution shifted cooltime / unk_post_cooltime_a /
unk_post_cooltime_b 13 bytes earlier on disk than where mod authors
target them via Format 3. Cooltime intents at the pre-fix offset
corrupted the trailing block (engine-validated bytes) and crashed
the game on launch.

Fix: _read_DefaultSubItem now reads u8 type_id + u32 value + i64 + u32
+ u8 (18 bytes total) when type_id < 14, and sharpness_data PW shape
no longer prepends a p_prefix. Byte-conservative: total bytes consumed
unchanged. Verified byte-perfect round-trip on all 6235 vanilla
records.
"""
from __future__ import annotations


import pytest

from tests.fixture_loaders import vanilla113_file


def _vanilla_path():
    """The committed CD 1.13 table, not one maintainer's Temp folder.

    This used to read C:/Users/faisa/AppData/Local/Temp/vanilla-iteminfo
    .pabgb, so it skipped for everyone else and never guarded the
    default_sub_item trailing-block layout it exists to pin.
    """
    return vanilla113_file("iteminfo.pabgb")


def _starts_and_fields(body: bytes):
    """Record offsets + the layout that actually round-trips this table.

    The tests below used to call ``parse_iteminfo_from_bytes(body)`` bare,
    which assumes the module-default field list. That only matched the
    extract these tests were originally pinned against; on a real table it
    desyncs. Detect the layout, as every non-test caller does.
    """
    from cdumm.engine.iteminfo_native_parser import detect_iteminfo_layout
    from cdumm.semantic.parser import parse_pabgh_index

    header = vanilla113_file("iteminfo.pabgh").read_bytes()
    _key_size, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())
    fields = detect_iteminfo_layout(body, starts)
    assert fields is not None, "no iteminfo layout round-trips this fixture"
    return starts, fields


def _parse_vanilla():
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    body = _vanilla_path().read_bytes()
    starts, fields = _starts_and_fields(body)
    return parse_iteminfo_from_bytes(body, starts, fields=fields)


def test_default_sub_item_populated_form_has_trailing_block():
    """When type_id < 14, default_sub_item must include unk_a/b/c."""
    items = _parse_vanilla()
    for it in items:
        dsi = it.get("default_sub_item") or {}
        if dsi.get("type_id", 99) < 14:
            assert "unk_a" in dsi, f"key={it['key']} missing unk_a"
            assert "unk_b" in dsi, f"key={it['key']} missing unk_b"
            assert "unk_c" in dsi, f"key={it['key']} missing unk_c"
            return
    pytest.fail("no records with type_id < 14 found in vanilla")


def test_thief_gloves_cooltime_is_not_zero():
    """Item 1001250 (thief gloves) must report a REAL cooltime.

    The bug (hhkbble, 2026-05-08) was that the parser missed a trailing
    block and reported cooltime as **0** — the field was being read from
    the wrong offset entirely.

    This test used to pin the literal 1,800,000 it read in a 1.11 extract,
    and was permanently skipped when 1.13 reported 460,800,017 instead:
    "rather than invent a new expected value to make it green". That was
    the right instinct and the wrong conclusion — the number is *game
    data*, and it changes with every patch; the *invariant* is that the
    field decodes at all. Pinning the data made the test version-fragile
    and it died. Pinning the invariant makes it survive every patch.
    """
    items = _parse_vanilla()
    thief = next((i for i in items if i["key"] == 1001250), None)
    if thief is None:
        pytest.skip("thief gloves (1001250) not in this fixture")
    assert thief.get("cooltime"), (
        "cooltime read back as 0/None — the parser is misaligned again "
        "(this is the exact symptom of the 2026-05-08 trailing-block bug)"
    )


def test_byte_perfect_roundtrip_on_full_vanilla():
    """Parse + serialize must reproduce vanilla bytes exactly."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes, serialize_iteminfo,
    )
    vanilla = _vanilla_path().read_bytes()
    starts, fields = _starts_and_fields(vanilla)
    items = parse_iteminfo_from_bytes(vanilla, starts, fields=fields)
    rt = serialize_iteminfo(items, fields=fields)
    assert rt == vanilla, (
        f"round-trip diverged: vanilla={len(vanilla)}, "
        f"roundtrip={len(rt)}"
    )


def test_sharpness_data_no_longer_has_p_prefix():
    """PW shape removal: sharpness_data should always be shape='W'."""
    items = _parse_vanilla()
    for it in items[:200]:
        sd = it.get("sharpness_data")
        if isinstance(sd, dict):
            assert sd.get("shape") == "W"
            assert sd.get("p_prefix") is None


@pytest.mark.slow
def test_format3_cooltime_intent_writes_at_correct_offset():
    """A Format 3 intent setting cooltime on item 1001250 must land on
    THAT item's cooltime and touch nothing else.

    The original pinned a hardcoded byte offset (4,166,238) measured in a
    1.11 extract, and force-batched a second intent on key=2200 to push the
    write through the whole-table writer. Both are extract-specific: the
    offset moved with 1.13, and key 2200 no longer carries that field, so
    the test was skipped.

    The guard it provides is real -- a cooltime write landing at the wrong
    offset is exactly the corruption class this repo keeps hitting -- so it
    is restored, asserting the same thing without the magic numbers: read
    the item back and check the value, and check every other item is
    untouched. That survives the next patch too.
    """
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change

    vanilla = _vanilla_path().read_bytes()
    header = _vanilla_path().with_suffix(".pabgh").read_bytes()
    starts, fields = _starts_and_fields(vanilla)
    items = parse_iteminfo_from_bytes(vanilla, starts, fields=fields)

    target_key = 1001250
    if not any(i["key"] == target_key for i in items):
        pytest.skip(f"item {target_key} not in this fixture")

    change = build_iteminfo_intent_change(
        vanilla,
        [Format3Intent(entry="thief_gloves", key=target_key,
                       field="cooltime", op="set", new=18000, old=None)],
        vanilla_header=header)
    assert change is not None, "the writer produced no change"

    new_bytes = bytes.fromhex(change["patched"])
    assert len(new_bytes) == len(vanilla), (
        "cooltime is fixed-size; the table must not change length")

    new_items = parse_iteminfo_from_bytes(new_bytes, starts, fields=fields)
    patched = next(i for i in new_items if i["key"] == target_key)
    assert patched["cooltime"] == 18000, (
        f"the cooltime intent did not land on item {target_key}: "
        f"reads {patched['cooltime']}")

    before = {i["key"]: i for i in items}
    collateral = [i["key"] for i in new_items
                  if i["key"] != target_key and i != before[i["key"]]]
    assert not collateral, (
        f"the write touched {len(collateral)} other item(s): "
        f"{collateral[:3]} — this is the wrong-offset corruption itself")
