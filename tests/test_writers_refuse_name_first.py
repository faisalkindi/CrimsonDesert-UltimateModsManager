"""Every table writer refuses a game 2.04 (name-first) table outright (#493).

Game 2.04 opens every record ``[u32 name_len][name][key]`` instead of
``[key][u32 name_len][name]`` (#458). The writers only know the key-first
layout. Before this check, 13 of the 15 refused 2.04.08 only because a
key-first read happened to fail on its bytes; stringinfo and storeinfo
wrote (at the right bytes, as it happens), and teaching four of the others
the header alone would have made them write the wrong bytes. Now each one
checks the table's own index first and refuses with a reason that names
the table and game 2.04.

No 2.04 table can be committed, so each test hands the writer a committed
pre-2.04 fixture and its 2.04-shaped copy (``to_name_first``: key and name
swapped in every record, nothing else). The same intent must write on the
fixture and be refused on the copy, with that reason, writing nothing.
Asserting the reason is what pins the check: on these copies most writers'
own reads already fail, and three (stringinfo, storeinfo, npcinfo) write
the same bytes they write on the fixture.
"""
from __future__ import annotations

import json
import logging
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from cdumm.engine.characterinfo_writer import build_characterinfo_changes
from cdumm.engine.dyecolorgroupinfo_writer import (
    DyecolorgroupinfoWriteRefused,
    build_dyecolorgroupinfo_changes,
)
from cdumm.engine.equipslotinfo_writer import (
    EquipslotWriteRefused,
    build_equipslotinfo_changes,
)
from cdumm.engine.format3_apply import _intents_to_v2_changes
from cdumm.engine.format3_handler import (
    Format3Intent,
    parse_format3_mod,
    validate_intents,
)
from cdumm.engine.interactioninfo_writer import build_interactioninfo_changes
from cdumm.engine.inventory_writer import build_inventory_changes
from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
from cdumm.engine.knowledgeinfo_writer import build_knowledgeinfo_changes
from cdumm.engine.multichangeinfo_writer import (
    build_multichangeinfo_changes,
    build_pabgh,
)
from cdumm.engine.npcinfo_writer import (
    NpcinfoWriteRefused,
    build_npcinfo_changes,
)
from cdumm.engine.skill_writer import _get_parser, build_skill_intent_change
from cdumm.engine.statusgroupinfo_writer import build_statusgroupinfo_changes
from cdumm.engine.statusinfo_writer import build_statusinfo_changes
from cdumm.engine.storeinfo_writer import (
    StoreinfoWriteRefused,
    build_storeinfo_changes,
)
from cdumm.engine.stringinfo_writer import build_stringinfo_changes, parse_pabgh
from cdumm.semantic.record_header import is_name_first, name_first_refusal
from tests.fixture_loaders import (
    load_vanilla110,
    load_vanilla113,
    load_vanilla115,
    load_vanilla_b24994088,
    load_vanilla_b25116796,
    to_name_first,
)

_DROPSET_MOD = (Path(__file__).resolve().parent / "fixtures" / "format3"
                / "dropsetinfo_5x_drops.json")


def _pair(load, table: str) -> tuple[bytes, bytes, bytes]:
    """``(fixture body, its name-first copy, the shared .pabgh)``."""
    body, header = load(f"{table}.pabgb"), load(f"{table}.pabgh")
    return body, to_name_first(body, header, table), header


def _intent(entry, key, field, new, op="set"):
    return Format3Intent(entry=entry, key=key, field=field, op=op, new=new)


def _warned(caplog, table: str) -> bool:
    return name_first_refusal(table) in caplog.text


# ── the check itself ─────────────────────────────────────────────────────

def test_the_refusal_names_the_table_and_game_2_04():
    why = name_first_refusal("storeinfo.pabgb")
    assert why.startswith("storeinfo.pabgb: ")
    assert "game 2.04 isn't supported" in why
    assert "nothing was written" in why


