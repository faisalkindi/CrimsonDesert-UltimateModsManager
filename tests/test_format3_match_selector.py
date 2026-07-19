"""Format 3 ``match`` selector — parse, validate gate, expansion, and a
byte-exact equivalence proof against hand-authored per-record intents.

A ``match`` intent targets every record whose fields all equal the given
values (AND). At apply time it is expanded into ordinary per-record
``set`` intents, which flow through the same trusted writer path — so the
central correctness claim is: *expanding a match selector produces
byte-for-byte the same table as hand-authoring one ``set`` intent per
matched record.* That is what ``test_expand_is_byte_exact_*`` pins.

Uses the same synthetic all-flat table the binary-roundtrip test uses so
no game files are needed and exact offsets are checkable.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.format3_apply import (
    _expand_match_intents,
    _lookup_record_field,
    _match_record_keys,
    _match_value_equals,
)
from cdumm.engine.format3_handler import (
    Format3Intent,
    _classify_match_selector,
    _parse_intents_block,
    apply_intents_to_pabgb_bytes,
    validate_intents,
)
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import FieldSpec, TableSchema, parse_records


# ── Synthetic table injection (optionally with verified_fields) ──────


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


def _build_entry(entry_id: int, name: str,
                 key_value: int, foo: int, bar: int) -> bytes:
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


def test_parse_accepts_match_selector():
    intents = _parse_intents_block(
        [{"match": {"_foo": 7}, "field": "_bar", "new": 9}])
    assert len(intents) == 1
    i = intents[0]
    assert i.match == {"_foo": 7}
    assert i.entry == ""       # unused when match present
    assert i.key == 0
    assert i.field == "_bar"
    assert i.new == 9
    assert i.op == "set"


def test_parse_match_allows_missing_entry():
    # entry omitted is fine when a match selector is present
    out = _parse_intents_block(
        [{"match": {"_foo": 1}, "field": "_bar", "new": 2}])
    assert out[0].match == {"_foo": 1}


def test_parse_still_requires_entry_without_match():
    with pytest.raises(ValueError):
        _parse_intents_block([{"field": "_bar", "new": 2}])


def test_parse_accepts_empty_match_as_match_all():
    # An empty match {} is DMM Mod Builder's "apply to every record"
    # selector; the apply path treats a no-condition match as all records.
    out = _parse_intents_block(
        [{"match": {}, "field": "_bar", "new": 2}])
    assert out[0].match == {}


def test_parse_rejects_non_dict_match():
    with pytest.raises(ValueError):
        _parse_intents_block(
            [{"match": [1, 2], "field": "_bar", "new": 2}])


def test_parse_match_still_requires_new():
    with pytest.raises(ValueError):
        _parse_intents_block([{"match": {"_foo": 1}, "field": "_bar"}])


# ── Value equality / field lookup helpers ───────────────────────────


def test_match_value_equals_type_tolerance():
    assert _match_value_equals(5, 5)
    assert _match_value_equals(5, "5")       # decoded int vs JSON string
    assert _match_value_equals(5.0, 5)       # float vs int
    assert not _match_value_equals(5, 6)
    assert not _match_value_equals(None, 5)  # missing field never matches
    # bool is not treated as numeric 1/0 for the float path
    assert not _match_value_equals(2, True)


def test_lookup_record_field_name_shapes():
    rec = {"_itemType": 3}
    # snake_case without prefix resolves to the schema's _camelCase name
    assert _lookup_record_field(rec, "item_type") == 3
    assert _lookup_record_field(rec, "_itemType") == 3
    assert _lookup_record_field(rec, "nope") is None


# ── Expansion ───────────────────────────────────────────────────────


def test_expand_targets_all_matching_records(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([
        (1, "A", 100, 7, 0x11),
        (2, "B", 200, 7, 0x22),
        (3, "C", 300, 9, 0x33),
    ])
    mi = Format3Intent(entry="", key=0, field="_bar",
                       op="set", new=0x99, match={"_foo": 7})
    out = _expand_match_intents("synthtest.pabgb", body, header, [mi])
    assert len(out) == 2
    assert {i.key for i in out} == {100, 200}
    assert {i.entry for i in out} == {"A", "B"}
    assert all(i.match is None and i.op == "set"
               and i.field == "_bar" and i.new == 0x99 for i in out)


def test_expand_matches_on_name_metadata(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([
        (1, "Sword", 100, 7, 0x11),
        (2, "Shield", 200, 7, 0x22),
    ])
    mi = Format3Intent(entry="", key=0, field="_bar",
                       op="set", new=0x9, match={"_name": "Sword"})
    out = _expand_match_intents("synthtest.pabgb", body, header, [mi])
    assert [i.key for i in out] == [100]


def test_expand_multi_condition_is_and(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([
        (1, "A", 100, 7, 0x11),   # foo=7 bar=0x11
        (2, "B", 200, 7, 0x22),   # foo=7 bar=0x22
    ])
    mi = Format3Intent(entry="", key=0, field="_bar", op="set", new=0x5,
                       match={"_foo": 7, "_bar": 0x22})
    out = _expand_match_intents("synthtest.pabgb", body, header, [mi])
    assert [i.key for i in out] == [200]   # only the AND match


def test_non_match_intents_pass_through_unchanged(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([(1, "A", 100, 7, 0x11)])
    plain = Format3Intent(entry="A", key=100, field="_bar",
                          op="set", new=0x5)
    out = _expand_match_intents("synthtest.pabgb", body, header, [plain])
    assert out == [plain]


def test_zero_match_expands_to_nothing(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([(1, "A", 100, 7, 0x11)])
    mi = Format3Intent(entry="", key=0, field="_bar",
                       op="set", new=0x9, match={"_foo": 999})
    out = _expand_match_intents("synthtest.pabgb", body, header, [mi])
    assert out == []


def test_expand_undecodable_table_is_safe_noop(monkeypatch):
    _inject(monkeypatch)
    mi = Format3Intent(entry="", key=0, field="_bar",
                       op="set", new=0x9, match={"_foo": 7})
    # Garbage bytes -> parse yields no records -> no expansion, no raise.
    out = _expand_match_intents("synthtest.pabgb", b"\x00\x01", b"", [mi])
    assert out == []


# ── The byte-exact equivalence proof ────────────────────────────────


def test_expand_is_byte_exact_vs_hand_authored(monkeypatch):
    _inject(monkeypatch)
    entries = [
        (1, "A", 100, 7, 0x11),
        (2, "B", 200, 7, 0x22),
        (3, "C", 300, 9, 0x33),
    ]
    body, header = _build_pabgb_pair(entries)

    mi = Format3Intent(entry="", key=0, field="_bar",
                       op="set", new=0x99, match={"_foo": 7})
    expanded = _expand_match_intents(
        "synthtest.pabgb", body, header, [mi])

    hand = [
        Format3Intent(entry="A", key=100, field="_bar",
                      op="set", new=0x99),
        Format3Intent(entry="B", key=200, field="_bar",
                      op="set", new=0x99),
    ]

    from_match = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, expanded)
    from_hand = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, hand)

    # Central claim: identical bytes.
    assert from_match == from_hand
    # And correctness: matched records changed, others preserved.
    recs = parse_records("synthtest", from_match, header)
    assert recs[100]["_bar"] == 0x99
    assert recs[200]["_bar"] == 0x99
    assert recs[300]["_bar"] == 0x33          # untouched
    assert recs[300]["_foo"] == 9             # untouched
    assert len(from_match) == len(body)        # size-preserving


def test_expand_no_hit_leaves_table_byte_identical(monkeypatch):
    _inject(monkeypatch)
    body, header = _build_pabgb_pair([(1, "A", 100, 7, 0x11)])
    mi = Format3Intent(entry="", key=0, field="_bar",
                       op="set", new=0x9, match={"_foo": 999})
    expanded = _expand_match_intents(
        "synthtest.pabgb", body, header, [mi])
    out = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, expanded)
    assert out == body


# ── Validation gate (match-fields must be safe to compare) ──────────


def _specs():
    fields = [
        FieldSpec(name="_key", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_foo", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_bar", stream_size=2,
                  field_type="direct_u16", struct_fmt="H"),
    ]
    return fields, {f.name: f for f in fields}


def test_classify_match_selector_allows_verified_field():
    fields, fs = _specs()
    schema = TableSchema(table_name="t", fields=fields,
                         verified_fields=frozenset({"_foo", "_bar"}))
    ok = Format3Intent(entry="", key=0, field="_bar", op="set",
                       new=1, match={"_foo": 7})
    assert _classify_match_selector(ok, schema, fs) is None


def test_classify_match_selector_rejects_unverified_field():
    fields, fs = _specs()
    schema = TableSchema(table_name="t", fields=fields,
                         verified_fields=frozenset({"_bar"}))
    bad = Format3Intent(entry="", key=0, field="_bar", op="set",
                        new=1, match={"_foo": 7})
    reason = _classify_match_selector(bad, schema, fs)
    assert reason and "not a verified field" in reason


def test_classify_match_selector_rejects_unknown_field():
    fields, fs = _specs()
    schema = TableSchema(table_name="t", fields=fields,
                         verified_fields=frozenset({"_foo", "_bar"}))
    unk = Format3Intent(entry="", key=0, field="_bar", op="set",
                        new=1, match={"_nope": 7})
    reason = _classify_match_selector(unk, schema, fs)
    assert reason and "not a known field" in reason


def test_classify_match_selector_metadata_always_safe():
    fields, fs = _specs()
    # _name is safe even though it isn't in verified_fields.
    schema = TableSchema(table_name="t", fields=fields,
                         verified_fields=frozenset({"_bar"}))
    meta = Format3Intent(entry="", key=0, field="_bar", op="set",
                         new=1, match={"_name": "x"})
    assert _classify_match_selector(meta, schema, fs) is None


def test_validate_match_skipped_on_no_schema_table():
    # No schema for this table -> match can't be resolved -> skipped
    # with a precise reason, never silently applied.
    mi = Format3Intent(entry="", key=0, field="whatever", op="set",
                       new=1, match={"x": 1})
    v = validate_intents("definitelynotatable.pabgb", [mi])
    assert not v.supported
    assert v.skipped
    assert "needs a decoded schema" in v.skipped[0][1]


# ── list / any-of match (GitHub #272, pinapana's Crazy ExtraSockets) ────
# A list on the mod side means "any of" (SQL IN), so one intent can target
# a whole family of records instead of one intent per value.

def test_match_value_list_is_any_of():
    assert _match_value_equals(5, [1, 5, 9]) is True
    assert _match_value_equals(7, [1, 5, 9]) is False


def test_match_value_list_keeps_numeric_and_string_tolerance():
    assert _match_value_equals(5, ["5", 9]) is True
    assert _match_value_equals(5.0, [1, 5]) is True


def test_match_value_empty_list_matches_nothing():
    assert _match_value_equals(5, []) is False


def test_match_value_list_vs_list_field_stays_exact_equality():
    # When the record's own value is a list, a list on the mod side must
    # keep exact-equality semantics so a genuinely list-valued field can
    # still be matched whole (and never becomes an accidental any-of).
    assert _match_value_equals([1, 2], [1, 2]) is True
    assert _match_value_equals([1, 2], [1, 2, 3]) is False
    assert _match_value_equals([3], [1, 2, 3]) is False


def test_match_record_keys_selects_every_record_in_the_list():
    records = {
        1: {"_name": "a", "_key": 1, "kind": 10},
        2: {"_name": "b", "_key": 2, "kind": 20},
        3: {"_name": "c", "_key": 3, "kind": 30},
        4: {"_name": "d", "_key": 4, "kind": 20},
    }
    # any-of picks up both kind==20 records plus kind==10
    got = _match_record_keys(records, {"kind": [10, 20]})
    assert got == [1, 2, 4]
    # a single scalar still behaves exactly as before
    assert _match_record_keys(records, {"kind": 30}) == [3]
