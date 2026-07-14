"""Format 3 ``delete_record`` — parse, byte-exact engine core with the
parse-back self-check, validation, and apply-pipeline wiring.

Deleting a record rebuilds the body from the survivors and reindexes the
``.pabgh`` (offsets after the removed record shift). Every survivor must
decode byte-identically and the deleted key must be gone — verified before
anything is committed; else the delete is refused.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.format3_apply import _build_record_ops_change_for_target
from cdumm.engine.format3_handler import (
    _parse_intents_block,
    apply_delete_to_pabgb_bytes,
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


def _entry(entry_id, name, key, foo, bar):
    nb = name.encode("utf-8")
    return struct.pack("<II", entry_id, len(nb)) + nb + b"\x00" + \
        struct.pack("<IIH", key, foo, bar)


def _pair(entries):
    body = bytearray()
    ko = []
    for eid, name, key, foo, bar in entries:
        ko.append((key, len(body)))
        body += _entry(eid, name, key, foo, bar)
    header = bytearray(struct.pack("<H", len(entries)))
    for k, o in ko:
        header += struct.pack("<II", k, o)
    return bytes(body), bytes(header)


# ── Parsing ─────────────────────────────────────────────────────────


def test_parse_accepts_delete_record():
    i = _parse_intents_block(
        [{"op": "delete_record", "key": 200, "entry": "Second"}])[0]
    assert i.op == "delete_record" and i.key == 200


@pytest.mark.parametrize("bad", [
    {"op": "delete_record"},                     # no key
    {"op": "delete_record", "key": "x"},         # non-int
    {"op": "delete_record", "key": True},        # bool
])
def test_parse_rejects_bad_delete(bad):
    with pytest.raises(ValueError):
        _parse_intents_block([bad])


# ── Engine core ─────────────────────────────────────────────────────


def test_delete_middle_record(monkeypatch):
    _inject(monkeypatch)
    entries = [
        (1, "First", 100, 0xAAAA1111, 0x55),
        (2, "Second", 200, 0xBBBB2222, 0x66),
        (3, "Third", 300, 0xCCCC3333, 0x77),
    ]
    body, header = _pair(entries)
    removed = _entry(*entries[1])
    res = apply_delete_to_pabgb_bytes("synthtest", body, header, 200)
    assert res is not None
    nb, nh = res
    recs = parse_records("synthtest", nb, nh)
    assert set(recs) == {100, 300}
    assert recs[100]["_foo"] == 0xAAAA1111 and recs[100]["_bar"] == 0x55
    assert recs[300]["_foo"] == 0xCCCC3333 and recs[300]["_bar"] == 0x77
    assert len(nb) == len(body) - len(removed)


def test_delete_first_and_last(monkeypatch):
    _inject(monkeypatch)
    entries = [
        (1, "First", 100, 1, 0x11),
        (2, "Second", 200, 2, 0x22),
        (3, "Third", 300, 3, 0x33),
    ]
    body, header = _pair(entries)
    nb, nh = apply_delete_to_pabgb_bytes("synthtest", body, header, 100)
    assert set(parse_records("synthtest", nb, nh)) == {200, 300}
    nb2, nh2 = apply_delete_to_pabgb_bytes("synthtest", body, header, 300)
    assert set(parse_records("synthtest", nb2, nh2)) == {100, 200}


def test_delete_missing_key_refused(monkeypatch):
    _inject(monkeypatch)
    body, header = _pair([(1, "First", 100, 1, 0x11)])
    assert apply_delete_to_pabgb_bytes(
        "synthtest", body, header, 999) is None


def test_delete_non_int_refused(monkeypatch):
    _inject(monkeypatch)
    body, header = _pair([(1, "First", 100, 1, 0x11)])
    assert apply_delete_to_pabgb_bytes(
        "synthtest", body, header, "x") is None


def test_delete_unknown_table_refused():
    body, header = _pair([(1, "First", 100, 1, 0x11)])
    assert apply_delete_to_pabgb_bytes(
        "notarealtable", body, header, 100) is None


def test_delete_survivors_byte_identical(monkeypatch):
    _inject(monkeypatch)
    entries = [
        (1, "First", 100, 0x11111111, 0x55),
        (2, "Second", 200, 0x22222222, 0x66),
        (3, "Third", 300, 0x33333333, 0x77),
    ]
    body, header = _pair(entries)
    nb, nh = apply_delete_to_pabgb_bytes("synthtest", body, header, 200)
    # concatenating the two surviving raw entries reproduces the new body
    assert nb == _entry(*entries[0]) + _entry(*entries[2])


# ── Validation ──────────────────────────────────────────────────────


def test_validate_delete_supported(monkeypatch):
    _inject(monkeypatch)
    i = _parse_intents_block([{"op": "delete_record", "key": 100}])[0]
    v = validate_intents("synthtest.pabgb", [i])
    assert i in v.supported


def test_validate_delete_no_schema_skipped():
    i = _parse_intents_block([{"op": "delete_record", "key": 100}])[0]
    v = validate_intents("definitelynotatable.pabgb", [i])
    assert not v.supported and "needs a decoded schema" in v.skipped[0][1]


# ── Apply-pipeline wiring ───────────────────────────────────────────


def test_build_change_deletes(monkeypatch):
    _inject(monkeypatch)
    body, header = _pair([
        (1, "First", 100, 1, 0x55),
        (2, "Second", 200, 2, 0x66),
    ])
    intent = _parse_intents_block([{"op": "delete_record", "key": 200}])[0]
    bc, comp, n = _build_record_ops_change_for_target(
        "synthtest.pabgb", body, header, [intent])
    assert n == 1 and bc is not None and comp is not None
    r = parse_records("synthtest", bytes.fromhex(bc["patched"]),
                      bytes.fromhex(comp["patched"]))
    assert set(r) == {100}


def test_build_change_missing_delete_returns_none(monkeypatch):
    _inject(monkeypatch)
    body, header = _pair([(1, "First", 100, 1, 0x55)])
    intent = _parse_intents_block([{"op": "delete_record", "key": 999}])[0]
    assert _build_record_ops_change_for_target(
        "synthtest.pabgb", body, header, [intent]) == (None, None, 0)


def test_clone_then_delete_compose(monkeypatch):
    _inject(monkeypatch)
    body, header = _pair([
        (1, "First", 100, 0xAAAA1111, 0x55),
        (2, "Second", 200, 0xBBBB2222, 0x66),
    ])
    clone = _parse_intents_block([{
        "op": "clone_record", "source_key": 100, "new_key": 500}])[0]
    delete = _parse_intents_block([{"op": "delete_record", "key": 200}])[0]
    bc, comp, n = _build_record_ops_change_for_target(
        "synthtest.pabgb", body, header, [clone, delete])
    assert n == 2
    r = parse_records("synthtest", bytes.fromhex(bc["patched"]),
                      bytes.fromhex(comp["patched"]))
    assert set(r) == {100, 500}          # 200 deleted, 500 cloned in
    assert r[500]["_foo"] == 0xAAAA1111
