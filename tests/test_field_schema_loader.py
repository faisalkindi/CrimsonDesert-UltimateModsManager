"""Field schema loader for Format 3 / JMM compatibility.

JMM's IteminfoBlobPatcher (decompiled from CD JSON Mod Manager
v9.9.3) uses a parallel "field schema" file separate from the
PABGB record schema:

    field_schema/iteminfo.json
    {
      "drops":   {"tid": "0xAABBCCDD", "value_offset": 5,
                  "type": "i32"},
      "attack":  {"rel_offset": 12, "type": "u32"},
      ...
    }

This is the "friendly name → write position" mapping Format 3
mods reference. It's separate from CDUMM's pabgb_complete_schema
(which describes the engine's record reader and is keyed by
internal field names like ``_attackPower``).

Two location strategies per entry:
  * ``rel_offset``: write at blob_start + rel_offset (fast, fixed)
  * ``tid``: search the entry blob for the 4-byte TID marker, write
    at tid_position + value_offset (durable across game updates as
    long as the TID is stable)

JMM ships no schemas — it expects the community to author them.
CDUMM does the same. The tests below pin the loader contract so
future-authored schemas drop in without code changes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cdumm.engine.field_schema import (
    FieldSchemaEntry,
    field_schema_path,
    load_field_schema,
    locate_field,
)


def _write_schema(tmp_path: Path, table: str, body: dict) -> Path:
    """Write a field_schema/<table>.json under tmp_path."""
    d = tmp_path / "field_schema"
    d.mkdir(exist_ok=True)
    p = d / f"{table}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


# ── load_field_schema ───────────────────────────────────────────────


def test_load_returns_empty_when_file_missing(tmp_path):
    """Missing schema file is normal — most tables won't have one
    until someone authors it. Loader must return an empty mapping
    instead of raising, so the apply path can fall back gracefully."""
    schema = load_field_schema("missing", search_root=tmp_path)
    assert schema == {}


def test_load_parses_tid_entry(tmp_path):
    _write_schema(tmp_path, "iteminfo", {
        "attack": {"tid": "0xAABBCCDD",
                   "value_offset": 5, "type": "i32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    entry = schema["attack"]
    assert isinstance(entry, FieldSchemaEntry)
    assert entry.tid == 0xAABBCCDD
    assert entry.value_offset == 5
    assert entry.rel_offset is None
    assert entry.data_type == "i32"


def test_load_parses_rel_offset_entry(tmp_path):
    _write_schema(tmp_path, "iteminfo", {
        "resetHour": {"rel_offset": 12, "type": "u32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    e = schema["resetHour"]
    assert e.rel_offset == 12
    assert e.tid is None
    assert e.data_type == "u32"


def test_load_accepts_int_tid_without_0x_prefix(tmp_path):
    """JMM's schema accepts both ``"tid": 0x12345678`` (number) and
    ``"tid": "0x12345678"`` (string) — keep parity."""
    _write_schema(tmp_path, "iteminfo", {
        "x": {"tid": 305419896, "value_offset": 5, "type": "i32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert schema["x"].tid == 0x12345678


def test_load_skips_underscore_keys(tmp_path):
    """JMM ignores keys starting with ``_`` — those are author
    annotations / comments in the schema file."""
    _write_schema(tmp_path, "iteminfo", {
        "_comment": "this is documentation, not a field",
        "real_field": {"tid": "0x1", "value_offset": 5,
                       "type": "i32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert "real_field" in schema
    assert "_comment" not in schema


def test_load_default_value_offset_is_5(tmp_path):
    """JMM defaults ``value_offset`` to 5 when absent (TID is 4
    bytes + 1-byte type tag → value sits at +5)."""
    _write_schema(tmp_path, "iteminfo", {
        "attack": {"tid": "0xAA", "type": "i32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert schema["attack"].value_offset == 5


def test_load_default_data_type_is_i32(tmp_path):
    _write_schema(tmp_path, "iteminfo", {
        "attack": {"tid": "0xAA"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert schema["attack"].data_type == "i32"


def test_load_skips_entries_without_tid_and_rel_offset(tmp_path):
    """An entry with neither tid nor rel_offset can't be located —
    drop it from the loaded schema rather than letting it fail
    silently at apply time."""
    _write_schema(tmp_path, "iteminfo", {
        "valid": {"tid": "0x1", "type": "i32"},
        "invalid": {"type": "i32"},   # no tid, no rel_offset
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert "valid" in schema
    assert "invalid" not in schema


def test_load_handles_malformed_json_gracefully(tmp_path):
    """Bad JSON should not crash the importer — log + return empty
    so the rest of CDUMM's import flow continues."""
    d = tmp_path / "field_schema"
    d.mkdir()
    (d / "iteminfo.json").write_text("not valid json {[}",
                                      encoding="utf-8")
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert schema == {}


# ── locate_field — TID search + rel_offset ──────────────────────────


def test_locate_rel_offset_returns_blob_start_plus_offset():
    blob = b"\xAA" * 100
    entry = FieldSchemaEntry(
        rel_offset=20, value_offset=0,
        data_type="u32", tid=None)
    pos = locate_field(blob, blob_start=10, blob_end=110,
                       entry=entry)
    assert pos == 30   # 10 + 20


