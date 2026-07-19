"""inventory.pabgb slot-count writer (DMM "max inventory" mods).

DMM Mod Builder sets default_slot_count / max_slot_count / need_save_slot_count
on named inventory records. The three counts are consecutive u16 before each
record's ``28 80 02 00 00`` marker (need_save@-6, default@-4, max@-2). These
tests prove the writer applies them byte-exact and refuses bad input.
"""
from __future__ import annotations

import json
import sqlite3
import struct

import pytest

from cdumm.engine.format3_handler import Format3Intent, validate_intents
from cdumm.engine.inventory_writer import (
    _find_record_start,
    _MARK,
    build_inventory_changes,
)

from tests.fixture_loaders import has_vanilla113, load_vanilla113

FIXTURE = "inventory.pabgb"

pytestmark = pytest.mark.skipif(
    not has_vanilla113(FIXTURE),
    reason="inventory fixture not present")


def _intent(entry, field, new):
    return Format3Intent(entry=entry, key=0, field=field, op="set", new=new)


def _apply(body, changes):
    out = bytearray(body)
    for c in changes:
        off = c["offset"]
        orig = bytes.fromhex(c["original"])
        assert out[off:off + len(orig)] == orig, "change must anchor"
        out[off:off + len(orig)] = bytes.fromhex(c["patched"])
    return bytes(out)


def _read_slots(body, name):
    rs = _find_record_start(body, name)
    m = body.find(_MARK, rs)
    return {
        "need_save_slot_count": struct.unpack_from("<H", body, m - 6)[0],
        "default_slot_count": struct.unpack_from("<H", body, m - 4)[0],
        "max_slot_count": struct.unpack_from("<H", body, m - 2)[0],
    }


def test_sets_slot_counts_byte_exact():
    body = load_vanilla113("inventory.pabgb")
    header = load_vanilla113("inventory.pabgh")
    intents = [
        _intent("CampWareHouse", "default_slot_count", 1000),
        _intent("CampWareHouse", "max_slot_count", 1000),
        _intent("CampWareHouse", "need_save_slot_count", 0),
    ]
    changes, dropped = build_inventory_changes(body, header, intents)
    assert not dropped, dropped
    assert len(changes) == 1                       # one record touched

    modified = _apply(body, changes)
    assert len(modified) == len(body)              # length-preserving
    assert _read_slots(modified, "CampWareHouse") == {
        "need_save_slot_count": 0,
        "default_slot_count": 1000,
        "max_slot_count": 1000,
    }
    # every other record byte-identical: only CampWareHouse's 6-byte block moved
    diff = [j for j in range(len(body)) if body[j] != modified[j]]
    assert diff and len(diff) <= 6


def test_two_records_two_changes():
    body = load_vanilla113("inventory.pabgb")
    header = load_vanilla113("inventory.pabgh")
    intents = [
        _intent("WareHouse", "default_slot_count", 1000),   # substring of CampWareHouse
        _intent("Character", "max_slot_count", 700),
    ]
    changes, dropped = build_inventory_changes(body, header, intents)
    assert not dropped, dropped
    assert len(changes) == 2
    modified = _apply(body, changes)
    assert _read_slots(modified, "WareHouse")["default_slot_count"] == 1000
    assert _read_slots(modified, "Character")["max_slot_count"] == 700
    # WareHouse must NOT have hit the CampWareHouse record
    assert _read_slots(modified, "CampWareHouse")["default_slot_count"] == \
        _read_slots(body, "CampWareHouse")["default_slot_count"]


def test_refuses_unknown_record():
    body = load_vanilla113("inventory.pabgb")
    header = load_vanilla113("inventory.pabgh")
    changes, dropped = build_inventory_changes(
        body, header, [_intent("NotARealBag", "default_slot_count", 100)])
    assert changes == []
    assert "no inventory record" in dropped[0][1]


def test_refuses_non_slot_field():
    body = load_vanilla113("inventory.pabgb")
    header = load_vanilla113("inventory.pabgh")
    changes, dropped = build_inventory_changes(
        body, header, [_intent("Character", "some_other_field", 1)])
    assert changes == []
    assert "not an inventory slot field" in dropped[0][1]


def test_refuses_out_of_u16_range():
    body = load_vanilla113("inventory.pabgb")
    header = load_vanilla113("inventory.pabgh")
    changes, dropped = build_inventory_changes(
        body, header, [_intent("Character", "default_slot_count", 70000)])
    assert changes == []
    assert "u16 range" in dropped[0][1]


def test_noop_when_value_matches_vanilla():
    body = load_vanilla113("inventory.pabgb")
    header = load_vanilla113("inventory.pabgh")
    cur = _read_slots(body, "Character")
    intents = [_intent("Character", f, cur[f]) for f in cur]
    changes, dropped = build_inventory_changes(body, header, intents)
    assert changes == []
    assert not dropped


def test_validate_intents_accepts_inventory_slot_fields():
    """No CDUMM schema for inventory -> must route via LIST_WRITERS so the
    writer sees it instead of a schema-less skip."""
    intents = [Format3Intent(entry="CampWareHouse", key=0,
                             field="default_slot_count", op="set", new=1000)]
    v = validate_intents("inventory.pabgb", intents)
    assert len(v.supported) == 1, v
    assert not v.skipped, v


def test_end_to_end_through_dispatch(tmp_path):
    """Format 3 inventory mod -> validate -> whole-table dispatch -> writer ->
    byte-exact change in the aggregator."""
    from cdumm.engine.format3_apply import expand_format3_into_aggregated

    body = load_vanilla113("inventory.pabgb")
    header = load_vanilla113("inventory.pabgh")
    doc = {"format": 3, "format_minor": 1, "modinfo": {"title": "inv"},
           "targets": [{"file": "inventory.pabgb", "intents": [
               {"entry": "CampWareHouse", "field": "default_slot_count",
                "op": "set", "new": 1000},
               {"entry": "CampWareHouse", "field": "max_slot_count",
                "op": "set", "new": 1000}]}]}
    jp = tmp_path / "inv.json"
    jp.write_text(json.dumps(doc), encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
                 "enabled INTEGER, json_source TEXT, priority INTEGER, "
                 "mod_type TEXT)")
    conn.execute("CREATE TABLE mod_config (mod_id INTEGER, custom_values TEXT)")
    conn.execute("INSERT INTO mods VALUES (1, 'Inv', 1, ?, 5, 'paz')",
                 (str(jp),))
    conn.commit()
    db = type("DB", (), {"connection": conn})()

    aggregated: dict = {}
    signatures: dict = {}
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: (body, header)
        if gf == "inventory.pabgb" else None)

    changes = aggregated.get("inventory.pabgb") or []
    assert len(changes) == 1, aggregated
    modified = _apply(body, changes)
    assert len(modified) == len(body)
    assert _read_slots(modified, "CampWareHouse") == {
        "need_save_slot_count": 0,
        "default_slot_count": 1000,
        "max_slot_count": 1000,
    }