def test_the_check_leaves_unreadable_and_mixed_tables_to_the_writer():
    """False, not a refusal, when the order can't be decided: the writer
    then does exactly what it did before."""
    assert is_name_first(b"", b"", "storeinfo") is False
    assert is_name_first(b"\x00", b"\x00\x01", "skill") is False
    good = struct.pack("<I", 5) + b"Seven" + (7).to_bytes(4, "little") + b"\x00"
    legacy = (8).to_bytes(4, "little") + struct.pack("<I", 5) + b"Eight\x00"
    header = struct.pack("<H", 2) + struct.pack("<IIII", 7, 0, 8, len(good))
    assert is_name_first(good + legacy, header, "mixed") is False


# ── the fifteen writers ──────────────────────────────────────────────────

def test_inventory():
    body, nf, header = _pair(load_vanilla113, "inventory")
    intents = [_intent("CampWareHouse", 0, "default_slot_count", 1000)]
    changes, dropped = build_inventory_changes(body, header, intents)
    assert len(changes) == 1 and not dropped
    assert build_inventory_changes(nf, header, intents) == (
        [], [(intents[0], name_first_refusal("inventory.pabgb"))])


def test_multichangeinfo(caplog):
    # No multichangeinfo fixture is committed, so this is the synthetic
    # table tests/test_multichangeinfo_writer.py builds: key, name, NUL, 53
    # fixed bytes, then a u16-counted list of 30-byte elements.
    def record(key, name, elements):
        rec = struct.pack("<II", key, len(name)) + name + b"\x00" + bytes(53)
        rec += struct.pack("<H", len(elements))
        for item_info, count in elements:
            el = bytearray(30)
            struct.pack_into("<I", el, 0, item_info)
            struct.pack_into("<Q", el, 22, count)
            rec += bytes(el)
        return rec + b"\x07" * 4

    recs = [record(1, b"Arrow", []),
            record(2, b"Sword_Recipe", [(720001, 5), (720004, 3)]),
            record(3, b"Helm_Recipe", [(75001, 1)])]
    body = b"".join(recs)
    header = build_pabgh([(k + 1, sum(map(len, recs[:k])))
                          for k in range(len(recs))])
    nf = to_name_first(body, header, "multichangeinfo")
    intents = [("Sword_Recipe", 2, "fixed_material_data_list[0].count", 42)]
    changes, _companion = build_multichangeinfo_changes(body, header, intents)
    assert len(changes) == 1
    with caplog.at_level(logging.WARNING):
        assert build_multichangeinfo_changes(nf, header, intents) == ([], None)
    assert _warned(caplog, "multichangeinfo.pabgb")


def test_characterinfo():
    body, nf, header = _pair(load_vanilla113, "characterinfo")
    intents = [("Kliff", 1, "lookup_25", 3511542393)]
    assert len(build_characterinfo_changes(body, header, intents)) == 1
    refusals: list[str] = []
    assert build_characterinfo_changes(
        nf, header, intents, refusals_out=refusals) == []
    assert refusals == [name_first_refusal("characterinfo.pabgb")]


def test_iteminfo(caplog):
    # The key-first half is left to the iteminfo writer's own tests: a full
    # parse of this table takes about 18 s. The check runs before any parse.
    body, nf, header = _pair(load_vanilla110, "iteminfo")
    assert is_name_first(body, header, "iteminfo") is False
    intents = [_intent("", 2200, "max_stack_count", 4321)]
    with caplog.at_level(logging.WARNING):
        assert build_iteminfo_intent_change(
            nf, intents, vanilla_header=header) is None
    assert _warned(caplog, "iteminfo.pabgb")


def test_storeinfo():
    # Build 25116796: the stock layout storeinfo's detector falls back to on
    # a name-first table, so without the check this copy is written, exactly
    # as 2.04.08's stores are.
    body, nf, header = _pair(load_vanilla_b25116796, "storeinfo")
    intents = [_intent("", 3101, "stock_data_list[0].raw_c", 999)]
    changes, _companion = build_storeinfo_changes(body, header, intents)
    assert len(changes) == 1
    with pytest.raises(StoreinfoWriteRefused) as refused:
        build_storeinfo_changes(nf, header, intents)
    assert str(refused.value) == name_first_refusal("storeinfo.pabgb")


