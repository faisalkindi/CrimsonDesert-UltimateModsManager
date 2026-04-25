"""Parser entry-header bug: entry_id width matches PABGH key_size.

Empirically verified by extracting vanilla storeinfo + dropsetinfo
from the live game (1.04.02):

  storeinfo  (key_size=2 in PABGH index)
    body[0..23] = u16 entry_id + u32 name_len + name + null
    First record: entry_id=0x0001, name_len=19,
                  name='Store_Her_Equipment'

  dropsetinfo (key_size=4 in PABGH index)
    body[0..27] = u32 entry_id + u32 name_len + name + null
    First record: entry_id=100000, name_len=20,
                  name='DropSet_BoxBarrel_01'

The CDUMM parser (parser.py:_parse_entry_header,
engine.py:_parse_entry_header_offset, format3_handler:_entry_
payload_offset) hardcoded u32 for entry_id. On key_size=2 tables
the parser misaligned everything: a 17-byte name was read as
'name_len=1.9 billion', which then bailed back to nlen=0, and
field-by-field stream parsing read garbage values. 292 records
appeared to "parse" (count matched the index) but every field
value was wrong.

These tests pin the fix: pass the key_size from the PABGH index
through to the entry-header parser so it reads the right number
of bytes for entry_id. Tests use synthetic bytes constructed in
each layout — that's enough to verify the dispatch without
checking a 1.8 MB binary into the repo.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.semantic.parser import (
    FieldSpec,
    TableSchema,
    parse_records,
)
from cdumm.semantic import parser as parser_mod


# ── Schema fixtures (one per key_size) ──────────────────────────────


def _flat_schema() -> TableSchema:
    return TableSchema(table_name="kstest", fields=[
        FieldSpec(name="_alpha", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_beta", stream_size=2,
                  field_type="direct_u16", struct_fmt="H"),
    ])


@pytest.fixture
def schema_in_cache(monkeypatch):
    """Inject a flat synthetic table into the parser cache."""
    parser_mod._load_schemas()
    cache = dict(parser_mod._loaded_schemas or {})
    cache["kstest"] = _flat_schema()
    monkeypatch.setattr(parser_mod, "_loaded_schemas", cache)


# ── Binary builders for both layouts ────────────────────────────────


def _entry_u16_id(entry_id: int, name: str,
                  alpha: int, beta: int) -> bytes:
    """Build an entry with u16 entry_id (storeinfo / inventory style).

    Layout: u16 entry_id + u32 name_len + name + null + u32 + u16.
    """
    name_bytes = name.encode("utf-8")
    head = struct.pack("<HI", entry_id, len(name_bytes))
    payload = struct.pack("<IH", alpha, beta)
    return head + name_bytes + b"\x00" + payload


def _entry_u32_id(entry_id: int, name: str,
                  alpha: int, beta: int) -> bytes:
    """Build an entry with u32 entry_id (dropsetinfo style)."""
    name_bytes = name.encode("utf-8")
    head = struct.pack("<II", entry_id, len(name_bytes))
    payload = struct.pack("<IH", alpha, beta)
    return head + name_bytes + b"\x00" + payload


def _build_pair(entries: list[bytes], keys: list[int],
                key_size: int) -> tuple[bytes, bytes]:
    """Build (body, header). Header = u16 count + N × (key + u32 offset).
    ``key_size`` is 2 or 4 — matches the entry_id width used in the
    body."""
    body = bytearray()
    pairs: list[tuple[int, int]] = []
    for entry, key in zip(entries, keys):
        pairs.append((key, len(body)))
        body.extend(entry)

    header = bytearray(struct.pack("<H", len(entries)))
    fmt = "<H" if key_size == 2 else "<I"
    for k, off in pairs:
        header.extend(struct.pack(fmt, k))
        header.extend(struct.pack("<I", off))
    return bytes(body), bytes(header)


# ── Parser correctness on each layout ───────────────────────────────


def test_parser_decodes_name_correctly_for_u16_entry_id(schema_in_cache):
    """key_size=2 table: entry_id is u16. Name 'Store_Her_Equipment'
    (19 bytes) must decode."""
    body, header = _build_pair(
        entries=[_entry_u16_id(0x0001, "Store_Her_Equipment",
                               0xCAFEBABE, 0x1234)],
        keys=[0x0001],
        key_size=2,
    )
    records = parse_records("kstest", body, header)
    assert len(records) == 1
    rec = records[1]
    assert rec["_name"] == "Store_Her_Equipment"
    assert rec["_alpha"] == 0xCAFEBABE
    assert rec["_beta"] == 0x1234


def test_parser_decodes_name_correctly_for_u32_entry_id(schema_in_cache):
    """key_size=4 table (matching dropsetinfo): entry_id is u32.
    Name 'DropSet_BoxBarrel_01' (20 bytes) must decode and field
    values must roundtrip."""
    body, header = _build_pair(
        entries=[_entry_u32_id(100000, "DropSet_BoxBarrel_01",
                               0xFEEDFACE, 0x5678)],
        keys=[100000],
        key_size=4,
    )
    records = parse_records("kstest", body, header)
    assert len(records) == 1
    rec = records[100000]
    assert rec["_name"] == "DropSet_BoxBarrel_01"
    assert rec["_alpha"] == 0xFEEDFACE
    assert rec["_beta"] == 0x5678


def test_parser_field_values_are_not_garbage_on_u16_table(
        schema_in_cache):
    """Regression guard for the original bug: before the fix,
    parsing a key_size=2 table read the name BYTES as field values
    (because name_len decoded as garbage ~1.9 billion → bailed →
    fields read at wrong offsets). After the fix, fields land at
    the right offsets and equal what we wrote."""
    body, header = _build_pair(
        entries=[
            _entry_u16_id(0x0001, "First", 0x11111111, 0x55),
            _entry_u16_id(0x0002, "Second", 0x22222222, 0x66),
        ],
        keys=[0x0001, 0x0002],
        key_size=2,
    )
    records = parse_records("kstest", body, header)
    assert records[1]["_alpha"] == 0x11111111
    assert records[1]["_beta"] == 0x55
    assert records[2]["_alpha"] == 0x22222222
    assert records[2]["_beta"] == 0x66
    # Record count matches input — no garbage records inserted
    assert len(records) == 2


# ── format3_handler must use the same width-aware parsing ───────────


def test_format3_writer_targets_correct_field_on_u16_table(
        schema_in_cache):
    """format3_handler.apply_intents_to_pabgb_bytes must locate
    the payload start using the same key_size-aware logic as the
    parser. Otherwise the writer reads from the wrong byte offset
    and silently corrupts neighboring fields."""
    from cdumm.engine.format3_handler import (
        Format3Intent,
        apply_intents_to_pabgb_bytes,
    )
    body, header = _build_pair(
        entries=[_entry_u16_id(0x0001, "Store_X",
                               0xCAFEBABE, 0x1234)],
        keys=[0x0001],
        key_size=2,
    )
    intents = [Format3Intent(
        entry="Store_X", key=0x0001, field="_alpha",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "kstest", body, header, intents)
    # Re-parse new body — _alpha must equal new value, _beta untouched
    records = parse_records("kstest", new_body, header)
    assert records[1]["_alpha"] == 0xDEADBEEF
    assert records[1]["_beta"] == 0x1234
    # Body length preserved
    assert len(new_body) == len(body)


def test_format3_writer_does_not_corrupt_name_bytes_on_u16_table(
        schema_in_cache):
    """Specific guard against the most painful failure mode: writing
    to _alpha at the WRONG offset corrupts the name bytes (because
    pre-fix the writer thought payload started 2 bytes earlier).
    Confirm the name string survives a write."""
    from cdumm.engine.format3_handler import (
        Format3Intent,
        apply_intents_to_pabgb_bytes,
    )
    body, header = _build_pair(
        entries=[_entry_u16_id(0x0001, "PreserveMe",
                               0x11111111, 0x99)],
        keys=[0x0001],
        key_size=2,
    )
    intents = [Format3Intent(
        entry="PreserveMe", key=0x0001, field="_alpha",
        op="set", new=0xAAAAAAAA)]
    new_body = apply_intents_to_pabgb_bytes(
        "kstest", body, header, intents)
    records = parse_records("kstest", new_body, header)
    assert records[1]["_name"] == "PreserveMe"


# ── Refuse unsupported key_size values ──────────────────────────────


def test_writer_refuses_apply_when_key_size_is_neither_2_nor_4(
        schema_in_cache):
    """parse_pabgh_index can yield key_size = 1, 3, 5, 6, 7, 8 from
    arithmetic on (header_len - count_size) / count if a header is
    truncated, malformed, or genuinely uses a width we don't know.
    The entry-header parser only handles 2 or 4. Anything else
    silently misaligns every payload read, which is worse than
    refusing the apply.

    Build a header where the arithmetic yields key_size=8 (a u64-
    keyed table — we don't know if any real CD table uses this
    width but it's reachable from the parse function). The writer
    must return vanilla unchanged.
    """
    import struct
    from cdumm.engine.format3_handler import (
        Format3Intent,
        apply_intents_to_pabgb_bytes,
    )
    # Construct a synthetic body + 8-byte-key header
    name = b"WeirdSize"
    head = struct.pack("<II", 1, len(name))
    payload = struct.pack("<II", 0xCAFEBABE, 0xCAFEBABE)
    body = head + name + b"\x00" + payload
    # Header: u16 count=1 + 1 entry of (8B key + 4B offset)
    header = bytearray(struct.pack("<H", 1))
    header.extend(struct.pack("<Q", 1))   # 8-byte key
    header.extend(struct.pack("<I", 0))   # offset
    header = bytes(header)

    intents = [Format3Intent(
        entry="WeirdSize", key=1, field="_alpha",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "kstest", bytes(body), header, intents)

    # Refused → bytes unchanged. We do NOT silently misalign.
    assert new_body == bytes(body)
