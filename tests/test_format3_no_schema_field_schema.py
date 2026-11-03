"""Format 3 + field_schema on tables WITHOUT a CDUMM PABGB schema.

skill.pabgb has no CDUMM PABGB schema entry, so Format 3 primitive
intents on it used to be silently dropped at validation. With
voiddoiv's contribution (Nexus 2026-05-08), the validator and apply
path now consult ``field_schema/<table>.json`` even when the table
isn't in the PABGB schema cache, enabling community-curated tid +
value_offset writes.

These tests pin the new behavior using a synthetic table name that
deliberately is NOT in any schema, isolating the no-schema path from
the existing field_schema-with-schema tests.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from cdumm.engine.format3_apply import _intents_to_v2_changes
from cdumm.engine.format3_handler import Format3Intent, validate_intents


def _entry_u32_id(entry_id: int, name: str,
                  alpha: int, beta: int, gamma: int) -> bytes:
    name_b = name.encode("utf-8")
    head = struct.pack("<II", entry_id, len(name_b))
    payload = struct.pack("<IIH", alpha, beta, gamma)
    return head + name_b + b"\x00" + payload


def _build_pabgb(entries: list[bytes],
                  keys: list[int]) -> tuple[bytes, bytes]:
    body = bytearray()
    pairs: list[tuple[int, int]] = []
    for e, k in zip(entries, keys):
        pairs.append((k, len(body)))
        body.extend(e)
    header = bytearray(struct.pack("<H", len(entries)))
    for k, off in pairs:
        header.extend(struct.pack("<II", k, off))
    return bytes(body), bytes(header)


@pytest.fixture
def field_schema_root(tmp_path, monkeypatch):
    """Isolate the field_schema loader from any shipped files."""
    monkeypatch.setenv("CDUMM_FIELD_SCHEMA_ROOT", str(tmp_path))
    return tmp_path


def _write_field_schema(root: Path, table: str, body: dict) -> Path:
    d = root / "field_schema"
    d.mkdir(exist_ok=True)
    p = d / f"{table}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_validator_accepts_field_schema_intent_on_no_schema_table(
        field_schema_root):
    """An intent targeting a no-PABGB-schema table should be accepted
    by the validator if its field is in field_schema/<table>.json."""
    _write_field_schema(field_schema_root, "synthnoschema", {
        "myField": {"tid": "0xAABBCCDD", "value_offset": 4, "type": "u32"},
    })

    intents = [Format3Intent(
        entry="First", key=1, field="myField", op="set", new=42)]
    result = validate_intents("synthnoschema.pabgb", intents)

    assert len(result.supported) == 1
    assert len(result.skipped) == 0


def test_validator_rejects_unknown_field_on_no_schema_table(
        field_schema_root):
    """An intent whose field has neither a list-writer NOR a
    field_schema entry should still be skipped with a clear reason."""
    _write_field_schema(field_schema_root, "synthnoschema", {
        "myField": {"tid": "0xAABBCCDD", "value_offset": 4, "type": "u32"},
    })

    intents = [Format3Intent(
        entry="First", key=1, field="unknownField", op="set", new=42)]
    result = validate_intents("synthnoschema.pabgb", intents)

    assert len(result.supported) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    assert "field_schema/synthnoschema.json" in reason


def test_apply_emits_v2_change_for_field_schema_intent_no_schema(
        field_schema_root):
    """End-to-end: intent on a no-PABGB-schema table whose field is
    declared in field_schema produces a v2 change with the right
    bytes at TID + value_offset."""
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0xAABBCCDD, 0x42424242, 0x9999)],
        keys=[1])
    _write_field_schema(field_schema_root, "synthnoschema", {
        "myField": {
            "tid": "0xAABBCCDD",
            "value_offset": 4,
            "type": "u32",
        },
    })

    intents = [Format3Intent(
        entry="First", key=1, field="myField",
        op="set", new=0xCAFEF00D)]
    changes = _intents_to_v2_changes(
        "synthnoschema.pabgb", body, header, intents)

    assert len(changes) == 1
    c = changes[0]
    assert c["entry"] == "First"
    assert c["original"] == "42424242"
    assert c["patched"] == "0df0feca"
    assert c["label"] == "First.myField"


def test_apply_returns_empty_when_no_routable_intents(
        field_schema_root):
    """Intent whose field is neither in LIST_WRITERS nor in
    field_schema must produce no changes, not crash."""
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0xAABBCCDD, 0x42424242, 0x9999)],
        keys=[1])
    # No field_schema file written.
    intents = [Format3Intent(
        entry="First", key=1, field="myField", op="set", new=42)]
    changes = _intents_to_v2_changes(
        "synthnoschema.pabgb", body, header, intents)
    assert changes == []


# ── Raw-record replacement (voiddoiv _buff_data_raw style) ─────────


def test_raw_replacement_searches_entry_for_old_bytes(
        field_schema_root):
    """An intent with old + new hex strings should locate `old`
    inside the entry's payload bounds and replace it with `new`."""
    # _alpha=0xAABBCCDD, _beta=0x42424242, _gamma=0x9999
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0xAABBCCDD, 0x42424242, 0x9999)],
        keys=[1])
    # Replace the _beta value (0x42424242 little-endian) with
    # 0xCAFEF00D. No field_schema entry needed.
    intents = [Format3Intent(
        entry="First", key=1, field="_buff_data_raw",
        op="set",
        old="42424242",
        new="0df0feca",
    )]
    changes = _intents_to_v2_changes(
        "synthnoschema.pabgb", body, header, intents)
    assert len(changes) == 1
    c = changes[0]
    assert c["entry"] == "First"
    assert c["original"] == "42424242"
    assert c["patched"] == "0df0feca"
    assert c["label"] == "First._buff_data_raw"