def test_stringinfo(caplog):
    body, nf, header = _pair(load_vanilla110, "stringinfo")
    key = sorted(parse_pabgh(header), key=lambda kv: kv[1])[10][0]
    intents = [("", key, "buffer", "name_first_marker")]
    changes, companion = build_stringinfo_changes(body, header, intents)
    assert len(changes) == 1 and companion is not None
    with caplog.at_level(logging.WARNING):
        assert build_stringinfo_changes(nf, header, intents) == ([], None)
    assert _warned(caplog, "stringinfo.pabgb")


def test_interactioninfo():
    body, nf, header = _pair(load_vanilla115, "interactioninfo")
    intents = [_intent("", 1000004, "interaction_pivot_list[0].raw_a",
                       1084227584)]                     # f32 5.0
    changes, dropped = build_interactioninfo_changes(body, header, intents)
    assert len(changes) == 1 and not dropped
    assert build_interactioninfo_changes(nf, header, intents) == (
        [], [(intents[0], name_first_refusal("interactioninfo.pabgb"))])


def test_statusgroupinfo():
    body, nf, header = _pair(load_vanilla115, "statusgroupinfo")
    intents = [_intent("StatOnActivateByItem", 1000006, "status_info_list[3]",
                       1000011)]
    changes, dropped = build_statusgroupinfo_changes(body, header, intents)
    assert changes and not dropped
    assert build_statusgroupinfo_changes(nf, header, intents) == (
        [], [(intents[0], name_first_refusal("statusgroupinfo.pabgb"))])


def test_knowledgeinfo():
    body, nf, header = _pair(load_vanilla115, "knowledgeinfo")
    intents = [_intent("", 1000092, "is_default", 1)]
    changes, dropped = build_knowledgeinfo_changes(body, header, intents)
    assert len(changes) == 1 and not dropped
    assert build_knowledgeinfo_changes(nf, header, intents) == (
        [], [(intents[0], name_first_refusal("knowledgeinfo.pabgb"))])


def test_statusinfo():
    body, nf, header = _pair(load_vanilla113, "statusinfo")
    intents = [_intent("MoveSpeedRate", 1000011, "stat_level_data[1]",
                       2_500_000_000)]
    changes, dropped = build_statusinfo_changes(body, header, intents)
    assert len(changes) == 1 and not dropped
    assert build_statusinfo_changes(nf, header, intents) == (
        [], [(intents[0], name_first_refusal("statusinfo.pabgb"))])


def test_npcinfo():
    body, nf, header = _pair(load_vanilla_b24994088, "npcinfo")
    intents = [SimpleNamespace(
        key=1000221, entry="", field="dye_color_group_data_list",
        op="array_append",
        new={"dye_color_group_key": 3693560950, "dye_target_key": 0})]
    changes, _companion = build_npcinfo_changes(body, header, intents)
    assert len(changes) == 1
    with pytest.raises(NpcinfoWriteRefused) as refused:
        build_npcinfo_changes(nf, header, intents)
    assert str(refused.value) == name_first_refusal("npcinfo.pabgb")


def test_dyecolorgroupinfo():
    body, nf, header = _pair(load_vanilla_b24994088, "dyecolorgroupinfo")
    intents = [SimpleNamespace(
        key=1586656, entry="", field="dye_color_data_list", op="array_append",
        new={"raw_color": 4278255513, "texture_lookup": 1008648})]
    changes, _companion = build_dyecolorgroupinfo_changes(body, header, intents)
    assert len(changes) == 1
    with pytest.raises(DyecolorgroupinfoWriteRefused) as refused:
        build_dyecolorgroupinfo_changes(nf, header, intents)
    assert str(refused.value) == name_first_refusal("dyecolorgroupinfo.pabgb")


def test_equipslotinfo():
    body, nf, header = _pair(load_vanilla115, "equipslotinfo")
    intents = [_intent("", 1, "entries[0].etl_hashes",
                       [1584411264, 2594511993, 2327795645, 1187101662])]
    changes, _companion = build_equipslotinfo_changes(body, header, intents)
    assert len(changes) == 1
    with pytest.raises(EquipslotWriteRefused) as refused:
        build_equipslotinfo_changes(nf, header, intents)
    assert str(refused.value) == name_first_refusal("equipslotinfo.pabgb")


