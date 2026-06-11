"""Name-only intent resolution in the Format 3 writers.

Audit finding 9 (2026-06-11): the Format 3 dialect allows key-omitted
intents (parser defaults key to 0) with the documented contract
"lookup by entry name first, key as fallback", but only the
multichangeinfo/characterinfo writers implemented it. The iteminfo,
skill, storeinfo and equipslotinfo writers now resolve by entry name
when the numeric key misses.

Synthetic tables are used throughout (the committed CD 1.10 iteminfo
fixture is 5.5 MB and slow to parse).
"""
from __future__ import annotations

import copy
import json
import struct
from dataclasses import dataclass
from typing import Any

from cdumm.engine.storeinfo_native_parser import (
    StockRecord,
    serialize_stock_list,
)
from cdumm.engine.equipslotinfo_writer import serialize_entry_payload


@dataclass
class _Intent:
    entry: str
    key: int
    field: str
    op: str = "set"
    new: Any = None
    old: Any = None


# ── Synthetic table builders ─────────────────────────────────────────

def build_store_table(entries):
    """entries: list of (key, name, [StockRecord, ...]). Returns
    (body, header) with the u16-keyed storeinfo layout: entry header
    (u16 key + u32 name_len + name + NUL), 43 scalar bytes, then the
    u32-count stock list."""
    body = bytearray()
    offs = {}
    for key, name, records in entries:
        offs[key] = len(body)
        nb = name.encode("utf-8")
        body += struct.pack("<H", key)
        body += struct.pack("<I", len(nb)) + nb + b"\x00"
        body += b"\x07" * 43
        body += serialize_stock_list(records)
    header = struct.pack("<H", len(entries))
    for key, _name, _records in entries:
        header += struct.pack("<H", key) + struct.pack("<I", offs[key])
    return bytes(body), bytes(header)


def build_equip_table(entries):
    """entries: list of (key, name, [(etl_count, hashes, fixed66)]).
    u32-keyed equipslotinfo layout with an empty footer + terminator."""
    body = bytearray()
    offs = {}
    footer = struct.pack("<I", 0) + struct.pack("<I", 0xB954D87C)
    for key, name, records in entries:
        offs[key] = len(body)
        nb = name.encode("utf-8")
        body += struct.pack("<I", key)
        body += struct.pack("<I", len(nb)) + nb + b"\x00"
        body += serialize_entry_payload(0, records, footer)
    header = struct.pack("<H", len(entries))
    for key, _name, _records in entries:
        header += struct.pack("<I", key) + struct.pack("<I", offs[key])
    return bytes(body), bytes(header)


# ── storeinfo ────────────────────────────────────────────────────────

def test_storeinfo_name_only_intent_applies():
    from cdumm.engine.storeinfo_writer import build_storeinfo_changes
    rec = StockRecord(body=1234)
    rec_vgap = bytearray(rec.vgap)
    struct.pack_into("<I", rec_vgap, 97 - 38, 1234)  # raw_q mirrors body
    rec.vgap = bytes(rec_vgap)
    body, header = build_store_table([
        (3101, "Store_Foo", [rec]),
        (3102, "Store_Bar", [rec]),
    ])
    # Key omitted (sentinel 0): must resolve via the entry name.
    intent = _Intent(entry="Store_Foo", key=0,
                     field="stock_data_list", new=[])
    changes, pabgh_change = build_storeinfo_changes(body, header, [intent])
    assert len(changes) == 1
    assert "3101" in changes[0]["label"]
    # The list shrank to zero records, so later offsets must shift.
    assert pabgh_change is not None


def test_storeinfo_unknown_name_and_key_skips():
    from cdumm.engine.storeinfo_writer import build_storeinfo_changes
    body, header = build_store_table([(3101, "Store_Foo", [])])
    intent = _Intent(entry="Store_Nope", key=0,
                     field="stock_data_list", new=[])
    changes, pabgh_change = build_storeinfo_changes(body, header, [intent])
    assert changes == [] and pabgh_change is None


# ── equipslotinfo ────────────────────────────────────────────────────

