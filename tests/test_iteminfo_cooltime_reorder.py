"""#191: the type_id==0 equipment shape reads item_charge_type BEFORE cooltime.

falobos76's Great Thief's Gloves cooldown mod writes 24 bytes (three i64
cooltime values) at one offset. CDUMM decoded cooltime, but for the
default_sub_item.type_id==0 shape a stray u8 (item_charge_type) sits before it,
so the three i64s were framed one byte early -- CDUMM read cooltime=460,800,017
where the true value is 1,800,000. Reordering item_charge_type before cooltime
for that shape (and only that shape) fixes the framing.

Derived on the real 1.13 table: every type_id==0 record reads a sensible cooltime
only in this order (390/390) and every other record only in the flat order
(6118/6118). The gloves then read exactly 1,800,000, matching DMM and the mod.
"""
from __future__ import annotations

import pytest

from cdumm.engine.iteminfo_native_parser import _reorder_equip_tail

from tests.fixture_loaders import has_vanilla113, load_vanilla113

FIXTURE = "iteminfo.pabgb"


# ── fast unit tests for the reorder helper (no game table) ──────────────

_FIELDS = [("default_sub_item", "struct"), ("cooltime", "i64"),
           ("unk_post_cooltime_a", "i64"), ("unk_post_cooltime_b", "i64"),
           ("item_charge_type", "u8"), ("sharpness_data", "struct")]


def test_reorder_moves_item_charge_type_before_cooltime_for_type0():
    rec = {"default_sub_item": {"type_id": 0}}
    out = [f[0] for f in _reorder_equip_tail(_FIELDS, rec)]
    assert out.index("item_charge_type") == out.index("cooltime") - 1, (
        "item_charge_type must sit immediately before cooltime for type_id==0")
    # every field still present exactly once
    assert sorted(out) == sorted(f[0] for f in _FIELDS)


def test_reorder_is_noop_for_other_type_ids():
    for tid in (15, 16, 17):
        rec = {"default_sub_item": {"type_id": tid}}
        assert _reorder_equip_tail(_FIELDS, rec) is _FIELDS, (
            "non-zero type_id must keep the flat order (returned unchanged)")


def test_reorder_is_noop_when_fields_absent():
    rec = {"default_sub_item": {"type_id": 0}}
    only = [("cooltime", "i64")]
    assert _reorder_equip_tail(only, rec) is only        # no item_charge_type
    assert _reorder_equip_tail([("key", "u32")], {}) == [("key", "u32")]


# ── real-1.13-table proof: byte-exact + gloves read correctly ───────────

@pytest.mark.slow
@pytest.mark.skipif(not has_vanilla113(FIXTURE),
                    reason="1.13 iteminfo fixture not present")
def test_cooltime_reorder_byte_exact_and_gloves_correct():
    import cdumm.engine.iteminfo_native_parser as np
    from cdumm.semantic.parser import parse_pabgh_index

    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    _, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())
    layout = np.detect_iteminfo_layout(body, starts)
    items = np.parse_iteminfo_from_bytes(body, record_offsets=starts, fields=layout)

    # the reorder must not disturb the sacred byte-exact round-trip
    ident = np.serialize_iteminfo(items, offsets_out={}, fields=layout)
    assert ident == body, "round-trip must stay byte-exact"
    assert sum(1 for it in items if it.get("_opaque_record")) == 0

    # no cooldown item reads a garbage (shifted) value any more
    garbage = [it["key"] for it in items
               if it.get("cooltime", 0) and not (0 <= it["cooltime"] < 10_000_000)]
    assert garbage == [], f"cooltime still garbage on {garbage[:5]}"

    # the thief gloves read the true cooldown (1,800,000), matching DMM + the mod
    g = next(it for it in items if it["key"] == 1001250)
    assert g["cooltime"] == 1_800_000
    assert g["unk_post_cooltime_a"] == 1_800_000
    assert g["unk_post_cooltime_b"] == 1_800_000


@pytest.mark.slow
@pytest.mark.skipif(not has_vanilla113(FIXTURE),
                    reason="1.13 iteminfo fixture not present")
def test_gloves_cooldown_change_converts_to_three_intents():
    """The 24-byte gloves change tiles three cooltime i64s; the converter now
    splits it into one intent per field and reproduces the v2 mod byte-exact."""
    from cdumm.engine.v2_to_format3 import convert_iteminfo, verify

    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    gloves = [{
        "offset": 4582747,
        "original": "40771b000000000040771b000000000040771b0000000000",
        "patched":  "e803000000000000e803000000000000e803000000000000",
    }]
    rep = convert_iteminfo(gloves, body, header)
    assert not rep.unconverted, [w for _, w in rep.unconverted]
    assert rep.converted == 3
    assert {i["field"] for i in rep.intents} == {
        "cooltime", "unk_post_cooltime_a", "unk_post_cooltime_b"}
    assert all(i["new"] == 1000 and i["key"] == 1001250 for i in rep.intents)
    assert verify(rep, gloves, body, header), (
        "converted mod must reproduce the v2 mod byte-for-byte")
