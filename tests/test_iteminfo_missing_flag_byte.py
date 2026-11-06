"""iteminfo reads five u8 flags after discard_offset_y, not four (GitHub #492).

The layout used to read four u8 flags after ``discard_offset_y``, then the
three u32 item refs, then a 5-byte filler (``unk_pre_pattern_a`` u32 +
``unk_pre_pattern_b`` u8). The records hold FIVE u8 flags there, then the
three u32s, then 4 bytes. So every field from
``hide_from_inventory_on_pop_item`` to ``convert_item_info_by_drop_npc`` was
read one byte early -- the name ``is_wild`` sat on the tower-shield flag --
and the filler absorbed the byte.

WHY A ROUND TRIP NEVER SAW IT
-----------------------------
Both readings tile every record of every committed fixture and re-serialize
byte-exact, and every field outside the run decodes the same under both.
The only difference is which name sits on which byte. So what pins the fix
is VALUES, from two oracles that need no game install:

* Item names. Read four-wide, ``is_shield_item`` was 1 on 27 records and not
  one of them a shield (21 quest items, 5 keys and a pear), and ``is_wild``
  was 1 on 22 of the 23 tower shields and on nothing else.
* The table's own keys. The three u32s are item references (the vendored
  crimson_rs stubs type them ``ItemKey``). Read correctly, every nonzero
  value is the key of a record in the same table. Read a byte early, not
  one nonzero ``unpacked_item_info`` or ``convert_item_info_by_drop_npc``
  value is.

The field names are 2.04's: its exe names every byte of the run
(``_discardAttachTerrain``, ``_hideFromInventoryOnPopItem``,
``_isShieldItem``, ``_isTowerShieldItem``, ``_isWild``, ...). On a 2.04.08
copy those fields equal the corrected reading, key by key, on every key each
fixture shares with it, bar 5 values listed in #492. CI has no 2.04 data, so
that check lives in the issue rather than here.
"""
from __future__ import annotations

from functools import cache

import pytest

import cdumm.engine.iteminfo_native_parser as IP
from cdumm.semantic.parser import parse_pabgh_index
from tests.fixture_loaders import (
    has_vanilla110,
    has_vanilla113,
    has_vanilla116,
    has_vanilla_b24773079,
    has_vanilla_b24934353,
    has_vanilla_b25116796,
    load_vanilla110,
    load_vanilla113,
    load_vanilla116,
    load_vanilla_b24773079,
    load_vanilla_b24934353,
    load_vanilla_b25116796,
)

#: Every layout reads exactly this from discard_offset_y on.
_RUN = [
    ("discard_offset_y", "f32"),
    ("discard_attach_terrain", "u8"),
    ("hide_from_inventory_on_pop_item", "u8"),
    ("is_shield_item", "u8"),
    ("is_tower_shield_item", "u8"),
    ("is_wild", "u8"),
    ("packed_item_info", "u32"),
    ("unpacked_item_info", "u32"),
    ("convert_item_info_by_drop_npc", "u32"),
    ("unk_pre_pattern", "u32"),
    ("pattern_description_data_list", "carray"),
]
_FLAGS = [name for name, kind in _RUN if kind == "u8"]
_REFS = ["packed_item_info", "unpacked_item_info",
         "convert_item_info_by_drop_npc"]

#: fixture -> (loader, has-check, the layout detect_iteminfo_layout picks,
#: records decoded, records carried opaque). All of it is what master read
#: before the fix: one byte moved from the filler to the new flag, so no
#: record changed width and no table changed layout.
_FIXTURES = {
    "vanilla110": (load_vanilla110, has_vanilla110, "default", 6419, 64),
    "vanilla113": (load_vanilla113, has_vanilla113, "cd113_enchant", 6508, 0),
    "vanilla116": (load_vanilla116, has_vanilla116, "cd116", 6567, 14),
    "vanilla_b24773079": (load_vanilla_b24773079, has_vanilla_b24773079,
                          "cd116b", 6573, 0),
    "vanilla_b24934353": (load_vanilla_b24934353, has_vanilla_b24934353,
                          "cd20", 6810, 0),
    "vanilla_b25116796": (load_vanilla_b25116796, has_vanilla_b25116796,
                          "cd20", 6813, 0),
}

_need_new = pytest.mark.skipif(
    not has_vanilla_b25116796("iteminfo.pabgb"),
    reason="b25116796 iteminfo fixture absent")


