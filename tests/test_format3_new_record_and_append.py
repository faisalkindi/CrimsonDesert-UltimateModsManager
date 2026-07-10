"""Format 3 ``new_record`` (template-based, via the clone engine) and the
``array_append`` recognition + actionable skip.

new_record with a ``source_key``/``template_key`` bases the new record on an
existing one and routes through the append-only, self-checked clone engine.
Without a template it parses but is skipped with guidance (building a valid
record from a bare field list needs a per-table serializer). ``array_append``
is recognized and skipped with a "use a set with the full list" message.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.format3_apply import _build_record_ops_change_for_target
from cdumm.engine.format3_handler import (
    _parse_intents_block,
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


def _pair(entries):
    body = bytearray()
    ko = []
    for eid, name, key, foo, bar in entries:
        ko.append((key, len(body)))
        nb = name.encode("utf-8")
        body += struct.pack("<II", eid, len(nb)) + nb + b"\x00" + \
            struct.pack("<IIH", key, foo, bar)
    header = bytearray(struct.pack("<H", len(entries)))
    for k, o in ko:
        header += struct.pack("<II", k, o)
    return bytes(body), bytes(header)


# ── new_record parsing ──────────────────────────────────────────────


def test_parse_new_record_with_template():
    i = _parse_intents_block([{
        "op": "new_record", "source_key": 100, "new_key": 500,
        "new_name": "Fresh", "patches": [{"field": "_bar", "new": 9}]}])[0]
    assert i.op == "new_record"
    assert i.clone == {"source_key": 100, "new_key": 500,
                       "patches": [{"field": "_bar", "new": 9}],
                       "new_name": "Fresh"}


def test_parse_new_record_template_key_alias():
    i = _parse_intents_block(
        [{"op": "new_record", "template_key": 100, "new_key": 500}])[0]
    assert i.clone is not None and i.clone["source_key"] == 100


def test_parse_new_record_without_template_has_no_clone():
    i = _parse_intents_block(
        [{"op": "new_record", "new_key": 500}])[0]
    assert i.op == "new_record" and i.clone is None


def test_parse_new_record_requires_new_key():
    with pytest.raises(ValueError):
        _parse_intents_block([{"op": "new_record", "source_key": 1}])


# ── new_record apply (template routes through the clone engine) ──────


def test_new_record_creates_from_template(monkeypatch):
    _inject(monkeypatch)
    body, header = _pair([(1, "Base", 100, 0xAAAA1111, 0x55)])
    intent = _parse_intents_block([{
        "op": "new_record", "source_key": 100, "new_key": 500,
        "new_name": "Made", "patches": [{"field": "_bar", "new": 0x99}]}])[0]
    bc, comp, n = _build_record_ops_change_for_target(
        "synthtest.pabgb", body, header, [intent])
    assert n == 1 and bc is not None and comp is not None
    r = parse_records("synthtest", bytes.fromhex(bc["patched"]),
                      bytes.fromhex(comp["patched"]))
    assert set(r) == {100, 500}
    assert r[500]["_name"] == "Made"
    assert r[500]["_foo"] == 0xAAAA1111        # copied from template
    assert r[500]["_bar"] == 0x99               # patched


# ── new_record validation ───────────────────────────────────────────


def test_validate_new_record_with_template_supported(monkeypatch):
    _inject(monkeypatch, verified_fields=frozenset({"_foo", "_bar"}))
    i = _parse_intents_block([{
        "op": "new_record", "source_key": 100, "new_key": 500,
        "patches": [{"field": "_bar", "new": 9}]}])[0]
    v = validate_intents("synthtest.pabgb", [i])
    assert i in v.supported, v.skipped


def test_validate_new_record_without_template_skipped(monkeypatch):
    _inject(monkeypatch)
    i = _parse_intents_block([{"op": "new_record", "new_key": 500}])[0]
    v = validate_intents("synthtest.pabgb", [i])
    assert not v.supported
    assert "source_key" in v.skipped[0][1]


def test_validate_new_record_no_schema_skipped():
    i = _parse_intents_block(
        [{"op": "new_record", "source_key": 1, "new_key": 2}])[0]
    v = validate_intents("definitelynotatable.pabgb", [i])
    assert not v.supported and "needs a decoded schema" in v.skipped[0][1]


# ── array_append recognition + actionable skip ──────────────────────


def test_array_append_skipped_with_guidance(monkeypatch):
    _inject(monkeypatch)
    i = _parse_intents_block(
        [{"op": "array_append", "entry": "x", "field": "_bar", "new": 1}])[0]
    v = validate_intents("synthtest.pabgb", [i])
    assert not v.supported
    reason = v.skipped[0][1]
    assert "array_append" in reason and "set" in reason