def test_raw_replacement_skips_when_old_not_in_entry(
        field_schema_root, caplog):
    """If the vanilla bytes don't contain `old`, the intent must
    be skipped (mod expects a different game version)."""
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0x11111111, 0x22222222, 0x3333)],
        keys=[1])
    intents = [Format3Intent(
        entry="First", key=1, field="_buff_data_raw",
        op="set",
        old="DEADBEEF",
        new="CAFEF00D",
    )]
    changes = _intents_to_v2_changes(
        "synthnoschema.pabgb", body, header, intents)
    assert changes == []


def test_raw_replacement_skips_on_ambiguous_match(field_schema_root):
    """If `old` appears at multiple positions inside the entry
    payload, refuse rather than guessing which one to replace."""
    # Both _alpha and _beta hold 0x12345678, so the bytes 78 56 34 12
    # appear at TWO positions inside the entry payload.
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0x12345678, 0x12345678, 0x9999)],
        keys=[1])
    intents = [Format3Intent(
        entry="First", key=1, field="_buff_data_raw",
        op="set",
        old="78563412",
        new="EFCDAB89",
    )]
    changes = _intents_to_v2_changes(
        "synthnoschema.pabgb", body, header, intents)
    assert changes == []


def test_raw_replacement_skips_when_old_is_not_a_string(field_schema_root):
    """GitHub #467: an exporter shipped 'old' as a JSON integer
    instead of a hex string. The apply path must skip just this
    intent (bytes.fromhex rejects a non-str argument) rather than
    crashing the whole batch."""
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0xAABBCCDD, 0x42424242, 0x9999)],
        keys=[1])
    intents = [Format3Intent(
        entry="First", key=1, field="_buff_data_raw",
        op="set",
        old=0,
        new="CAFEF00D",
    )]
    changes = _intents_to_v2_changes(
        "synthnoschema.pabgb", body, header, intents)
    assert changes == []


def test_raw_replacement_skips_on_length_mismatch(field_schema_root):
    """old and new must be equal-length byte strings; otherwise the
    write would shift trailing bytes and corrupt subsequent fields."""
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0xAABBCCDD, 0x42424242, 0x9999)],
        keys=[1])
    intents = [Format3Intent(
        entry="First", key=1, field="_buff_data_raw",
        op="set",
        old="42424242",
        new="00",
    )]
    changes = _intents_to_v2_changes(
        "synthnoschema.pabgb", body, header, intents)
    assert changes == []
