"""Per-intent refusal degradation + shape gates (audit finding 8).

A single refused/malformed intent used to abort the entire multi-mod
batch for a table:

* storeinfo/equipslot: a writer refusal (StoreinfoWriteRefused /
  EquipslotWriteRefused) dropped EVERY batched intent. The dispatch
  now probes per-intent and keeps the survivors.
* iteminfo/skill: a malformed ``new`` value passed straight into the
  parsed dicts and blew up serialization later, killing the whole
  batch. The writers now shape-check per-intent and skip+count.
"""
from __future__ import annotations

import copy
import struct
from dataclasses import dataclass
from typing import Any

import pytest

from cdumm.engine.format3_apply import _build_with_per_intent_refusals
from cdumm.engine.storeinfo_native_parser import StockRecord
from cdumm.engine.storeinfo_writer import (
    StoreinfoWriteRefused,
    build_storeinfo_changes,
)

from tests.test_writer_name_lookup import (
    _FakeSkillParser,
    _SKILL_ENTRIES,
    _fake_item_parse,
    _fake_item_serialize,
    _ITEMS,
    build_store_table,
)


@dataclass
class _Intent:
    entry: str
    key: int
    field: str
    op: str = "set"
    new: Any = None
    old: Any = None


# ── Helper-level behavior ────────────────────────────────────────────

class _Refused(ValueError):
    pass


def test_helper_keeps_survivors_and_reports_dropped():
    bad = _Intent("Bad", 2, "f", new=[])
    good_a = _Intent("GoodA", 1, "f", new=[])
    good_b = _Intent("GoodB", 3, "f", new=[])

    def build(body, header, intents):
        if any(i.entry == "Bad" for i in intents):
            raise _Refused("entry Bad cannot be placed")
        return ([{"offset": 0, "label": i.entry} for i in intents],
                None)

    changes, companion, dropped = _build_with_per_intent_refusals(
        build, _Refused, b"", b"", [good_a, bad, good_b])
    assert [c["label"] for c in changes] == ["GoodA", "GoodB"]
    assert companion is None
    assert len(dropped) == 1
    assert dropped[0][0] is bad
    assert "cannot be placed" in dropped[0][1]


def test_helper_full_batch_success_is_single_build():
    calls = []

    def build(body, header, intents):
        calls.append(len(intents))
        return [{"offset": 0}], None

    changes, _companion, dropped = _build_with_per_intent_refusals(
        build, _Refused, b"", b"", [_Intent("A", 1, "f")])
    assert changes and not dropped
    assert calls == [1]


def test_helper_all_refused_returns_empty():
    def build(body, header, intents):
        raise _Refused("nope")

    changes, companion, dropped = _build_with_per_intent_refusals(
        build, _Refused, b"", b"",
        [_Intent("A", 1, "f"), _Intent("B", 2, "f")])
    assert changes == [] and companion is None
    assert len(dropped) == 2


def test_helper_non_refusal_exception_propagates():
    def build(body, header, intents):
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _build_with_per_intent_refusals(
            build, _Refused, b"", b"", [_Intent("A", 1, "f")])


# ── storeinfo integration: one refused intent, one good one ──────────

def test_storeinfo_refusal_degrades_per_intent():
    rec = StockRecord(body=1234)
    vgap = bytearray(rec.vgap)
    struct.pack_into("<I", vgap, 97 - 38, 1234)
    rec.vgap = bytes(vgap)
    body, header = build_store_table([
        (3101, "Store_Foo", [rec]),
        (3102, "Store_Bar", [rec]),
    ])
    good = _Intent("Store_Foo", 3101, "stock_data_list", new=[])
    # New record with a non-zero UNMAPPED interior field: the writer
    # refuses it (placement unknown, wrong placement corrupts).
    bad = _Intent("Store_Bar", 3102, "stock_data_list", new=[
        {"value": {"payload": {"body": 999}, "disc": 5}},
    ])
    changes, _companion, dropped = _build_with_per_intent_refusals(
        build_storeinfo_changes, StoreinfoWriteRefused,
        body, header, [good, bad])
    assert len(changes) == 1
    assert "3101" in changes[0]["label"]
    assert len(dropped) == 1
    assert dropped[0][0] is bad
    assert "disc" in dropped[0][1]


# ── expand-level: dispatch warning names dropped intents + mods ──────

