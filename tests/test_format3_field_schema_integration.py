"""Format 3 + field_schema integration.

Wires JMM-style field_schema/<table>.json into the apply path so
mods authored against community-curated TID / rel_offset entries
can target real-game tables (storeinfo, iteminfo, etc.) where
CDUMM's PABGB record schema can't read past the first variable-
length field.

Two location strategies tested end-to-end:

  * ``rel_offset``: deterministic position from blob start.
  * ``tid``: search for a 4-byte type-id marker inside the entry,
    write at ``tid_pos + value_offset``.

The validator also needs to know about field_schema-resolved
fields, otherwise it would surface them as "field not found"
even when a schema entry exists.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from cdumm.engine.format3_handler import (
    Format3Intent,
    apply_intents_to_pabgb_bytes,
    validate_intents,
)
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import FieldSpec, TableSchema


# ── Synthetic-table fixtures (carry over from binary_roundtrip) ────


@pytest.fixture
def synth_schema(monkeypatch):
    """Synthetic 'kvtest' table — three flat fields, total 10 bytes
    payload. Used as the PABGB schema fallback target."""
    fields = [
        FieldSpec(name="_alpha", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_beta", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        FieldSpec(name="_gamma", stream_size=2,
                  field_type="direct_u16", struct_fmt="H"),
    ]
    schema = TableSchema(table_name="kvtest", fields=fields)
    parser_mod._load_schemas()
    cache = dict(parser_mod._loaded_schemas or {})
    cache["kvtest"] = schema
    monkeypatch.setattr(parser_mod, "_loaded_schemas", cache)
    yield schema


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


# ── field_schema search-root fixture ────────────────────────────────


@pytest.fixture
def field_schema_root(tmp_path, monkeypatch):
    """Point the loader at a tmp_path/field_schema/ dir for the
    duration of the test, isolated from any real shipped schema."""
    monkeypatch.setenv("CDUMM_FIELD_SCHEMA_ROOT", str(tmp_path))
    return tmp_path


def _write_field_schema(root: Path, table: str, body: dict) -> Path:
    d = root / "field_schema"
    d.mkdir(exist_ok=True)
    p = d / f"{table}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


# ── rel_offset path ─────────────────────────────────────────────────


def test_rel_offset_field_schema_writes_correct_bytes(
        synth_schema, field_schema_root):
    """A field_schema entry with rel_offset writes at exactly that
    byte offset inside the entry payload. This exercises the path
    that a mod author would use for fields with known stable
    positions."""
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0x11111111, 0x22222222, 0x33)],
        keys=[1],
    )
    # rel_offset=4 lands exactly on _beta in our synthetic layout
    _write_field_schema(field_schema_root, "kvtest", {
        "myFriendlyName": {"rel_offset": 4, "type": "u32"},
    })

    intents = [Format3Intent(
        entry="First", key=1, field="myFriendlyName",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "kvtest", body, header, intents)

    # Verify _beta byte changed and _alpha / _gamma preserved.
    from cdumm.semantic.parser import parse_records
    records = parse_records("kvtest", new_body, header)
    assert records[1]["_alpha"] == 0x11111111   # untouched
    assert records[1]["_beta"] == 0xDEADBEEF    # changed
    assert records[1]["_gamma"] == 0x33         # untouched


def test_tid_field_schema_searches_for_marker(
        synth_schema, field_schema_root):
    """A field_schema entry with tid searches the entry payload
    for the 4-byte TID, then writes at ``tid_pos + value_offset``.
    This is the JMM path that survives game updates as long as
    the TID is stable."""
    # Embed a known TID inside our synthetic entry so the search
    # finds it. _alpha holds 0xAABBCCDD; we'll target a write at
    # the 5th byte after the TID (value_offset=5).
    # Layout: payload = [u32 _alpha=0xAABBCCDD][u32 _beta=0x42424242]
    #                   [u16 _gamma=0x9999]
    #                   = 10 payload bytes total
    # TID 0xAABBCCDD lives at payload[0:4]. value_offset=5 →
    # write position = (TID start) + 5 = payload[5:9] which is
    # the high 3 bytes of _beta + first byte of _gamma. That's
    # ugly to verify, so let's pick value_offset=4 instead:
    # write at payload[4:8] which is exactly _beta.
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0xAABBCCDD, 0x42424242, 0x9999)],
        keys=[1],
    )
    _write_field_schema(field_schema_root, "kvtest", {
        "tidField": {
            "tid": "0xAABBCCDD",
            "value_offset": 4,
            "type": "u32",
        },
    })

    intents = [Format3Intent(
        entry="First", key=1, field="tidField",
        op="set", new=0xCAFEF00D)]
    new_body = apply_intents_to_pabgb_bytes(
        "kvtest", body, header, intents)

    from cdumm.semantic.parser import parse_records
    records = parse_records("kvtest", new_body, header)
    assert records[1]["_alpha"] == 0xAABBCCDD   # the TID itself, untouched
    assert records[1]["_beta"] == 0xCAFEF00D    # written at TID+4
    assert records[1]["_gamma"] == 0x9999       # untouched


def test_tid_not_found_in_entry_skips_write(
        synth_schema, field_schema_root):
    """If the TID isn't in this entry's payload, the writer must
    leave the body unchanged — it must NOT match a TID belonging
    to another entry."""
    body, header = _build_pabgb([
        _entry_u32_id(1, "First", 0x11111111, 0, 0),
        _entry_u32_id(2, "Second", 0x22222222, 0, 0),
    ], keys=[1, 2])
    _write_field_schema(field_schema_root, "kvtest", {
        "tidField": {
            "tid": "0x22222222",   # only in Second's _alpha
            "value_offset": 4,
            "type": "u32",
        },
    })
    # Intent on First — TID 0x22222222 is NOT in First's blob.
    intents = [Format3Intent(
        entry="First", key=1, field="tidField",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "kvtest", body, header, intents)
    # Bytes unchanged: TID search bounded to First's blob found nothing
    assert new_body == body


def test_tid_search_refuses_write_on_multiple_matches(
        synth_schema, field_schema_root):
    """A 4-byte TID has a low but nonzero collision rate inside a
    real entry payload — values inside CString text, integer
    constants, and other fields can match by chance.

    JMM v9.9.3 takes the FIRST match silently. CDUMM does
    something safer: refuse to write when the TID appears more
    than once in the entry's blob. The author can either pick a
    different TID, narrow with rel_offset, or add disambiguating
    context.

    Test: synthtest entry where TID bytes appear at TWO positions
    inside the payload (here as both _alpha and _beta values).
    The writer must NOT clobber bytes at either match."""
    tid_value = 0x12345678
    body, header = _build_pabgb([
        # _alpha and _beta both equal the TID — two matches
        _entry_u32_id(1, "First", tid_value, tid_value, 0xCAFE),
    ], keys=[1])
    _write_field_schema(field_schema_root, "kvtest", {
        "ambiguous": {
            "tid": tid_value, "value_offset": 4, "type": "u32",
        },
    })

    intents = [Format3Intent(
        entry="First", key=1, field="ambiguous",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "kvtest", body, header, intents)

    # Body must be unchanged — refusing to guess which match was
    # meant is safer than silently corrupting one of them.
    assert new_body == body


def test_tid_search_bound_excludes_next_entry_header(
        synth_schema, field_schema_root):
    """Adjacent-entry collision guard.

    The TID search runs on [payload_start, entry_end). entry_end
    equals the NEXT entry's start — so a 4-byte TID matching the
    next entry's u32 entry_id (which sits at entry_end..entry_end+4)
    would return a write position inside that next entry's
    name_len bytes. Tighten the upper bound to entry_end - eid_size
    - 4 (i.e. the latest position where a 4-byte TID can fit
    entirely inside the current entry's payload).

    This test pins the bound: a TID that exists ONLY at the start
    of the next entry must NOT match for the current entry's
    intent.
    """
    # First entry has _alpha=0x11111111 (no match), second has
    # entry_id=2 at its first 4 bytes which we'll target as the
    # TID in field_schema.
    body, header = _build_pabgb([
        _entry_u32_id(1, "First", 0x11111111, 0x77777777, 0xAAAA),
        _entry_u32_id(2, "Second", 0x33333333, 0x44444444, 0xBBBB),
    ], keys=[1, 2])
    # Pick a TID that ONLY appears as the entry_id of Second.
    # Second's entry header starts at the offset of Second; the
    # entry_id u32 there is 2 → bytes 02 00 00 00. If the bound
    # is loose, the writer searching record 1 would match Second's
    # entry_id at the boundary and write into Second's name_len.
    _write_field_schema(field_schema_root, "kvtest", {
        "tidField": {
            "tid": 0x00000002,   # bytes 02 00 00 00
            "value_offset": 4,
            "type": "u32",
        },
    })

    intents = [Format3Intent(
        entry="First", key=1, field="tidField",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "kvtest", body, header, intents)

    # The body must be unchanged — the TID for the intent on
    # record 1 lives only in record 2's header bytes, which are
    # outside record 1's bound.
    assert new_body == body


# ── Fallback: PABGB schema field name still works ──────────────────


def test_pabgb_schema_fallback_when_no_field_schema_entry(
        synth_schema, field_schema_root):
    """If field_schema has no matching entry, fall back to the
    existing PABGB schema-name path. This is the path Phase 1+2
    already supported — must keep working."""
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0x11111111, 0x22222222, 0x33)],
        keys=[1],
    )
    # Empty field_schema — nothing to translate
    _write_field_schema(field_schema_root, "kvtest", {})

    intents = [Format3Intent(
        entry="First", key=1, field="_alpha",   # PABGB schema name
        op="set", new=0xAAAAAAAA)]
    new_body = apply_intents_to_pabgb_bytes(
        "kvtest", body, header, intents)

    from cdumm.semantic.parser import parse_records
    records = parse_records("kvtest", new_body, header)
    assert records[1]["_alpha"] == 0xAAAAAAAA


def test_field_schema_entry_takes_precedence_over_pabgb_schema(
        synth_schema, field_schema_root):
    """When a name appears in BOTH field_schema and PABGB schema,
    field_schema wins — that's the intended override mechanism for
    community-authored mappings.

    Uses a non-underscore key because JMM convention treats
    underscore-prefixed keys as comments (and CDUMM matches it).
    Real-world Format 3 mods use friendly names like 'drops',
    'attack', 'price' — never the engine's underscore-prefixed
    reader names — so this is the realistic case anyway.
    """
    body, header = _build_pabgb(
        [_entry_u32_id(1, "First", 0x11111111, 0x22222222, 0x33)],
        keys=[1],
    )
    # Add a 'beta' alias pointing at rel_offset=4 in field_schema.
    # PABGB schema has '_beta' at the same byte offset. The test
    # confirms an intent on the friendly name 'beta' resolves via
    # field_schema and lands on the same bytes the underscore name
    # would have hit.
    _write_field_schema(field_schema_root, "kvtest", {
        "beta": {"rel_offset": 4, "type": "u32"},
    })

    intents = [Format3Intent(
        entry="First", key=1, field="beta",
        op="set", new=0xDEADBEEF)]
    new_body = apply_intents_to_pabgb_bytes(
        "kvtest", body, header, intents)

    from cdumm.semantic.parser import parse_records
    records = parse_records("kvtest", new_body, header)
    # field_schema's 'beta' alias resolved to PABGB schema's _beta
    assert records[1]["_alpha"] == 0x11111111   # untouched
    assert records[1]["_beta"] == 0xDEADBEEF    # written via field_schema


# ── validate_intents must classify field_schema fields as supported ─


def test_validator_accepts_field_schema_resolved_field(
        synth_schema, field_schema_root):
    """A friendly name like 'drops' (not in PABGB schema) must be
    classified as supported when a field_schema entry exists."""
    _write_field_schema(field_schema_root, "kvtest", {
        "myField": {"tid": "0xAA", "type": "i32"},
    })
    intents = [Format3Intent(
        entry="X", key=1, field="myField", op="set", new=42)]
    result = validate_intents("kvtest.pabgb", intents)
    assert len(result.supported) == 1
    assert len(result.skipped) == 0


def test_validator_still_skips_unmapped_field(
        synth_schema, field_schema_root):
    """A friendly name absent from BOTH field_schema AND PABGB
    schema must remain in skipped with a clear reason — so the
    user knows they need to author a field_schema entry."""
    _write_field_schema(field_schema_root, "kvtest", {})
    intents = [Format3Intent(
        entry="X", key=1, field="totallyMadeUp",
        op="set", new=42)]
    result = validate_intents("kvtest.pabgb", intents)
    assert len(result.supported) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    # Reason should now mention field_schema as the resolution path
    assert ("field_schema" in reason.lower()
            or "totallyMadeUp" in reason
            or "friendly" in reason.lower())
