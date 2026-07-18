"""Format 3 ``clone_record`` — parse, engine-core byte-exactness, the
parse-back self-check gate, validation, and apply-pipeline wiring.

``clone_record`` deep-copies a source record to a new key + optional name
and applies field patches to the copy. It is append-only (existing records
are never touched) and every clone is parse-back self-checked before it is
committed, so corruption is impossible: a clone the engine can't do safely
returns None and is skipped.

Uses the same synthetic all-flat table the binary-roundtrip test uses.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.format3_apply import _build_record_ops_change_for_target
from cdumm.engine.format3_handler import (
    Format3Intent,
    _parse_intents_block,
    apply_clone_to_pabgb_bytes,
    validate_intents,
)
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import FieldSpec, TableSchema, parse_records


def _inject(monkeypatch, verified_fields=None):
    fields = [
        FieldSpec(name="_key", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_foo", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_bar", stream_size=2,
                  field_type="direct_u16", struct_fmt="H"),
    ]
    schema = TableSchema(table_name="synthtest", fields=fields,
                         verified_fields=verified_fields)
    original = parser_mod._loaded_schemas
    if original is None:
        parser_mod._load_schemas()
        original = parser_mod._loaded_schemas
    new_cache = dict(original or {})
    new_cache["synthtest"] = schema
    monkeypatch.setattr(parser_mod, "_loaded_schemas", new_cache)
    return schema


def _build_entry(entry_id, name, key_value, foo, bar):
    name_bytes = name.encode("utf-8")
    head = struct.pack("<II", entry_id, len(name_bytes))
    payload = struct.pack("<IIH", key_value, foo, bar)
    return head + name_bytes + b"\x00" + payload


def _build_pabgb_pair(entries):
    body = bytearray()
    keys_offsets = []
    for entry_id, name, key, foo, bar in entries:
        offset = len(body)
        body.extend(_build_entry(entry_id, name, key, foo, bar))
        keys_offsets.append((key, offset))
    header = bytearray(struct.pack("<H", len(entries)))
    for k, o in keys_offsets:
        header.extend(struct.pack("<II", k, o))
    return bytes(body), bytes(header)


# ── Parsing ─────────────────────────────────────────────────────────


def test_parse_accepts_clone_record():
    intents = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 500,
        "new_name": "Copy", "patches": [{"field": "_bar", "new": 9}],
    }])
    assert len(intents) == 1
    i = intents[0]
    assert i.op == "clone_record"
    assert i.clone == {
        "source_key": 100, "new_key": 500,
        "patches": [{"field": "_bar", "new": 9}], "new_name": "Copy",
    }
    assert i.entry == "" and i.field == "" and i.new is None


def test_parse_clone_defaults_empty_patches_and_no_name():
    i = _parse_intents_block(
        [{"op": "clone_record", "source_key": 1, "new_key": 2}])[0]
    assert i.clone["patches"] == []
    assert "new_name" not in i.clone


@pytest.mark.parametrize("bad", [
    {"op": "clone_record", "new_key": 2},                       # no source
    {"op": "clone_record", "source_key": 1},                    # no new_key
    {"op": "clone_record", "source_key": "x", "new_key": 2},    # non-int
    {"op": "clone_record", "source_key": 1, "new_key": 2,
     "patches": "nope"},                                        # patches not list
    {"op": "clone_record", "source_key": 1, "new_key": 2,
     "patches": [{"field": "_bar"}]},                           # patch no new
    {"op": "clone_record", "source_key": 1, "new_key": 2,
     "new_name": 7},                                            # non-str name
])
def test_parse_rejects_bad_clone(bad):
    with pytest.raises(ValueError):
        _parse_intents_block([bad])


# ── Engine core: byte-exact + self-check ────────────────────────────


def test_clone_no_patch_is_faithful_copy(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xAAAA1111, 0x55),
        (2, "Second", 200, 0xBBBB2222, 0x66),
    ])
    res = apply_clone_to_pabgb_bytes(
        "synthtest", body, header, {"source_key": 100, "new_key": 500})
    assert res is not None
    new_body, new_header = res
    # append-only: original body prefix untouched
    assert new_body[:len(body)] == body
    recs = parse_records("synthtest", new_body, new_header)
    assert set(recs) == {100, 200, 500}
    # the clone equals the source on every data field
    assert recs[500]["_foo"] == recs[100]["_foo"] == 0xAAAA1111
    assert recs[500]["_bar"] == recs[100]["_bar"] == 0x55
    # originals unchanged
    assert recs[200]["_foo"] == 0xBBBB2222


def test_clone_with_patch_applies_only_to_copy(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xAAAA1111, 0x55),
    ])
    res = apply_clone_to_pabgb_bytes(
        "synthtest", body, header,
        {"source_key": 100, "new_key": 500,
         "patches": [{"field": "_bar", "new": 0x99}]})
    assert res is not None
    new_body, new_header = res
    recs = parse_records("synthtest", new_body, new_header)
    assert recs[500]["_bar"] == 0x99          # patched on the copy
    assert recs[500]["_foo"] == 0xAAAA1111     # copied unchanged
    assert recs[100]["_bar"] == 0x55           # source untouched


def test_clone_new_name(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([(1, "First", 100, 7, 0x55)])
    res = apply_clone_to_pabgb_bytes(
        "synthtest", body, header,
        {"source_key": 100, "new_key": 500, "new_name": "Cloned"})
    assert res is not None
    new_body, new_header = res
    recs = parse_records("synthtest", new_body, new_header)
    assert recs[500]["_name"] == "Cloned"
    assert recs[100]["_name"] == "First"


def test_clone_key_collision_refused(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([
        (1, "First", 100, 7, 0x55),
        (2, "Second", 200, 8, 0x66),
    ])
    # new_key 200 already exists -> refuse (return None), never overwrite
    assert apply_clone_to_pabgb_bytes(
        "synthtest", body, header,
        {"source_key": 100, "new_key": 200}) is None


def test_clone_missing_source_refused(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([(1, "First", 100, 7, 0x55)])
    assert apply_clone_to_pabgb_bytes(
        "synthtest", body, header,
        {"source_key": 999, "new_key": 500}) is None


def test_clone_index_grows_by_one_entry(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([(1, "First", 100, 7, 0x55)])
    res = apply_clone_to_pabgb_bytes(
        "synthtest", body, header, {"source_key": 100, "new_key": 500})
    assert res is not None
    _new_body, new_header = res
    ks, offs = parser_mod.parse_pabgh_index(new_header, "synthtest")
    assert ks == 4
    assert set(offs) == {100, 500}
    assert offs[100] == 0                 # original offset preserved
    assert offs[500] == len(body)         # clone appended at the end


def test_clone_unknown_table_refused():
    body, header = _build_pabgb_pair([(1, "First", 100, 7, 0x55)])
    assert apply_clone_to_pabgb_bytes(
        "notarealtable", body, header,
        {"source_key": 100, "new_key": 500}) is None


# ── Validation ──────────────────────────────────────────────────────


def test_validate_clone_supported_with_verified_patch(monkeypatch):
    _inject(monkeypatch, verified_fields=frozenset({"_foo", "_bar"}))
    i = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 500,
        "patches": [{"field": "_bar", "new": 9}]}])[0]
    v = validate_intents("synthtest.pabgb", [i])
    assert i in v.supported, v.skipped


def test_validate_clone_unverified_patch_skipped(monkeypatch):
    _inject(monkeypatch, verified_fields=frozenset({"_bar"}))  # _foo unverified
    i = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 500,
        "patches": [{"field": "_foo", "new": 9}]}])[0]
    v = validate_intents("synthtest.pabgb", [i])
    assert not v.supported
    assert "not a verified field" in v.skipped[0][1]


def test_validate_clone_unknown_patch_skipped(monkeypatch):
    _inject(monkeypatch, verified_fields=frozenset({"_foo", "_bar"}))
    i = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 500,
        "patches": [{"field": "_nope", "new": 9}]}])[0]
    v = validate_intents("synthtest.pabgb", [i])
    assert not v.supported
    assert "plain schema field" in v.skipped[0][1]


def test_validate_clone_no_schema_skipped():
    i = _parse_intents_block([{
        "op": "clone_record", "source_key": 1, "new_key": 2}])[0]
    v = validate_intents("definitelynotatable.pabgb", [i])
    assert not v.supported
    assert "needs a decoded schema" in v.skipped[0][1]


def test_validate_clone_gear_stat_patch_on_iteminfo():
    # gear_stat[...] on iteminfo is a byte-exact structural stat overwrite,
    # so clone patches may target it (real: clone a weapon, buff its damage)
    # — but only when the gear-stat feature is present in the build.
    from cdumm.engine.format3_handler import _gear_stats_available
    i = _parse_intents_block([{
        "op": "clone_record", "source_key": 1000080, "new_key": 1000090,
        "patches": [{"field": "gear_stat[1000000]", "new": 500}]}])[0]
    v = validate_intents("iteminfo.pabgb", [i])
    if _gear_stats_available():
        assert i in v.supported, v.skipped
    else:
        assert not v.supported
        assert "gear-stat editing isn't available" in v.skipped[0][1]


def test_validate_clone_rejects_gear_stat_on_non_iteminfo(monkeypatch):
    _inject(monkeypatch, verified_fields=frozenset({"_foo", "_bar"}))
    i = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 500,
        "patches": [{"field": "gear_stat[5]", "new": 1}]}])[0]
    v = validate_intents("synthtest.pabgb", [i])
    assert not v.supported   # gear_stat is only allowed on iteminfo


# ── Apply-pipeline wiring ───────────────────────────────────────────


def test_build_clone_change_emits_whole_table_change(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([(1, "First", 100, 0xAAAA1111, 0x55)])
    intent = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 500,
        "patches": [{"field": "_bar", "new": 0x99}]}])[0]
    body_change, companion, n = _build_record_ops_change_for_target(
        "synthtest.pabgb", body, header, [intent])
    assert n == 1
    assert body_change is not None and companion is not None
    assert body_change["offset"] == 0
    assert body_change["original"] == body.hex()
    assert body_change["patched"] != body.hex()
    # the emitted body parses back with the clone present + patched
    new_body = bytes.fromhex(body_change["patched"])
    new_header = bytes.fromhex(companion["patched"])
    recs = parse_records("synthtest", new_body, new_header)
    assert recs[500]["_bar"] == 0x99
    assert recs[500]["_foo"] == 0xAAAA1111
    assert recs[100]["_bar"] == 0x55


def test_build_clone_change_all_refused_returns_none(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([
        (1, "First", 100, 7, 0x55),
        (2, "Second", 200, 8, 0x66),
    ])
    # collision on the only clone -> nothing applied
    intent = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 200}])[0]
    body_change, companion, n = _build_record_ops_change_for_target(
        "synthtest.pabgb", body, header, [intent])
    assert (body_change, companion, n) == (None, None, 0)


def test_build_clone_change_composes_with_set(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([(1, "First", 100, 0xAAAA1111, 0x55)])
    clone = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 500}])[0]
    # a plain set on the ORIGINAL record, alongside the clone
    plain = Format3Intent(entry="First", key=100, field="_bar",
                          op="set", new=0x77)
    body_change, companion, n = _build_record_ops_change_for_target(
        "synthtest.pabgb", body, header, [clone, plain])
    assert n == 1
    new_body = bytes.fromhex(body_change["patched"])
    new_header = bytes.fromhex(companion["patched"])
    recs = parse_records("synthtest", new_body, new_header)
    assert recs[100]["_bar"] == 0x77      # set applied to original
    assert recs[500]["_bar"] == 0x55      # clone kept source value