def test_equipslotinfo_name_only_intent_applies():
    from cdumm.engine.equipslotinfo_writer import (
        build_equipslotinfo_changes,
    )
    fixed = b"\x42" * 66
    body, header = build_equip_table([
        (701, "Equip_Foo", [(1, [0x11111111], fixed)]),
        (702, "Equip_Bar", [(1, [0x22222222], fixed)]),
    ])
    intent = _Intent(entry="Equip_Foo", key=0,
                     field="entries[0].etl_hashes", new=[1, 2, 3])
    changes, pabgh_change = build_equipslotinfo_changes(
        body, header, [intent])
    assert len(changes) == 1
    assert "701" in changes[0]["label"]
    patched = bytes.fromhex(changes[0]["patched"])
    # New payload carries 3 hashes.
    assert struct.unpack_from("<I", patched, 6)[0] == 3
    # List grew by 8 bytes, the second entry's offset must shift.
    assert pabgh_change is not None


# ── iteminfo (parser faked, synthetic per the audit instructions) ────

_ITEMS = [{"key": 5, "string_key": "Item_Foo", "max_stack_count": 1}]


def _fake_item_parse(body, record_offsets=None):
    return copy.deepcopy(_ITEMS)


def _fake_item_serialize(items, offsets_out=None):
    if offsets_out is not None:
        for i, it in enumerate(items):
            offsets_out[it["key"]] = i
    return json.dumps(items, sort_keys=True).encode("utf-8")


def test_iteminfo_name_only_intent_applies(monkeypatch):
    import cdumm.engine.iteminfo_writer as iw
    monkeypatch.setattr(iw, "parse_iteminfo_from_bytes", _fake_item_parse)
    monkeypatch.setattr(iw, "serialize_iteminfo", _fake_item_serialize)
    vanilla_body = _fake_item_serialize(copy.deepcopy(_ITEMS))

    intent = _Intent(entry="Item_Foo", key=0,
                     field="max_stack_count", new=99)
    change = iw.build_iteminfo_intent_change(vanilla_body, [intent])
    assert change is not None
    expected = copy.deepcopy(_ITEMS)
    expected[0]["max_stack_count"] = 99
    assert bytes.fromhex(change["patched"]) == \
        _fake_item_serialize(expected)


def test_iteminfo_unknown_name_and_key_returns_none(monkeypatch):
    import cdumm.engine.iteminfo_writer as iw
    monkeypatch.setattr(iw, "parse_iteminfo_from_bytes", _fake_item_parse)
    monkeypatch.setattr(iw, "serialize_iteminfo", _fake_item_serialize)
    vanilla_body = _fake_item_serialize(copy.deepcopy(_ITEMS))
    intent = _Intent(entry="Item_Nope", key=0,
                     field="max_stack_count", new=99)
    assert iw.build_iteminfo_intent_change(vanilla_body, [intent]) is None


# ── skill (vendored parser faked) ────────────────────────────────────

_SKILL_ENTRIES = [
    {"key": 7, "name": "Skill_Foo", "_useResourceStatList": [{"v": 1}]},
]


class _FakeSkillParser:
    def parse_all(self, header, body):
        return copy.deepcopy(_SKILL_ENTRIES)

    def serialize_all(self, entries):
        body = json.dumps(entries, sort_keys=True).encode("utf-8")
        synth = struct.pack("<H", len(entries))
        for e in entries:
            synth += struct.pack("<II", e["key"], 0)
        return synth, body


def _skill_fixture(monkeypatch):
    import cdumm.engine.skill_writer as sw
    fake = _FakeSkillParser()
    monkeypatch.setattr(sw, "_cached_module", fake)
    monkeypatch.setattr(sw, "_load_attempted", True)
    vanilla_header, vanilla_body = fake.serialize_all(
        copy.deepcopy(_SKILL_ENTRIES))
    return sw, vanilla_body, vanilla_header


def test_skill_name_only_intent_applies(monkeypatch):
    sw, vanilla_body, vanilla_header = _skill_fixture(monkeypatch)
    intent = _Intent(entry="Skill_Foo", key=0,
                     field="_useResourceStatList", new=[{"v": 2}])
    change = sw.build_skill_intent_change(
        vanilla_body, vanilla_header, [intent])
    assert change is not None
    expected = copy.deepcopy(_SKILL_ENTRIES)
    expected[0]["_useResourceStatList"] = [{"v": 2}]
    assert bytes.fromhex(change["patched"]) == \
        _FakeSkillParser().serialize_all(expected)[1]


def test_skill_unknown_name_and_key_returns_none(monkeypatch):
    sw, vanilla_body, vanilla_header = _skill_fixture(monkeypatch)
    intent = _Intent(entry="Skill_Nope", key=0,
                     field="_useResourceStatList", new=[{"v": 2}])
    assert sw.build_skill_intent_change(
        vanilla_body, vanilla_header, [intent]) is None
