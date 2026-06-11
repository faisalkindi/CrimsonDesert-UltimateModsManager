"""Format 3 primitive intents silently failed at apply time because
format3_apply emitted `rel_offset = abs_off - entry_off`
(record-start-relative), but the apply pipeline
(`_apply_byte_patches`) resolves rel_offset against `name_end`
(= entry_off + eid_size + 4 + name_len). The two coordinate systems
disagree by `8 + name_len` bytes per record, so the apply landed
`8 + name_len` bytes past the actual field and read garbage from
adjacent bytes. The verification then rejected 1812/1827 patches
with byte mismatches (Faisal's Can It Stack JSON V3 test,
2026-05-01).

Fix: format3_apply must emit `rel_offset = abs_off - name_end` so
the round-trip through `_apply_byte_patches` lands at `abs_off`.

Bug was latent since v3.2.3 when Format 3 primitive support
shipped. ZirconX1 / Lichtnocht / others' "applies cleanly but does
nothing in-game" reports trace here.

Vehicle note (2026-06-11): these tests originally used iteminfo as
the vehicle. Audit finding C routed ALL iteminfo intents through
the native whole-table writer (the schema walk still carries the
pre-1.09 layout), so iteminfo no longer exercises the generic
rel_offset path. The convention still applies to every other
schema-walked table; `fieldinfo` has the same leading layout the
old vehicle relied on (_isBlocked u8 at name_end+0, then a
primitive at name_end+1, no_null_skip=True).
"""
from __future__ import annotations
import struct
import pytest

_TABLE = "fieldinfo.pabgb"
_ENTRY = "Test_Field_Entry"


def _build_minimal_pair():
    """Build a synthetic fieldinfo.pabgb + .pabgh pair with one
    record so we can exercise the Format 3 → V2 → apply round-trip
    without needing the live game.

    fieldinfo schema (per get_schema('fieldinfo')):
      field 0: _isBlocked (direct_u8, 1 byte)
      field 1: _fieldType1 (direct_u32, 4 bytes)
      ...
    no_null_skip=True so the payload starts AT name_end.
    """
    name = _ENTRY.encode()
    name_len = len(name)
    entry_key = 2200

    record_body = (
        struct.pack("<I", entry_key)
        + struct.pack("<I", name_len)
        + name
        + struct.pack("<B", 0)               # _isBlocked = 0
        + struct.pack("<I", 100)             # _fieldType1 = 100
        + b"\x00" * 64                        # filler so walker has room
    )

    pabgb_body = record_body
    # pabgh: u16 count, then count * (u32 key/hash, u32 offset).
    pabgh = struct.pack("<H", 1) + struct.pack("<II", entry_key, 0)
    return pabgb_body, pabgh, entry_key, name_len


