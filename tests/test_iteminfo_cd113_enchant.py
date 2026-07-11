"""CD 1.13 iteminfo: SubItem tag 17 + _enchantDataList.

Two coupled wire changes landed in CD 1.13 (GitHub #272, gear stats):

  * ``SubItem`` tag 17 is a *None* sentinel carrying no u32 payload.
  * ``ItemInfo`` grew ``_enchantDataList`` right after _dropDefaultData.

The pre-1.13 reader spends the same 7 bytes as
``tag(1) + u32 value(4) + svc(1) + use(1)`` where the truth is
``tag(1) + svc(1) + use(1) + u32 enchant count(4)``. Identical width --
which is exactly why non-equipment (enchant count 0, so those 4 bytes are
zero either way) round-tripped byte-exact and never looked broken, while
equipment (count is almost always 11) desynced. It also explains why
fixing the sentinel *alone* destroyed non-equipment: that drops the u32
without putting the enchant count back. The two changes are inseparable,
so these tests pin them together.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import cdumm.engine.iteminfo_native_parser as nat
from cdumm.engine.iteminfo_native_parser import (
    _ITEM_FIELDS_CD113_ENCHANT,
    _Reader,
    _Writer,
    _read_DropDefaultData_CD113,
    _read_EnchantData_CD113,
    _write_DropDefaultData_CD113,
    _write_EnchantData_CD113,
    detect_iteminfo_layout,
    parse_iteminfo_from_bytes,
    serialize_iteminfo,
)


def _u16(v):
    return v.to_bytes(2, "little")


def _u32(v):
    return v.to_bytes(4, "little")


def _u64(v):
    return v.to_bytes(8, "little")


# ── DropDefaultData: tag 17 carries no payload ───────────────────────────

def test_subitem_tag_17_consumes_no_u32():
    # u16 level | empty carray | empty carray | tag 17 | svc | use
    raw = _u16(3) + _u32(0) + _u32(0) + bytes([17]) + bytes([0]) + bytes([1])
    r = _Reader(raw, 0, rec_end=len(raw))
    ddd = _read_DropDefaultData_CD113(r)
    assert r.pos == len(raw)               # nothing left over
    assert ddd["default_sub_item"] == {"type_id": 17, "value": None}
    assert ddd["socket_valid_count"] == 0
    assert ddd["use_socket"] == 1
    w = _Writer()
    _write_DropDefaultData_CD113(w, ddd)
    assert bytes(w.buf) == raw


def test_subitem_real_tag_still_carries_its_u32():
    raw = _u16(0) + _u32(0) + _u32(0) + bytes([3]) + _u32(9) + bytes([2, 1])
    r = _Reader(raw, 0, rec_end=len(raw))
    ddd = _read_DropDefaultData_CD113(r)
    assert r.pos == len(raw)
    assert ddd["default_sub_item"] == {"type_id": 3, "value": 9}
    w = _Writer()
    _write_DropDefaultData_CD113(w, ddd)
    assert bytes(w.buf) == raw


def test_sockets_survive_the_new_ddd():
    """#272 edits add_socket_material_item_list -- it must still decode."""
    raw = (_u16(0) + _u32(0)
           + _u32(1) + _u32(1) + _u64(200)      # one SocketMaterialItem
           + bytes([17, 5, 1]))
    r = _Reader(raw, 0, rec_end=len(raw))
    ddd = _read_DropDefaultData_CD113(r)
    assert ddd["add_socket_material_item_list"] == [{"item": 1, "value": 200}]
    assert ddd["socket_valid_count"] == 5
    assert ddd["use_socket"] == 1
    w = _Writer()
    _write_DropDefaultData_CD113(w, ddd)
    assert bytes(w.buf) == raw


# ── EnchantData = pre-1.13 shape + the u32 CD 1.12 added ─────────────────

def _enchant_bytes(level, *, prices=(), buffs=(), static_lvl=(), effect=0):
    b = _u16(level)
    b += _u32(0) * 3                                    # 3 empty stat lists
    b += _u32(len(static_lvl))
    for stat, chg in static_lvl:                        # EnchantLevelChange 5B
        b += _u32(stat) + chg.to_bytes(1, "little", signed=True)
    b += _u32(len(prices))
    for key, price in prices:                           # ItemPriceInfo 20B
        b += _u32(key) + _u64(price) + _u32(0) + _u32(key)
    b += _u32(len(buffs))
    for buff, lvl in buffs:                             # EquipmentBuff 8B
        b += _u32(buff) + _u32(lvl)
    b += _u32(effect)                                   # the 1.12 addition
    return b