@cache
def _parse(fixture: str):
    load = _FIXTURES[fixture][0]
    body = load("iteminfo.pabgb")
    header = load("iteminfo.pabgh")
    _ks, offs = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offs.values())
    fields = IP.detect_iteminfo_layout(body, starts)
    items = IP.parse_iteminfo_from_bytes(body, starts, fields=fields)
    return body, header, starts, fields, items


def _by_name() -> dict:
    items = _parse("vanilla_b25116796")[4]
    return {it["string_key"]: it for it in items
            if not it.get("_opaque_record")}


def _set(field: str) -> list[str]:
    return sorted(n for n, it in _by_name().items() if it[field] == 1)


def _layouts():
    yield "default", IP._ITEM_FIELDS
    for label, fields in IP._ITEM_LAYOUTS:
        if fields is not None:
            yield label, fields


# ── the layout ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "label,fields", list(_layouts()), ids=[lb for lb, _f in _layouts()])
def test_every_layout_reads_five_flags_then_a_four_byte_filler(label, fields):
    """Every variant is derived from ``_ITEM_FIELDS``, so every one has to
    carry the byte. A variant built some other way would silently keep
    reading the run one byte early."""
    specs = [tuple(spec[:2]) for spec in fields]
    at = specs.index(("discard_offset_y", "f32"))
    assert specs[at:at + len(_RUN)] == _RUN
    assert not {"unk_pre_pattern_a", "unk_pre_pattern_b"} & {
        spec[0] for spec in fields}


@pytest.mark.slow
@pytest.mark.parametrize("fixture", list(_FIXTURES))
def test_every_fixture_still_round_trips_byte_exact(fixture):
    """Same layout, same records decoded, every byte back, on all six.

    A round trip proves the bytes are preserved, not that they are
    understood; both readings pass it. It is here to show the correction
    costs nothing, and to pin the filler at 0 on every decoded record. Read
    5 wide, its first byte was 122 on 15 records of every fixture, which was
    the top byte of ``convert_item_info_by_drop_npc``.
    """
    _load, have, layout, decoded, opaque = _FIXTURES[fixture]
    if not have("iteminfo.pabgb"):
        pytest.skip(f"{fixture} iteminfo fixture absent")
    body, _h, _s, fields, items = _parse(fixture)
    label = next((lb for lb, f in IP._ITEM_LAYOUTS if f is fields), "?")
    assert label == layout
    dec = [it for it in items if not it.get("_opaque_record")]
    assert (len(dec), len(items) - len(dec)) == (decoded, opaque)
    assert not [it["key"] for it in dec if it.get("_tail_slack")]
    assert IP.serialize_iteminfo(items, fields=fields) == body
    assert {it["unk_pre_pattern"] for it in dec} == {0}
    for flag in _FLAGS:
        assert {it[flag] for it in dec} <= {0, 1}, flag


# ── what each byte is, by the items that carry it (buildid 25116796) ────

@_need_new
def test_the_tower_shield_flag_is_set_on_tower_shields_only():
    """Read four-wide, this byte was ``is_wild``."""
    rec = _by_name()
    on = _set("is_tower_shield_item")
    assert len(on) == 22
    assert all("TowerShield" in n for n in on)
    towers = sorted(n for n in rec if "TowerShield" in n)
    assert [n for n in towers if n not in on] == [
        "Soulknight_Sprit_OneHandTowerShield"]
    assert rec["Pavis_OneHandTowerShield"]["is_tower_shield_item"] == 1
    assert rec["Hexe_Soldier_OneHandShield"]["is_tower_shield_item"] == 0
    # and the real is_wild is set on none of them
    assert not [n for n in towers if rec[n]["is_wild"]]


@_need_new
def test_the_shield_flag_is_set_on_shields():
    """Read four-wide, this byte was ``is_tower_shield_item``."""
    rec = _by_name()
    on = _set("is_shield_item")
    assert len(on) == 78
    assert [n for n in on if "Shield" not in n] == ["Animal_Spirit"]
    assert all(rec[n]["is_shield_item"] == 1
               for n in rec if "TowerShield" in n)
    assert rec["Hexe_Soldier_OneHandShield"]["is_shield_item"] == 1