def test_dropset(caplog):
    # dropset_writer gets one record's bytes and no .pabgh, so the table is
    # refused where its caller has the index: format3_apply's per-record
    # list-writer hook.
    body, nf, header = _pair(load_vanilla113, "dropsetinfo")
    _target, intents = parse_format3_mod(_DROPSET_MOD)
    supported = validate_intents("dropsetinfo.pabgb", intents).supported[:5]
    assert len(_intents_to_v2_changes(
        "dropsetinfo.pabgb", body, header, supported)) == 5
    with caplog.at_level(logging.WARNING):
        assert _intents_to_v2_changes(
            "dropsetinfo.pabgb", nf, header, supported) == []
    assert _warned(caplog, "dropsetinfo.pabgb")


def test_skill(caplog):
    body, nf, header = _pair(load_vanilla113, "skill")
    parser = _get_parser()
    if parser is None:
        pytest.skip("skill parser not loadable")
    skill = next(e for e in parser.parse_all(header, body)
                 if e.get("_useResourceStatList"))
    cost = skill["_useResourceStatList"][0]["value"]
    intents = [_intent(skill.get("name", ""), skill["key"],
                       "use_resource_stat_list[0].d",
                       (cost + 1) & (2 ** 64 - 1))]
    assert build_skill_intent_change(body, header, intents) is not None
    with caplog.at_level(logging.WARNING):
        assert build_skill_intent_change(nf, header, intents) is None
    assert _warned(caplog, "skill.pabgb")


# ── the generic schema-driven path ───────────────────────────────────────

def test_generic_path_refuses_before_payload_offset(tmp_path, monkeypatch,
                                                     caplog):
    """A field_schema entry on interactioninfo goes to the generic path,
    whose first step is _payload_offset read key first.

    This used is_blocked, placed by the schema walk. interactioninfo's
    schema is still in memory order, so the walk refuses every field of it
    now, whatever the header order; a field_schema entry still takes the
    generic path, through _payload_offset."""
    monkeypatch.setenv("CDUMM_FIELD_SCHEMA_ROOT", str(tmp_path))
    (tmp_path / "field_schema").mkdir()
    (tmp_path / "field_schema" / "interactioninfo.json").write_text(
        json.dumps({"probe_u8": {"rel_offset": 1, "type": "u8"}}),
        encoding="utf-8")
    body, nf, header = _pair(load_vanilla115, "interactioninfo")
    intents = [_intent("Gimmick_PickUp", 1000004, "probe_u8", 1)]
    assert len(_intents_to_v2_changes(
        "interactioninfo.pabgb", body, header, intents)) == 1
    with caplog.at_level(logging.WARNING):
        assert _intents_to_v2_changes(
            "interactioninfo.pabgb", nf, header, intents) == []
    assert _warned(caplog, "interactioninfo.pabgb")


def test_generic_field_schema_path_refuses_too(tmp_path, monkeypatch, caplog):
    """A table with no PABGB schema but a field_schema entry: the synthetic
    table tests/test_format3_no_schema_field_schema.py builds."""
    monkeypatch.setenv("CDUMM_FIELD_SCHEMA_ROOT", str(tmp_path))
    (tmp_path / "field_schema").mkdir()
    (tmp_path / "field_schema" / "synthnoschema.json").write_text(json.dumps(
        {"myField": {"tid": "0xAABBCCDD", "value_offset": 4, "type": "u32"}}),
        encoding="utf-8")
    body = (struct.pack("<II", 1, 5) + b"First" + b"\x00"
            + struct.pack("<IIH", 0xAABBCCDD, 0x42424242, 0x9999))
    header = struct.pack("<H", 1) + struct.pack("<II", 1, 0)
    nf = to_name_first(body, header, "synthnoschema")
    intents = [_intent("First", 1, "myField", 0xCAFEF00D)]
    assert len(_intents_to_v2_changes(
        "synthnoschema.pabgb", body, header, intents)) == 1
    with caplog.at_level(logging.WARNING):
        assert _intents_to_v2_changes(
            "synthnoschema.pabgb", nf, header, intents) == []
    assert _warned(caplog, "synthnoschema.pabgb")