def test_expand_dispatch_degrades_per_intent_and_names_mods(tmp_path):
    import json as _json
    import cdumm.engine.format3_apply as f3a
    from cdumm.storage.database import Database

    rec = StockRecord(body=1234)
    vgap = bytearray(rec.vgap)
    struct.pack_into("<I", vgap, 97 - 38, 1234)
    rec.vgap = bytes(vgap)
    body, header = build_store_table([
        (3101, "Store_Foo", [rec]),
        (3102, "Store_Bar", [rec]),
    ])

    def _mod_file(name, key, entry, new):
        p = tmp_path / name
        p.write_text(_json.dumps({
            "format": 3,
            "target": "storeinfo.pabgb",
            "intents": [{"entry": entry, "key": key,
                         "field": "stock_data_list", "new": new}],
        }), encoding="utf-8")
        return p

    good_src = _mod_file("good.field.json", 3101, "Store_Foo", [])
    bad_src = _mod_file("bad.field.json", 3102, "Store_Bar", [
        {"value": {"payload": {"body": 999}, "disc": 5}},
    ])

    db = Database(tmp_path / "t.db")
    db.initialize()
    try:
        db.connection.execute(
            "INSERT INTO mods (name, mod_type, enabled, priority, "
            "json_source) VALUES ('GoodMod', 'paz', 1, 1, ?)",
            (str(good_src),))
        db.connection.execute(
            "INSERT INTO mods (name, mod_type, enabled, priority, "
            "json_source) VALUES ('BadMod', 'paz', 1, 2, ?)",
            (str(bad_src),))
        db.connection.commit()

        aggregated: dict = {}
        warnings: list[str] = []
        f3a.expand_format3_into_aggregated(
            aggregated, {}, db,
            vanilla_extractor=lambda t: (
                (body, header) if t == "storeinfo.pabgb" else None),
            warnings_out=warnings)

        # GoodMod's change landed despite BadMod's refusal.
        changes = aggregated.get("storeinfo.pabgb", [])
        assert len(changes) == 1
        assert "3101" in changes[0]["label"]
        # The warning names the dropped intent's mod and entry.
        joined = "\n".join(warnings)
        assert "BadMod" in joined
        assert "Store_Bar" in joined
        assert "still applied" in joined
    finally:
        db.close()


# ── iteminfo per-intent shape gate ───────────────────────────────────

def test_iteminfo_bad_shape_skipped_good_intent_survives(monkeypatch):
    import cdumm.engine.iteminfo_writer as iw
    monkeypatch.setattr(iw, "parse_iteminfo_from_bytes", _fake_item_parse)
    monkeypatch.setattr(iw, "serialize_iteminfo", _fake_item_serialize)
    vanilla_body = _fake_item_serialize(copy.deepcopy(_ITEMS))

    bad = _Intent("Item_Foo", 5, "max_stack_count",
                  new=[{"not": "an int"}])  # list for an int field
    good = _Intent("Item_Foo", 5, "max_stack_count", new=77)
    change = iw.build_iteminfo_intent_change(vanilla_body, [bad, good])
    assert change is not None
    expected = copy.deepcopy(_ITEMS)
    expected[0]["max_stack_count"] = 77
    assert bytes.fromhex(change["patched"]) == \
        _fake_item_serialize(expected)
    assert "1 bad value shape" in change["label"]


def test_iteminfo_additive_list_requires_element_kind(monkeypatch):
    import cdumm.engine.iteminfo_writer as iw
    monkeypatch.setattr(iw, "parse_iteminfo_from_bytes", _fake_item_parse)
    monkeypatch.setattr(iw, "serialize_iteminfo", _fake_item_serialize)
    vanilla_body = _fake_item_serialize(copy.deepcopy(_ITEMS))

    # item_tag_list is carray_u32 on disk: a list of dicts must skip.
    bad = _Intent("Item_Foo", 5, "item_tag_list", new=[{"a": 1}])
    assert iw.build_iteminfo_intent_change(vanilla_body, [bad]) is None
    # While a list of ints is accepted (additive write).
    good = _Intent("Item_Foo", 5, "item_tag_list", new=[10, 20])
    assert iw.build_iteminfo_intent_change(
        vanilla_body, [good]) is not None


# ── skill per-intent shape gate ──────────────────────────────────────

def test_skill_bad_shape_skipped_good_intent_survives(monkeypatch):
    import cdumm.engine.skill_writer as sw
    fake = _FakeSkillParser()
    monkeypatch.setattr(sw, "_cached_module", fake)
    monkeypatch.setattr(sw, "_load_attempted", True)
    vanilla_header, vanilla_body = fake.serialize_all(
        copy.deepcopy(_SKILL_ENTRIES))

    bad = _Intent("Skill_Foo", 7, "_useResourceStatList",
                  new=[1, 2, 3])  # ints where dicts are required
    good = _Intent("Skill_Foo", 7, "_useResourceStatList",
                   new=[{"v": 9}])
    change = sw.build_skill_intent_change(
        vanilla_body, vanilla_header, [bad, good])
    assert change is not None
    expected = copy.deepcopy(_SKILL_ENTRIES)
    expected[0]["_useResourceStatList"] = [{"v": 9}]
    assert bytes.fromhex(change["patched"]) == \
        fake.serialize_all(expected)[1]
    assert "1 bad value shape" in change["label"]


def test_skill_bufflevellist_shape_requires_list_of_lists(monkeypatch):
    import cdumm.engine.skill_writer as sw
    assert sw._shape_ok("_buffLevelList", [[{"b": 1}], []])
    assert not sw._shape_ok("_buffLevelList", [{"b": 1}])
    assert not sw._shape_ok("_buffLevelList", "nope")