@_need_new
def test_the_hide_flag_is_set_on_quest_items_and_keys_not_shields():
    """Read four-wide, this byte was ``is_shield_item``."""
    on = _set("hide_from_inventory_on_pop_item")
    assert len(on) == 27
    assert sum(n.startswith("Quest_") for n in on) == 21
    assert not [n for n in on if "Shield" in n]


@_need_new
def test_the_new_byte_is_set_on_the_fish():
    """2.04 calls it ``_discardAttachTerrain``. Read four-wide, this byte
    was ``hide_from_inventory_on_pop_item``."""
    on = _set("discard_attach_terrain")
    assert len(on) == 48
    assert all(n.startswith("Food_") for n in on)


@_need_new
def test_the_three_refs_are_keys_of_this_table():
    """The oracle for the u32s. Read a byte early, Trade_Cheese_03 gave
    256,441,600 (0x0F48FD00): its is_wild byte, then the low three bytes of
    the real reference, which is 1,001,725 -- Trade_Cheese_PackedInVehicle.
    """
    items = _parse("vanilla_b25116796")[4]
    keys = {it["key"] for it in items}
    dec = [it for it in items if not it.get("_opaque_record")]
    for ref, nonzero in zip(_REFS, (59, 59, 456)):
        vals = [it[ref] for it in dec if it[ref]]
        assert len(vals) == nonzero, ref
        assert all(v in keys for v in vals), ref
    rec = _by_name()
    cheese = rec["Trade_Cheese_03"]
    assert cheese["packed_item_info"] == 1_001_725
    assert cheese["is_wild"] == 0
    assert rec["Trade_Cheese_PackedInVehicle"]["key"] == 1_001_725


# ── every name now reaches the byte it describes ────────────────────────

@_need_new
def test_format3_edits_land_on_the_byte_their_name_describes():
    """Both spellings of the new byte resolve, and an edit to a flag or a
    ref changes that field and nothing else, through the function Apply
    uses. Before the fix, ``_isTowerShieldItem = 0`` on Pavis cleared its
    shield flag, ``packed_item_info`` on this quest item overwrote its
    ``is_wild``, and ``_discardAttachTerrain`` was skipped as unknown.
    """
    from cdumm.engine import format3_apply as fa
    from cdumm.engine.format3_handler import Format3Intent, validate_intents
    from cdumm.engine.iteminfo_writer import _resolve_field_name

    body, header, starts, fields, _items = _parse("vanilla_b25116796")
    rec = _by_name()
    fish = rec["Food_Carp"]
    assert fish["discard_attach_terrain"] == 1
    for spelling in ("discard_attach_terrain", "_discardAttachTerrain"):
        assert _resolve_field_name(spelling, fish) == "discard_attach_terrain"
    assert _resolve_field_name("_isWild", fish) == "is_wild"
    assert _resolve_field_name("unk_pre_pattern_a", fish) is None

    pavis = rec["Pavis_OneHandTowerShield"]
    bremer = rec["Quest_BrokenVisionPro_Bremer"]
    assert (bremer["is_wild"], bremer["packed_item_info"]) == (1, 0)
    intents = [
        Format3Intent(entry="Pavis_OneHandTowerShield", key=pavis["key"],
                      field="_isTowerShieldItem", op="set", new=0),
        Format3Intent(entry="Quest_BrokenVisionPro_Bremer",
                      key=bremer["key"], field="packed_item_info", op="set",
                      new=1_001_725),
        Format3Intent(entry="Food_Carp", key=fish["key"],
                      field="_discardAttachTerrain", op="set", new=0),
    ]
    checked = validate_intents("iteminfo.pabgb", intents)
    assert len(checked.supported) == 3, checked.skipped
    changes = fa._intents_to_v2_changes("iteminfo.pabgb", body, header,
                                        intents)
    patched = bytearray(body)
    for ch in changes:
        new = bytes.fromhex(ch["patched"])
        patched[ch["offset"]:ch["offset"] + len(new)] = new
    assert len(patched) == len(body)
    after = {it["key"]: it for it in IP.parse_iteminfo_from_bytes(
        bytes(patched), starts, fields=fields)}
    p, b, f = after[pavis["key"]], after[bremer["key"]], after[fish["key"]]
    assert (p["is_tower_shield_item"], p["is_shield_item"]) == (0, 1)
    assert (b["packed_item_info"], b["is_wild"]) == (1_001_725, 1)
    assert f["discard_attach_terrain"] == 0
    # one flag byte, three bytes of the u32, one flag byte -- nothing else
    assert sum(x != y for x, y in zip(patched, body)) == 5


