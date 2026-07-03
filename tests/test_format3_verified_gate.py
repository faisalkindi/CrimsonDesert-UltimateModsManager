"""Format 3 apply honors the verified-only field gate.

A table can mark which fields are validated (`_verified_fields` in the type
overrides). Those fields decode in the Game Data grid AND are writable by
Format 3 `.field.json` mods; fields it doesn't vouch for render `(unverified)`
and must be refused by the apply path too — writing to an unproven offset
could land on the wrong byte. First real user: wantedinfo (`_increasePrice`
validated; `_isBlocked` / `_useTargetPrice` not).
"""
from __future__ import annotations

import struct

from cdumm.engine.format3_apply import _resolve_write_pos
from cdumm.engine.format3_handler import Format3Intent
from cdumm.semantic.parser import FieldSpec, TableSchema


def _schema(verified):
    return TableSchema(
        "wantedlike",
        [FieldSpec("_increasePrice", 8, "direct_u64", "Q", "u64"),
         FieldSpec("_isBlocked", 1, "direct_u8", "B", "u8")],
        verified_fields=verified)


def _intent(field):
    return Format3Intent(entry="", key=1, field=field, op="set", new=5)


def test_verified_field_resolves():
    schema = _schema(frozenset({"_increasePrice"}))
    fs = {f.name: f for f in schema.fields}
    body = struct.pack("<Q", 1500) + b"\x01"
    # the validated field writes at payload offset 0, u64
    assert _resolve_write_pos(
        _intent("increase_price"), {}, fs, schema, body, 0, len(body)
    ) == (0, 8, "Q")


def test_unverified_field_refused():
    schema = _schema(frozenset({"_increasePrice"}))
    fs = {f.name: f for f in schema.fields}
    body = struct.pack("<Q", 1500) + b"\x01"
    # not vouched for → apply must refuse (no write to an unproven byte)
    assert _resolve_write_pos(
        _intent("_isBlocked"), {}, fs, schema, body, 0, len(body)) is None


def test_no_gate_when_verified_is_none():
    # tables that don't opt in are unaffected — both fields resolve
    schema = _schema(None)
    fs = {f.name: f for f in schema.fields}
    body = struct.pack("<Q", 1500) + b"\x01"
    assert _resolve_write_pos(
        _intent("increase_price"), {}, fs, schema, body, 0, len(body)
    ) == (0, 8, "Q")
    assert _resolve_write_pos(
        _intent("_isBlocked"), {}, fs, schema, body, 0, len(body)
    ) == (8, 1, "B")