def test_format3_primitive_rel_offset_relative_to_name_end():
    """Direct assertion: the `rel_offset` emitted by Format 3 expansion
    must be relative to `name_end` (matching V2 JMM/SWISS Knife
    convention), not relative to `entry_off`.

    For fieldinfo (no_null_skip=True), name_end = entry_off + 8 +
    name_len, payload starts at name_end, _isBlocked is at name_end+0,
    _fieldType1 is at name_end+1. So rel_offset for field_type1
    should be 1, not 8 + name_len + 1."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )

    body, header, entry_key, name_len = _build_minimal_pair()
    SENTINEL = 9999999
    intent = Format3Intent(
        entry=_ENTRY, key=entry_key,
        field="field_type1", op="set", new=SENTINEL,
    )
    v = validate_intents(_TABLE, [intent])
    if not v.supported:
        pytest.skip(f"validator skipped: {v.skipped}")
    changes = _intents_to_v2_changes(
        _TABLE, body, header, v.supported)
    assert changes, "_intents_to_v2_changes produced 0 changes"
    rel = changes[0]["rel_offset"]

    # _fieldType1 is the SECOND field (after _isBlocked u8) at
    # name_end + 1. rel_offset must be 1 so the apply lands there.
    assert rel == 1, (
        f"rel_offset = {rel}, expected 1 (name-end-relative). "
        f"format3_apply emits in record-start coords; apply pipeline "
        f"adds rel_offset to name_end. Mismatch causes silent apply "
        f"failure on Format 3 primitive mods. Source: Faisal's Can It "
        f"Stack JSON V3 test 2026-05-01 (1812/1827 patches mismatched)."
    )


def test_format3_primitive_rel_offset_apply_lands_at_correct_position():
    """End-to-end: emit a Format 3 primitive intent, feed the produced
    V2 change through `_apply_byte_patches` against a vanilla buffer,
    and assert the patched bytes land at the actual field position
    (not 8 + name_len bytes past it)."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )
    from cdumm.engine.json_patch_handler import (
        _apply_byte_patches, _build_name_offsets_generic,
    )

    body, header, entry_key, name_len = _build_minimal_pair()

    SENTINEL = 9999999
    intent = Format3Intent(
        entry=_ENTRY, key=entry_key,
        field="field_type1", op="set", new=SENTINEL,
    )
    validation = validate_intents(_TABLE, [intent])
    if not validation.supported:
        pytest.skip(
            f"validator skipped intent in this build: {validation.skipped}"
        )

    changes = _intents_to_v2_changes(
        _TABLE, body, header, validation.supported)
    assert changes, "_intents_to_v2_changes produced 0 changes"
    change = changes[0]

    # The V2 apply path resolves entry name → name_end via
    # _build_name_offsets_generic. Run that same resolver here so the
    # test mirrors the production apply pipeline.
    name_offsets = _build_name_offsets_generic(body, header)
    assert name_offsets, "name_offsets resolver returned None"
    assert _ENTRY in name_offsets

    # Now apply the change through the real apply path.
    data = bytearray(body)
    skipped = []
    applied, mismatched, _relocated = _apply_byte_patches(
        data, [change],
        name_offsets=name_offsets,
        skipped_out=skipped,
    )

    # The apply MUST succeed (1 applied, 0 mismatched). If it fails
    # with byte mismatch, the rel_offset coordinate system is wrong
    # (the bug we're fixing).
    assert mismatched == 0, (
        f"apply rejected the patch with byte mismatch, rel_offset "
        f"is off relative to name_end. Skipped: {skipped!r}"
    )
    assert applied == 1, (
        f"apply produced {applied} writes, expected 1. Skipped: {skipped!r}"
    )

    # Verify the SENTINEL value is at the actual _fieldType1 byte
    # position: entry_off (0) + header (8) + name + _isBlocked (1).
    actual_value = struct.unpack_from("<I", data, 8 + name_len + 1)[0]
    assert actual_value == SENTINEL, (
        f"_fieldType1 after apply = {actual_value}, "
        f"expected {SENTINEL}. The patch landed in the wrong byte "
        f"position."
    )


def test_iteminfo_primitive_routes_to_native_writer():
    """Audit finding C: iteminfo primitives must NOT take the schema
    walk anymore (the walker schema still carries the pre-1.09
    layout). A primitive-only iteminfo intent batch must come back
    either as a whole-table change from the native writer (real
    table) or as nothing at all (writer refused this body), never
    as a per-record rel_offset change from the walk."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )

    # Reuse the synthetic builder shape under the iteminfo name: the
    # native writer cannot parse this stub (and must refuse), and the
    # walk must not pick it up either.
    name = b"Pyeonjeon_Arrow"
    body = (
        struct.pack("<I", 2200) + struct.pack("<I", len(name)) + name
        + struct.pack("<B", 0) + struct.pack("<Q", 100) + b"\x00" * 64
    )
    header = struct.pack("<H", 1) + struct.pack("<II", 2200, 0)

    intent = Format3Intent(
        entry="Pyeonjeon_Arrow", key=2200,
        field="max_stack_count", op="set", new=9999999,
    )
    v = validate_intents("iteminfo.pabgb", [intent])
    if not v.supported:
        pytest.skip(f"validator skipped: {v.skipped}")
    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", body, header, v.supported)
    walk_changes = [c for c in changes if "rel_offset" in c]
    assert not walk_changes, (
        "iteminfo primitive intent took the stale schema walk "
        "(audit finding C regression)")
