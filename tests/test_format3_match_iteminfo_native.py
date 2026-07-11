"""Format 3 `match` on iteminfo must decode via the NATIVE parser.

The generic ``parse_records`` walker only reaches a handful of iteminfo
fields before it stops. Everything past them -- including
``equip_type_info``, which is exactly what the socket mods select on
(GitHub #272) -- comes back ``None``, so ``match`` compares against
nothing and silently expands to **zero** records. The mod then applies
cleanly and changes nothing, which is the worst possible failure: no
error, no warning, no effect.

The native parser decodes all 116 fields, so ``match`` on iteminfo routes
through it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cdumm.engine.format3_apply import (
    _decode_records_for_match,
    _expand_match_intents,
    _match_record_keys,
)
from cdumm.engine.format3_handler import Format3Intent


def _live_iteminfo():
    env = os.environ.get("CDUMM_VANILLA_ITEMINFO_DIR")
    dirs = ([Path(env)] if env else []) + [
        Path(__file__).parent / "fixtures" / "iteminfo"]
    for d in dirs:
        body, header = d / "iteminfo.pabgb", d / "iteminfo.pabgh"
        if body.exists() and header.exists():
            return body.read_bytes(), header.read_bytes()
    return None


def _require_live():
    pair = _live_iteminfo()
    if pair is None:
        pytest.skip("vanilla iteminfo.pabgb/.pabgh not available")
    return pair


def test_non_iteminfo_tables_still_use_the_generic_walker(monkeypatch):
    """Routing is iteminfo-only -- every other table is untouched."""
    called = {}

    def fake_parse_records(name, body, header):
        called["name"] = name
        return {1: {"_key": 1, "_name": "x"}}

    monkeypatch.setattr(
        "cdumm.engine.format3_apply.parse_records", fake_parse_records)
    out = _decode_records_for_match("dropsetinfo", b"body", b"hdr")
    assert called["name"] == "dropsetinfo"
    assert out == {1: {"_key": 1, "_name": "x"}}


def test_native_decode_failure_falls_back_to_generic(monkeypatch):
    """A native decode that blows up must not lose the match entirely."""
    def boom(body, header):
        raise RuntimeError("nope")

    monkeypatch.setattr(
        "cdumm.engine.format3_apply._decode_iteminfo_for_match", boom)
    monkeypatch.setattr(
        "cdumm.engine.format3_apply.parse_records",
        lambda n, b, h: {7: {"_key": 7, "_name": "fallback"}})
    out = _decode_records_for_match("iteminfo", b"body", b"hdr")
    assert out == {7: {"_key": 7, "_name": "fallback"}}


def test_empty_native_decode_falls_back_to_generic(monkeypatch):
    monkeypatch.setattr(
        "cdumm.engine.format3_apply._decode_iteminfo_for_match",
        lambda b, h: {})
    monkeypatch.setattr(
        "cdumm.engine.format3_apply.parse_records",
        lambda n, b, h: {9: {"_key": 9, "_name": "fallback"}})
    assert _decode_records_for_match("iteminfo", b"b", b"h") == {
        9: {"_key": 9, "_name": "fallback"}}


# ── live table ──────────────────────────────────────────────────────────

def test_native_decode_exposes_the_fields_match_needs():
    body, header = _require_live()
    records = _decode_records_for_match("iteminfo", body, header)
    assert records, "native decode produced no records"

    sample = next(iter(records.values()))
    # The generic walker reaches ~5 fields; the native one reaches ~116.
    assert len(sample) > 100, f"only {len(sample)} fields decoded"

    # The specific fields the socket mods select on.
    assert "equip_type_info" in sample
    assert "drop_default_data" in sample
    assert "_key" in sample and "_name" in sample

    # equip_type_info must be a real value on equipment, not None -- that
    # None is precisely what made match silently select nothing.
    equipment = [r for r in records.values() if r.get("equip_type_info")]
    assert len(equipment) > 3000, f"only {len(equipment)} equipment records"


def test_match_on_equip_type_info_selects_records():
    """The #272 shape: select a family of equip_type_info values in one
    intent. Under the generic walker this expanded to 0 records."""
    body, header = _require_live()
    records = _decode_records_for_match("iteminfo", body, header)

    # Pick two real equip types off the live table.
    types = []
    for rec in records.values():
        t = rec.get("equip_type_info")
        if t and t not in types:
            types.append(t)
        if len(types) == 2:
            break
    assert len(types) == 2

    single = _match_record_keys(records, {"equip_type_info": types[0]})
    assert single, "match on a real equip_type_info selected nothing"

    # any-of (the list form from PR #271) must union the two.
    both = _match_record_keys(records, {"equip_type_info": types})
    assert set(single).issubset(set(both))
    assert len(both) >= len(single)
    for key in both:
        assert records[key]["equip_type_info"] in types


def test_expand_match_emits_one_set_intent_per_matched_record():
    body, header = _require_live()
    records = _decode_records_for_match("iteminfo", body, header)
    equip_type = next(r["equip_type_info"] for r in records.values()
                      if r.get("equip_type_info"))

    intent = Format3Intent(
        entry="", key=0, field="max_stack_count", op="set", new=99,
        old=None, match={"equip_type_info": equip_type},
    )
    out = _expand_match_intents(
        "gamedata/binary__/client/bin/iteminfo.pabgb",
        body, header, [intent],
    )
    assert out, "match expanded to zero intents"
    expected = _match_record_keys(records, {"equip_type_info": equip_type})
    assert len(out) == len(expected)
    for got in out:
        assert got.op == "set"
        assert got.match is None            # fully resolved
        assert got.field == "max_stack_count"
        assert got.new == 99
        assert got.key in expected
        # carries the record's real name so the writer resolves it like a
        # hand-authored single-record intent
        assert got.entry == records[got.key]["_name"]


def test_non_match_intents_pass_through_untouched():
    body, header = _require_live()
    plain = Format3Intent(entry="Notepad", key=1, field="max_stack_count",
                          op="set", new=5, old=None, match=None)
    out = _expand_match_intents(
        "gamedata/binary__/client/bin/iteminfo.pabgb", body, header, [plain])
    assert out == [plain]
