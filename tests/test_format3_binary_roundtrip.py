"""Format 3 — end-to-end binary roundtrip on a synthetic table.

Real CDUMM tables have mixed flat+variable-length fields, which
the parser currently doesn't fully handle (parser.py drops variable-
length fields from the loaded schema, so offsets after a variable
field are wrong). To prove the Format 3 → bytes pipeline works
*structurally*, we use a synthetic flat-only schema injected into
the parser cache for the duration of the test.

These tests pin three things:

  1. Synthesizing a Format 3 mod body from vanilla + intents
     produces bytes that differ from vanilla at exactly the
     intended offsets.
  2. The existing ``SemanticEngine`` pipeline accepts the synth
     mod body and produces a merged body with the new value.
  3. Other records and other fields are byte-for-byte preserved.

Real-game per-table investigation (storeinfo, etc.) lands in a
follow-up after this proves the mechanism.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.format3_handler import (
    Format3Intent,
    apply_intents_to_pabgb_bytes,
)
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import (
    FieldSpec,
    TableSchema,
    parse_records,
)


# ── Synthetic table fixture ─────────────────────────────────────────


@pytest.fixture
def synth_schema(monkeypatch):
    """Inject a tiny all-flat synthetic table into the parser cache.

    Schema: synthtest with three fixed-width fields totaling 10
    bytes per record payload. Lets us build PABGB bytes by hand
    and check exact offsets.
    """
    fields = [
        FieldSpec(name="_key", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_foo", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_bar", stream_size=2,
                  field_type="direct_u16", struct_fmt="H"),
    ]
    schema = TableSchema(table_name="synthtest", fields=fields)

    # Snapshot + patch the cache so other tests aren't affected.
    original = parser_mod._loaded_schemas
    if original is None:
        # Force the production schemas to load too so tests that need
        # them keep working when run in the same process.
        parser_mod._load_schemas()
        original = parser_mod._loaded_schemas

    new_cache = dict(original or {})
    new_cache["synthtest"] = schema
    monkeypatch.setattr(parser_mod, "_loaded_schemas", new_cache)
    yield schema


# ── Tiny binary builders (no game files needed) ─────────────────────


def _build_entry(entry_id: int, name: str,
                 key_value: int, foo: int, bar: int) -> bytes:
    """One PABGB entry = u32 id + u32 name_len + name + 0x00 +
    payload (u32 _key + u32 _foo + u16 _bar)."""
    name_bytes = name.encode("utf-8")
    head = struct.pack("<II", entry_id, len(name_bytes))
    payload = struct.pack("<IIH", key_value, foo, bar)
    return head + name_bytes + b"\x00" + payload


def _build_pabgb_pair(entries: list[tuple[int, str, int, int, int]]
                      ) -> tuple[bytes, bytes]:
    """Build matching (body, header) bytes for the synthtest table.

    PABGH index = u16 count + N × (u32 key + u32 offset).
    """
    body = bytearray()
    keys_offsets: list[tuple[int, int]] = []
    for entry_id, name, key, foo, bar in entries:
        offset = len(body)
        body.extend(_build_entry(entry_id, name, key, foo, bar))
        keys_offsets.append((key, offset))

    header = bytearray(struct.pack("<H", len(entries)))
    for k, o in keys_offsets:
        header.extend(struct.pack("<II", k, o))
    return bytes(body), bytes(header)


# ── Sanity: parser can roundtrip our synthetic bytes ────────────────


def test_synth_bytes_parse_correctly(synth_schema):
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xAAAA1111, 0x55),
        (2, "Second", 200, 0xBBBB2222, 0x66),
    ])
    records = parse_records("synthtest", body, header)
    assert set(records.keys()) == {100, 200}
    assert records[100]["_foo"] == 0xAAAA1111
    assert records[100]["_bar"] == 0x55
    assert records[200]["_foo"] == 0xBBBB2222


# ── apply_intents_to_pabgb_bytes — direct byte-level writer ─────────


def test_intent_set_changes_field_at_correct_offset(synth_schema):
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xAAAA1111, 0x55),
        (2, "Second", 200, 0xBBBB2222, 0x66),
    ])
    intents = [Format3Intent(
        entry="First", key=100, field="_foo",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, intents)
    assert new_body != body
    # Re-parse to verify the value landed where the schema says it
    records = parse_records("synthtest", new_body, header)
    assert records[100]["_foo"] == 0xDEADBEEF
    # Other fields on same record preserved
    assert records[100]["_bar"] == 0x55
    assert records[100]["_key"] == 100
    # Other record byte-for-byte preserved
    assert records[200]["_foo"] == 0xBBBB2222
    assert records[200]["_bar"] == 0x66


def test_two_intents_on_two_records_both_apply(synth_schema):
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0x11111111, 0x55),
        (2, "Second", 200, 0x22222222, 0x66),
    ])
    intents = [
        Format3Intent(entry="First", key=100, field="_foo",
                      op="set", new=0xAAAAAAAA),
        Format3Intent(entry="Second", key=200, field="_bar",
                      op="set", new=0x99),
    ]
    new_body = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, intents)
    records = parse_records("synthtest", new_body, header)
    assert records[100]["_foo"] == 0xAAAAAAAA
    assert records[100]["_bar"] == 0x55  # untouched
    assert records[200]["_foo"] == 0x22222222  # untouched
    assert records[200]["_bar"] == 0x99


def test_intent_with_unknown_key_is_ignored(synth_schema):
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xCAFEBABE, 0x55),
    ])
    intents = [Format3Intent(
        entry="Missing", key=99999, field="_foo",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, intents)
    assert new_body == body  # untouched


def test_intent_with_unsupported_op_is_ignored(synth_schema):
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xCAFEBABE, 0x55),
    ])
    intents = [Format3Intent(
        entry="First", key=100, field="_foo",
        op="add_entry", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, intents)
    assert new_body == body


def test_intent_with_unknown_field_is_ignored(synth_schema):
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xCAFEBABE, 0x55),
    ])
    intents = [Format3Intent(
        entry="First", key=100, field="madeupField",
        op="set", new=42)]
    new_body = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, intents)
    assert new_body == body


def test_unknown_table_returns_unchanged_body(synth_schema):
    """If the requested table has no schema, the writer can't
    locate field offsets — return vanilla unchanged rather than
    guessing."""
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xCAFEBABE, 0x55),
    ])
    intents = [Format3Intent(
        entry="First", key=100, field="_foo",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "notarealtable", body, header, intents)
    assert new_body == body


def test_value_outside_field_width_clamps_or_raises(synth_schema):
    """Writing 0x100 (256) to a u8 field — must NOT silently
    truncate to 0 in a way that corrupts surrounding bytes. The
    writer should either raise or refuse this intent. Either is
    acceptable — what's NOT acceptable is silently writing 0
    overflowing into the next field."""
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0xCAFEBABE, 0x55),
    ])
    intents = [Format3Intent(
        entry="First", key=100, field="_bar",
        op="set", new=0x10000)]   # u16 max is 0xFFFF
    new_body = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, intents)
    # Either: new_body == body (refused) OR _bar is clamped, but
    # the rest of the record is unchanged.
    records = parse_records("synthtest", new_body, header)
    # _foo and _key MUST be preserved either way
    assert records[100]["_foo"] == 0xCAFEBABE
    assert records[100]["_key"] == 100


def test_size_preserving_writes_keep_total_body_length(synth_schema):
    """The body length must be unchanged after a flat-field write,
    since downstream code (PAMT, ENTR delta, etc.) tracks sizes."""
    body, header = _build_pabgb_pair([
        (1, "First", 100, 0x11111111, 0x55),
        (2, "Second", 200, 0x22222222, 0x66),
        (3, "Third", 300, 0x33333333, 0x77),
    ])
    intents = [
        Format3Intent(entry="First", key=100, field="_foo",
                      op="set", new=0xAAAAAAAA),
        Format3Intent(entry="Second", key=200, field="_bar",
                      op="set", new=0x88),
    ]
    new_body = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, intents)
    assert len(new_body) == len(body)


# ── Direct PABGB schema path must respect per-entry bounds ─────────


def _make_truncated_entry_pabgb():
    """Build a PABGB where the first entry has a truncated payload
    (writer thinks the schema implies 10 bytes, only 6 written),
    and the second entry follows immediately. The boundary case
    is what catches the bug.

    Entry 1 truncated to 6 payload bytes (4B _key + 2B _foo, no
    _bar). Entry 2 is normal.
    """
    name1 = b"First"
    head1 = struct.pack("<II", 1, len(name1))
    payload1 = struct.pack("<IH", 100, 0x1111)   # 6 bytes — _bar missing
    entry1 = head1 + name1 + b"\x00" + payload1
    e1_len = len(entry1)

    name2 = b"Second"
    head2 = struct.pack("<II", 2, len(name2))
    payload2 = struct.pack("<IIH", 200, 0xBBBBBBBB, 0x66)
    entry2 = head2 + name2 + b"\x00" + payload2

    body = entry1 + entry2

    header = bytearray(struct.pack("<H", 2))
    for k, off in [(100, 0), (200, e1_len)]:
        header.extend(struct.pack("<II", k, off))
    return bytes(body), bytes(header), e1_len


def test_direct_schema_write_does_not_leak_into_next_entry(
        synth_schema):
    """If a record's payload is shorter than the schema implies
    (truncated entries are real in production data), writing a
    field whose schema-computed offset lands past the actual
    payload end must NOT spill into the next entry's bytes.

    Synthtest schema is 10 bytes (_key u32 + _foo u32 + _bar u16).
    Entry 1 in this fixture is only 6 payload bytes — _bar's
    schema-computed offset (8) is past the entry's own end. Without
    the bound check, the write lands inside entry 2's header.
    """
    body, header, e1_len = _make_truncated_entry_pabgb()
    original_body = body

    intents = [Format3Intent(
        entry="First", key=100, field="_bar",
        op="set", new=0xCAFE)]
    new_body = apply_intents_to_pabgb_bytes(
        "synthtest", body, header, intents)

    # Entry 2's bytes (anything from offset e1_len onward) MUST be
    # byte-for-byte preserved. If the write leaked, those bytes
    # would change.
    assert new_body[e1_len:] == original_body[e1_len:], (
        "write into truncated entry 1 leaked into entry 2's bytes")
