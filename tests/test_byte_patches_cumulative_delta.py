"""CRITICAL #2 + HIGH #9: cumulative_delta must respect where the write actually happened.

_apply_byte_patches sorts changes by PRIMARY offset, then walks in order
tracking a single cumulative_delta. When a patch takes a FALLBACK offset
that lands outside the sort window (e.g. primary=0x100 but fallback
lands at 0x250, between the next patches), the shift from that write
should only affect patches with primary >= 0x250 — not all subsequent
patches in the sort.

The current single-counter design shifts all later patches by every
earlier patch's delta regardless of where the actual write happened.
"""
from __future__ import annotations

from cdumm.engine.json_patch_handler import _apply_byte_patches


def test_fallback_write_above_next_primary_does_not_shift_next_primary():
    """
    Fallback-write tracking: an earlier patch's fallback that lands HIGHER
    than a later patch's primary must NOT shift that later patch.

    Layout:
      A anchored primary at 0x10 (bytes 'ff' — drift) → fallback literal
        offset 0x50 (bytes 'aa' — real). Fallback write expands 2 → 4 bytes.
      B primary at 0x20 (bytes 'bb'). Because A's write at 0x50 is ABOVE B's
        primary 0x20, B must NOT shift. The vanilla-remnant branch is taken
        (vanilla_data has 'bb' at 0x20), and that branch writes directly at
        `offset`. If cumulative_delta has been inflated by A's fallback, the
        remnant branch writes at 0x22 — wrong.
    """
    data = bytearray(b"\xff" * 0x80)
    vanilla = bytearray(b"\xff" * 0x80)
    vanilla[0x20:0x22] = b"\xbb\xbb"
    data[0x20:0x22] = b"\xbb\xbb"
    data[0x50:0x52] = b"\xaa\xaa"

    # Reach the vanilla-remnant branch by corrupting bytes at 0x20 AFTER
    # vanilla is captured: set data[0x20:0x22] to 'ffbb' so the primary
    # check mismatches ('bbbb' vs 'ffbb') but vanilla still has 'bbbb'.
    # Remnant branch writes patched at the CURRENT offset.
    # We want that CURRENT offset to be 0x20, not 0x22.
    # So we need the code to NOT over-shift B by A's +2 delta.
    data[0x20:0x22] = b"\xff\xbb"   # primary mismatch, forces remnant/fallback

    changes = [
        {
            "record_key": 5,
            "relative_offset": 0,
            "offset": 0x50,
            "original": "aaaa",
            "patched": "11112222",
        },
        {"offset": 0x20, "original": "bbbb", "patched": "3333"},
    ]
    record_offsets = {5: 0x10}

    applied, mismatched, _r = _apply_byte_patches(
        data, changes, record_offsets=record_offsets,
        vanilla_data=bytes(vanilla))

    # With correct per-write delta tracking, B should resolve correctly.
    # Either pattern_scan found it, fallback list had it, or remnant wrote
    # at a not-over-shifted offset. End state must have 3333 at 0x20.
    assert data[0x50:0x54] == b"\x11\x11\x22\x22", "A fallback must apply"
    assert data[0x20:0x22] == b"\x33\x33" or data[0x1e:0x20] == b"\x33\x33", (
        f"B wrote at wrong offset (over-shifted by A's fallback delta): "
        f"data[0x1e:0x24]={data[0x1e:0x24].hex()}")
    # Whatever happens, applied must equal 2 and mismatched==0.
    assert applied == 2 and mismatched == 0, (
        f"applied={applied} mismatched={mismatched}")


def test_primary_write_below_next_primary_still_shifts_next():
    """
    Control case: normal sorted primary writes must still shift later patches.
    A expands at 0x10 by 2 bytes. B's primary is 0x30 > 0x10. B must shift by 2.
    """
    data = bytearray(b"\xff" * 0x80)
    data[0x10:0x12] = b"\xaa\xaa"   # A's primary
    data[0x30:0x32] = b"\xbb\xbb"   # B's primary

    changes = [
        {"offset": 0x10, "original": "aaaa", "patched": "11112222"},  # +2
        {"offset": 0x30, "original": "bbbb", "patched": "3333"},
    ]

    applied, _m, _r = _apply_byte_patches(data, changes)
    assert applied == 2
    # A took 2 extra bytes at 0x10, so B shifted to 0x32
    assert data[0x32:0x34] == b"\x33\x33", (
        f"B should have shifted +2: data[0x30:0x36]={data[0x30:0x36].hex()}")


def test_fallback_bounds_clamp_uses_cumulative_delta():
    """
    HIGH #9: the fallback bounds-overflow check must use the delta-adjusted
    offset. Otherwise a fallback that fits in the CURRENT data but would
    overflow if delta were ignored could be wrongly skipped.
    """
    # Pre-expand data with an insert so later offsets are shifted by delta.
    data = bytearray(b"\xff" * 0x40)
    data[0x10:0x12] = b"\xaa\xaa"
    data[0x30:0x32] = b"\xbb\xbb"

    changes = [
        # Insert 4 bytes at 0x0 to create a cumulative_delta > 0 when B runs.
        {"offset": 0x0, "type": "insert", "bytes": "deadbeef"},
        # B's literal 0x30 won't match bytes (we inserted 4 bytes ahead);
        # fallback entry-anchor resolves to 0x30 (i.e. 0x34 post-delta).
        {
            "offset": 0x99,          # primary won't match (bytes are 'ff')
            "entry": "B",
            "rel_offset": 0,
            "original": "bbbb",
            "patched": "3333",
        },
    ]
    name_offsets = {"B": 0x30}

    applied, mismatched, _r = _apply_byte_patches(
        data, changes, name_offsets=name_offsets)

    assert applied == 2, f"both should apply, got applied={applied} mismatched={mismatched}"
    # After 4-byte insert at 0x0, original 0x30 is now at 0x34
    assert data[0x34:0x36] == b"\x33\x33", (
        f"fallback bounds should have used delta-adjusted offset, "
        f"data around 0x34={data[0x30:0x38].hex()}")
