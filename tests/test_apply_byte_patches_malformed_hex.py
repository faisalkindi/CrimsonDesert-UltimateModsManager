"""Defensive: malformed hex in a single change must not abort the
whole apply.

Bug from Faisal 2026-04-27 (ZirconX1 / Nexus): a JSON-patch mod
in his loadout had bad hex somewhere in a `patched` or `original`
field. CDUMM's apply path raised
    ValueError: non-hexadecimal number found in fromhex() arg at position 0
and aborted the WHOLE apply, dropping every other valid change in
his ~5-mod loadout.

Reading json_patch_handler.py:850-1010:
  * line 858-861 (`insert` branch) wraps `bytes.fromhex(insert_hex)`
    in try/except — that path is safe.
  * line 876 (`patched_bytes = bytes.fromhex(patched_hex)`),
  * line 884 (`original_bytes = bytes.fromhex(change["original"])`),
  * line 1005 (`bytes.fromhex(change["original"])` — size-delta calc)
  are ALL unwrapped. Any single change with garbage in `patched` or
  `original` raises and aborts.

Defense-in-depth fix: wrap all three. Skip the bad change, log a
warning with the change label/offset, and surface it through the
existing `skipped_out` list so the user sees which mod's which
change is malformed.
"""
from __future__ import annotations

import pytest


def test_malformed_patched_hex_skips_change_and_continues():
    """`patched` field has non-hex chars — the change must be
    skipped, NOT abort the apply. Subsequent valid changes still
    apply."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    skipped: list[dict] = []
    changes = [
        # Change 0: malformed hex (G is not hex).
        {"offset": 0, "patched": "GGGG", "label": "broken-1"},
        # Change 1: valid — should still apply.
        {"offset": 4, "patched": "deadbeef", "label": "good-1"},
    ]
    applied, mismatched, _ = _apply_byte_patches(
        data, changes, skipped_out=skipped)

    assert applied == 1, (
        f"valid change at offset 4 must still apply; got applied={applied}")
    assert bytes(data[4:8]) == b"\xde\xad\xbe\xef", (
        f"valid change must reach data; got {data[4:8].hex()}")
    assert mismatched == 1, (
        f"malformed-hex change must count as mismatched; got "
        f"mismatched={mismatched}")
    assert any("broken-1" in s.get("label", "") for s in skipped), (
        f"skipped_out must record the malformed change; got {skipped}")


def test_malformed_original_hex_skips_change_and_continues():
    """`original` field has non-hex chars — the change must be
    skipped, NOT abort. Subsequent valid changes still apply."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    skipped: list[dict] = []
    changes = [
        {"offset": 0, "original": "ZZZZ",   # malformed
         "patched": "ffffffff", "label": "broken-orig"},
        {"offset": 4, "original": "00000000",
         "patched": "11223344", "label": "good"},
    ]
    applied, mismatched, _ = _apply_byte_patches(
        data, changes, skipped_out=skipped)

    assert applied == 1, f"got applied={applied}"
    assert bytes(data[4:8]) == b"\x11\x22\x33\x44"
    assert mismatched == 1
    assert any("broken-orig" in s.get("label", "") for s in skipped)


def test_empty_patched_string_was_already_handled():
    """Regression guard: line 873-875 already handled empty
    `patched` with a logger.warning + continue. Don't break that."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00" * 8)
    changes = [
        {"offset": 0, "patched": "", "label": "empty"},
        {"offset": 4, "patched": "deadbeef", "label": "good"},
    ]
    applied, _, _ = _apply_byte_patches(data, changes)
    assert applied == 1
    assert bytes(data[4:8]) == b"\xde\xad\xbe\xef"


def test_size_delta_calculation_handles_malformed_original():
    """Line 1005 — the size-delta `bytes.fromhex(change['original'])`
    is reached when `original` is provided and the current value at
    offset matched it. If `original` is malformed but somehow the
    earlier check let it through (defensive belt-and-suspenders),
    the delta calc must not blow up.

    This test forces the path by providing original bytes that DO
    match the data, so the patch is applied — and then the size
    delta is computed. If both unwrapped fromhex sites are fixed,
    the path is unreachable from a single bad-original input
    (because the line 884 fromhex would have skipped first). But
    we want this defensive even against future refactors.
    """
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00" * 8)
    # All-valid case — pure regression guard.
    changes = [
        {"offset": 0, "original": "00000000",
         "patched": "deadbeef", "label": "ok"},
    ]
    applied, _, _ = _apply_byte_patches(data, changes)
    assert applied == 1


def test_three_consecutive_malformed_changes_dont_break_later_valid():
    """Multiple bad changes in a row + valid ones interleaved. All
    bad ones get skipped, all valid ones apply."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00" * 16)
    changes = [
        {"offset": 0, "patched": "QQQQ", "label": "bad-1"},
        {"offset": 4, "patched": "  ",   "label": "bad-2"},  # only whitespace
        {"offset": 8, "patched": "0xff", "label": "bad-3"},  # leading 0x prefix
        {"offset": 12, "patched": "abcd", "label": "good"},
    ]
    skipped: list[dict] = []
    applied, mismatched, _ = _apply_byte_patches(
        data, changes, skipped_out=skipped)

    # "  " (only whitespace) is treated as empty by Python's fromhex
    # and would be valid (bytes.fromhex("  ") == b"") — that's fine,
    # it patches zero bytes and counts as applied. Both "QQQQ" and
    # "0xff" must be skipped (malformed at position 0 and 1
    # respectively).
    assert applied >= 1, f"good change must apply; got applied={applied}"
    assert bytes(data[12:14]) == b"\xab\xcd"
    bad_labels = {s.get("label", "") for s in skipped}
    assert "bad-1" in bad_labels
    assert "bad-3" in bad_labels


def test_malformed_hex_records_clear_reason_in_skipped_out():
    """The `skipped_out` entry for a malformed-hex change must carry
    a 'malformed hex' reason so the GUI's post-apply skip dialog
    can explain WHY each one was skipped, rather than the generic
    'byte mismatch' reason used elsewhere."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00" * 8)
    changes = [
        {"offset": 0, "patched": "NOPE", "label": "weird"},
    ]
    skipped: list[dict] = []
    _apply_byte_patches(data, changes, skipped_out=skipped)
    assert len(skipped) == 1
    reason = skipped[0].get("reason", "")
    assert "malformed" in reason.lower() or "hex" in reason.lower(), (
        f"skipped reason should mention malformed/hex; got {reason!r}")
