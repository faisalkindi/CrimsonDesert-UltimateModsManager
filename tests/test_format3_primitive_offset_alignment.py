"""Format 3 primitive intents (e.g. `max_stack_count` on iteminfo)
silently failed at apply time because format3_apply emitted
`rel_offset = abs_off - entry_off` (record-start-relative), but the
apply pipeline (`_apply_byte_patches`) resolves rel_offset against
`name_end` (= entry_off + eid_size + 4 + name_len). The two
coordinate systems disagree by `8 + name_len` bytes per record, so
the apply landed `8 + name_len` bytes past the actual field and
read garbage from adjacent bytes. The verification then rejected
1812/1827 patches with byte mismatches (Faisal's Can It Stack JSON
V3 test, 2026-05-01).

Fix: format3_apply must emit `rel_offset = abs_off - name_end` so
the round-trip through `_apply_byte_patches` lands at `abs_off`.

Bug was latent since v3.2.3 when Format 3 primitive support
shipped. ZirconX1 / Lichtnocht / others' "applies cleanly but does
nothing in-game" reports trace here.
"""
from __future__ import annotations
import struct
import pytest


def _build_minimal_iteminfo_pair():
    """Build a synthetic iteminfo.pabgb + iteminfo.pabgh pair with
    one record (Pyeonjeon_Arrow, name_len=15) so we can exercise the
    Format 3 → V2 → apply round-trip without needing the live game.
    """
    name = b"Pyeonjeon_Arrow"
    name_len = len(name)  # 15
    entry_key = 2200

    # Iteminfo schema (per get_schema('iteminfo')):
    #   field 0: _isBlocked (u8, 1 byte)
    #   field 1: _maxStackCount (u64, 8 bytes)
    #   field 2: _itemName (LocalizableString, variable)
    #   ...
    # The schema has no_null_skip=True so payload starts AT name_end
    # (not name_end+1). Build record: entry_key, name_len, name,
    # _isBlocked (1 byte), _maxStackCount=100 (u64), then enough
    # filler to pretend there's more content.
    record_body = (
        struct.pack("<I", entry_key)
        + struct.pack("<I", name_len)
        + name
        + struct.pack("<B", 0)               # _isBlocked = 0
        + struct.pack("<Q", 100)             # _maxStackCount = 100
        + b"\x00" * 64                        # filler so walker has room
    )

    pabgb_body = record_body
    # pabgh: u16 count, then count * (u32 key/hash, u32 offset).
    # parse_pabgh_index returns this u32 as the lookup key, so we
    # use entry_key here so the apply pipeline's `intent.key` matches.
    pabgh = struct.pack("<H", 1) + struct.pack("<II", entry_key, 0)
    return pabgb_body, pabgh, entry_key, name_len


def test_format3_primitive_rel_offset_relative_to_name_end():
    """Direct assertion: the `rel_offset` emitted by Format 3 expansion
    must be relative to `name_end` (matching V2 JMM/SWISS Knife
    convention), not relative to `entry_off`. The apply pipeline's
    `_build_name_offsets_generic` anchors entry names at name_end,
    so a wrong-coordinate-system rel_offset causes the apply to land
    `8 + name_len` bytes past the target field.

    For iteminfo (no_null_skip=True), name_end = entry_off + 8 +
    name_len, payload starts at name_end, _isBlocked is at name_end+0,
    _maxStackCount is at name_end+1. So rel_offset for max_stack_count
    should be 1, not 24."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )

    body, header, entry_key, name_len = _build_minimal_iteminfo_pair()
    SENTINEL = 9999999
    intent = Format3Intent(
        entry="Pyeonjeon_Arrow", key=entry_key,
        field="max_stack_count", op="set", new=SENTINEL,
    )
    v = validate_intents("iteminfo.pabgb", [intent])
    if not v.supported:
        pytest.skip(f"validator skipped: {v.skipped}")
    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", body, header, v.supported)
    assert changes, "_intents_to_v2_changes produced 0 changes"
    rel = changes[0]["rel_offset"]

    # _maxStackCount is the SECOND field (after _isBlocked u8) at
    # name_end + 1. rel_offset must be 1 so the apply lands there.
    assert rel == 1, (
        f"rel_offset = {rel}, expected 1 (name-end-relative). "
        f"format3_apply emits in record-start coords; apply pipeline "
        f"adds rel_offset to name_end. Mismatch causes silent apply "
        f"failure on iteminfo Format 3 primitive mods. Source: "
        f"Faisal's Can It Stack JSON V3 test 2026-05-01 (1812/1827 "
        f"patches mismatched)."
    )


def test_format3_primitive_rel_offset_apply_lands_at_correct_position():
    """End-to-end: emit a Format 3 primitive intent (`max_stack_count
    = 9999999` on Pyeonjeon_Arrow), feed the produced V2 change
    through `_apply_byte_patches` against a vanilla buffer, and
    assert the patched bytes land at the actual `max_stack_count`
    byte position (not 8 + name_len bytes past it)."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )
    from cdumm.engine.json_patch_handler import (
        _apply_byte_patches, _build_name_offsets_generic,
    )

    body, header, entry_key, name_len = _build_minimal_iteminfo_pair()

    SENTINEL = 9999999
    intent = Format3Intent(
        entry="Pyeonjeon_Arrow", key=entry_key,
        field="max_stack_count", op="set", new=SENTINEL,
    )
    validation = validate_intents("iteminfo.pabgb", [intent])
    if not validation.supported:
        pytest.skip(
            f"validator skipped intent in this build: {validation.skipped}"
        )

    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", body, header, validation.supported)
    assert changes, "_intents_to_v2_changes produced 0 changes"
    change = changes[0]

    # The V2 apply path resolves entry name → name_end via
    # _build_name_offsets_generic. Run that same resolver here so the
    # test mirrors the production apply pipeline.
    name_offsets = _build_name_offsets_generic(body, header)
    assert name_offsets, "name_offsets resolver returned None"
    assert "Pyeonjeon_Arrow" in name_offsets

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
        f"apply rejected the patch with byte mismatch — rel_offset "
        f"is off relative to name_end. Skipped: {skipped!r}"
    )
    assert applied == 1, (
        f"apply produced {applied} writes, expected 1. Skipped: {skipped!r}"
    )

    # Verify the SENTINEL value is at the actual _maxStackCount byte
    # position: entry_off (0) + header (8) + name (15) + _isBlocked (1)
    # = 24. Read as u64 to match the schema's `Q` format.
    actual_value = struct.unpack_from("<Q", data, 8 + name_len + 1)[0]
    assert actual_value == SENTINEL, (
        f"_maxStackCount after apply = {actual_value}, "
        f"expected {SENTINEL}. The patch landed in the wrong byte "
        f"position."
    )