def test_enchant_data_roundtrips_and_has_the_trailing_u32():
    raw = _enchant_bytes(0, prices=[(1000003, 2000)], effect=7)
    r = _Reader(raw, 0, rec_end=len(raw))
    ed = _read_EnchantData_CD113(r)
    assert r.pos == len(raw)
    assert ed["level"] == 0
    assert ed["buy_price_list"][0]["key"] == 1000003
    assert ed["buy_price_list"][0]["price"]["price"] == 2000
    assert ed["item_effect_info"] == 7
    w = _Writer()
    _write_EnchantData_CD113(w, ed)
    assert bytes(w.buf) == raw


def test_enchant_element_sizes_are_pinned():
    """Sizes are the whole ballgame: a wrong element width still
    round-trips byte-exact while silently mis-assigning every field after
    it. Pin them numerically.

    2 level + 3*4 empty + (4 + 1*5) + (4 + 2*20) + (4 + 1*8) + 4 = 83
    """
    raw = _enchant_bytes(
        1,
        static_lvl=[(1000007, 1)],
        prices=[(1000002, 8000), (11, 4000)],
        buffs=[(1000009, 0)],
        effect=0,
    )
    assert len(raw) == 83
    r = _Reader(raw, 0, rec_end=len(raw))
    ed = _read_EnchantData_CD113(r)
    assert r.pos == 83
    assert len(ed["enchant_stat_data"]["stat_list_static_level"]) == 1
    assert len(ed["buy_price_list"]) == 2
    assert len(ed["equip_buffs"]) == 1
    w = _Writer()
    _write_EnchantData_CD113(w, ed)
    assert bytes(w.buf) == raw


def test_carray_guard_bounds_by_record_not_body():
    """A desynced count must be rejected against the *record*, not the
    5.9MB body -- otherwise it allocates ~500k dicts before failing."""
    data = b"\xff\xff\x00\x00" + b"\x00" * 4096      # count = 65535
    r = _Reader(data, 0, rec_end=16)                 # only 16 bytes of record
    with pytest.raises(ValueError, match="exceeds"):
        r.carray(_Reader.u32)


# ── layout selection ─────────────────────────────────────────────────────

def test_more_specific_layout_wins_a_tie():
    """A sample drawing only non-equipment records makes the enchant-blind
    layout tie with the enchant one. The tie must go to the enchant layout
    or all 3151 equipment records get carried opaque."""
    labels = [lbl for lbl, _f in nat._ITEM_LAYOUTS]
    assert labels.index("cd113_enchant") > labels.index("cd113_prefab_relocated")


def test_detect_claims_nothing_when_nothing_roundtrips():
    assert detect_iteminfo_layout(b"", []) is None
    assert detect_iteminfo_layout(b"\x00" * 4, [0]) is None


# ── real-game integration (skips when the game isn't installed) ──────────

def _live_iteminfo():
    env = os.environ.get("CDUMM_VANILLA_ITEMINFO_DIR")
    dirs = ([Path(env)] if env else []) + [
        Path(__file__).parent / "fixtures" / "iteminfo"]
    for d in dirs:
        body, header = d / "iteminfo.pabgb", d / "iteminfo.pabgh"
        if body.exists() and header.exists():
            return body.read_bytes(), header.read_bytes()
    return None


def test_live_1_13_table_decodes_fully_and_roundtrips_byte_exact():
    pair = _live_iteminfo()
    if pair is None:
        pytest.skip("vanilla iteminfo.pabgb/.pabgh not available")
    body, header = pair

    count = int.from_bytes(header[:2], "little")
    starts = sorted(
        int.from_bytes(header[2 + i * 8 + 4:2 + i * 8 + 8], "little")
        for i in range(count))

    fields = detect_iteminfo_layout(body, starts)
    if fields is not _ITEM_FIELDS_CD113_ENCHANT:
        pytest.skip("installed game is not the CD 1.13 layout")

    items = parse_iteminfo_from_bytes(body, starts, fields=fields)

    # Not one record may fall back to opaque.
    assert [i for i in items if "_opaque_record" in i] == []

    # Equipment must actually decode (it was 0 before this fix).
    assert sum(1 for i in items if i.get("equip_type_info")) > 3000

    # ORACLE: enchant tiers are levels 0,1,2,...,N-1. A wrong-but-same-size
    # layout round-trips byte-exact and would pass every check but this one.
    for it in items:
        tiers = it.get("enchant_data_list", [])
        assert [t["level"] for t in tiers] == list(range(len(tiers)))

    assert serialize_iteminfo(items, fields=fields) == body