@_need_new
def test_v2_edits_convert_to_the_name_of_the_byte_they_touch():
    """v2_to_format3 names bytes from the parser's layout, so it moves with
    it. On master, the same edit to Pavis's tower-shield byte converted to
    ``is_wild``, and the 4-byte write over ``packed_item_info`` was refused
    as landing between named fields."""
    from cdumm.engine.v2_to_format3 import _field_spans, convert_iteminfo

    body, header, _s, fields, _items = _parse("vanilla_b25116796")
    pavis = _by_name()["Pavis_OneHandTowerShield"]
    _ks, offs = parse_pabgh_index(header, "iteminfo")
    spans = _field_spans(pavis, fields, offs[pavis["key"]])
    at = spans["discard_offset_y"][1]
    assert list(body[at:at + 5]) == [0, 0, 1, 1, 0]
    changes = [{"offset": at + i, "original": f"{body[at + i]:02x}",
                "patched": f"{body[at + i] ^ 1:02x}"} for i in range(5)]
    changes.append({"offset": at + 5, "original": body[at + 5:at + 9].hex(),
                    "patched": (1_001_725).to_bytes(4, "little").hex()})
    rep = convert_iteminfo(changes, body, header)
    assert not rep.unconverted
    assert {i["field"]: i["new"] for i in rep.intents} == {
        "discard_attach_terrain": 1,
        "hide_from_inventory_on_pop_item": 1,
        "is_shield_item": 0,
        "is_tower_shield_item": 0,
        "is_wild": 1,
        "packed_item_info": 1_001_725,
    }


@_need_new
def test_the_game_data_grid_reads_the_run_where_the_parser_does():
    """The grid walks the schema's ItemInfo order, not the native parser,
    so the order needs the byte too. It had no filler at all, so from
    ``_patternDescriptionDataList`` on it read 5 bytes early: on master,
    ``_enableAlertSystemToUI`` matched the native parser on 19 of the 6,788
    records it reached here. schema_verify's value check never saw that,
    because ``_camel_to_snake`` can't map that name, so it is compared
    here directly."""
    from cdumm.semantic.parser import parse_records_display

    body, header, _s, _f, items = _parse("vanilla_b25116796")
    native = {it["key"]: it for it in items if not it.get("_opaque_record")}
    grid = parse_records_display("iteminfo", body, header)
    pairs = {
        "_discardOffsetY": "discard_offset_y",
        "_discardAttachTerrain": "discard_attach_terrain",
        "_hideFromInventoryOnPopItem": "hide_from_inventory_on_pop_item",
        "_isShieldItem": "is_shield_item",
        "_isTowerShieldItem": "is_tower_shield_item",
        "_isWild": "is_wild",
        "_packedItemInfo": "packed_item_info",
        "_unpackedItemInfo": "unpacked_item_info",
        "_convertItemInfoByDropNPC": "convert_item_info_by_drop_npc",
        "_unk_pre_pattern": "unk_pre_pattern",
        "_lookDetailGameAdviceInfoWrapper":
            "look_detail_game_advice_info_wrapper",
        "_lookDetailMissionInfo": "look_detail_mission_info",
        "_enableAlertSystemToUI": "enable_alert_system_to_ui",
        "_isSaveGameDataAtUseItem": "is_save_game_data_at_use_item",
        "_isLogoutAtUseItem": "is_logout_at_use_item",
        "_sharedCoolTimeGroupNameHash": "shared_cool_time_group_name_hash",
    }
    reached = [k for k, row in grid.items()
               if "_sharedCoolTimeGroupNameHash" in row]
    assert len(reached) == 6803
    for col, field in pairs.items():
        bad = [k for k in reached if grid[k][col] != native[k][field]]
        assert not bad, f"{col} differs from the native parser on {len(bad)}"
    by_name = {row["_name"]: row for row in grid.values()}
    hexe = by_name["Hexe_Soldier_OneHandShield"]
    assert (hexe["_isShieldItem"], hexe["_isTowerShieldItem"]) == (1, 0)
    assert by_name["Trade_Cheese_03"]["_packedItemInfo"] == 1_001_725