def test_locate_tid_searches_blob_then_adds_value_offset():
    """Build a blob with a known TID at a specific position, verify
    the locator finds it and returns ``tid_pos + value_offset``."""
    import struct
    tid = 0x12345678
    tid_bytes = struct.pack("<I", tid)
    pre = b"\x00" * 30
    post = b"\xFF" * 30
    blob = pre + tid_bytes + post   # TID starts at byte 30

    entry = FieldSchemaEntry(
        tid=tid, value_offset=5,
        data_type="i32", rel_offset=None)
    pos = locate_field(blob, blob_start=0, blob_end=len(blob),
                       entry=entry)
    assert pos == 30 + 5    # 35


def test_locate_tid_returns_none_when_not_found():
    blob = b"\x00" * 100
    entry = FieldSchemaEntry(
        tid=0xDEADBEEF, value_offset=5,
        data_type="i32", rel_offset=None)
    assert locate_field(blob, 0, 100, entry) is None


def test_locate_tid_search_respects_blob_bounds():
    """A TID outside [blob_start, blob_end) must NOT be returned —
    we'd write into another entry."""
    import struct
    tid = 0xCAFEBABE
    blob = struct.pack("<I", tid) + b"\x00" * 100
    # blob_start=10 means the TID at byte 0 is OUT of bounds
    entry = FieldSchemaEntry(
        tid=tid, value_offset=5,
        data_type="i32", rel_offset=None)
    assert locate_field(blob, 10, len(blob), entry) is None


# ── field_schema_path search order ──────────────────────────────────


def test_path_resolves_to_search_root_field_schema_dir(tmp_path):
    expected = tmp_path / "field_schema" / "iteminfo.json"
    expected.parent.mkdir()
    expected.write_text("{}", encoding="utf-8")
    p = field_schema_path("iteminfo", search_root=tmp_path)
    assert p == expected


# ── Reject offsets that would write outside the entry ───────────────


def test_load_rejects_negative_rel_offset(tmp_path):
    """A typo like ``"rel_offset": -4`` (forgotten minus sign,
    confused convention) writes 4 bytes BEFORE the entry's payload
    start — into the previous entry's bytes. The bounds check in
    _apply_via_field_schema only validates the upper bound, so
    negatives slip through. Reject at load time so the mod author
    sees the bad entry instead of silent corruption."""
    _write_schema(tmp_path, "iteminfo", {
        "bad_field": {"rel_offset": -4, "type": "u32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert "bad_field" not in schema


def test_load_rejects_negative_value_offset(tmp_path):
    """``value_offset`` is the bytes between the TID match and the
    write position. Negative values overlap the TID itself or
    earlier bytes — corrupts the type tag or unrelated fields."""
    _write_schema(tmp_path, "iteminfo", {
        "bad_field": {"tid": "0xAA", "value_offset": -1,
                      "type": "i32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert "bad_field" not in schema


def test_load_keeps_valid_entries_when_one_is_negative(tmp_path):
    """One bad entry must not poison the rest of the schema."""
    _write_schema(tmp_path, "iteminfo", {
        "good": {"tid": "0xAA", "type": "i32"},
        "bad": {"rel_offset": -8, "type": "u32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert "good" in schema
    assert "bad" not in schema


def test_load_zero_offsets_are_allowed(tmp_path):
    """Zero offsets are legitimate (rel_offset=0 = blob start;
    value_offset=0 = write at TID position). Don't conflate
    'falsy' with 'invalid' — only negatives are wrong."""
    _write_schema(tmp_path, "iteminfo", {
        "at_zero_rel": {"rel_offset": 0, "type": "u32"},
        "at_tid": {"tid": "0xAA", "value_offset": 0,
                   "type": "i32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert "at_zero_rel" in schema
    assert schema["at_zero_rel"].rel_offset == 0
    assert "at_tid" in schema
    assert schema["at_tid"].value_offset == 0


# ── Reject unsupported data types at load ───────────────────────────


def test_load_rejects_unknown_data_type(tmp_path):
    """If a schema entry's ``type`` doesn't appear in the writer's
    dtype table (e.g. ``u128``, typo), the entry slipped through
    validation but silently no-op'd at apply time. The user saw
    'intent ready to apply' followed by no byte change. Refuse at
    load — surface the typo to the author."""
    _write_schema(tmp_path, "iteminfo", {
        "bogus_type": {"tid": "0xAA", "value_offset": 5,
                       "type": "u128"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert "bogus_type" not in schema


def test_load_accepts_all_supported_dtypes(tmp_path):
    """All struct types in the writer's dtype table must pass
    load. Pin the supported set so a future writer change can't
    silently drop a type without breaking this test."""
    supported = ["i8", "u8", "i16", "u16", "i32", "u32",
                 "f32", "i64", "u64", "f64"]
    schema_dict = {
        f"f_{t}": {"tid": "0xAA", "value_offset": 5, "type": t}
        for t in supported
    }
    _write_schema(tmp_path, "iteminfo", schema_dict)
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    for t in supported:
        assert f"f_{t}" in schema, (
            f"data_type {t!r} dropped at load — would have silently "
            f"no-op'd at apply")


def test_load_dtype_check_is_case_insensitive(tmp_path):
    """``"I32"`` and ``"i32"`` should both work — the writer
    lowercases before lookup so the loader should match its
    behaviour to keep the user-facing contract consistent."""
    _write_schema(tmp_path, "iteminfo", {
        "lower": {"tid": "0xAA", "type": "i32"},
        "upper": {"tid": "0xAA", "type": "I32"},
    })
    schema = load_field_schema("iteminfo", search_root=tmp_path)
    assert "lower" in schema
    assert "upper" in schema
