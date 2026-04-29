"""A mod with a malformed `signature` field must not crash the
whole apply.

Round-5 systematic-debugging finding: the fix shipped in v3.2.4
that wrapped `patched` and `original` hex parsing missed a third
fromhex site at json_patch_handler.py:726, where the SIGNATURE
itself is parsed. A mod with garbage in its signature field
(e.g., a typo, a quoted hex with surrounding "0x", trailing
newline) raises ValueError before any patch even gets attempted.
That ValueError propagates up through _apply_byte_patches into
the apply pipeline, killing the entire mod's apply for ALL its
patches.

The defense-in-depth pattern from the v3.2.4 fix applies: catch
the ValueError, log it clearly naming the bad signature, treat
the patch as if no signature were provided (absolute offsets).
The new stale-signature fallback already handles "signature found
but doesn't help" — extending it to "signature couldn't even be
parsed" is the natural completion.
"""
from __future__ import annotations

from cdumm.engine.json_patch_handler import _apply_byte_patches


def test_malformed_signature_does_not_crash_apply():
    """A mod with garbage in `signature` must produce a clean
    'no patches applied' result (or successful absolute fallback),
    not raise ValueError."""
    data = bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    changes = [{"offset": 0, "original": "00000000", "patched": "ffffffff"}]

    # Signature contains non-hex chars — should not crash.
    applied, mismatched, _ = _apply_byte_patches(
        data, changes, signature="GG_NOT_HEX")
    # The patch's `original` matches absolute offset 0 in data
    # (which is all zeros). Absolute fallback should apply it.
    assert applied == 1, (
        f"Malformed signature must be treated as absent and the "
        f"patch should apply via absolute offsets. applied={applied}")


def test_signature_with_leading_0x_prefix_does_not_crash():
    """A common author mistake: writing `\"signature\": \"0x...\"`
    instead of bare hex. Must not crash."""
    data = bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    changes = [{"offset": 4, "original": "00000000", "patched": "11223344"}]

    applied, _, _ = _apply_byte_patches(
        data, changes, signature="0xff00aa55")
    # Same shape — should not crash; absolute fallback should apply.
    assert applied == 1


def test_empty_string_signature_treated_as_absent():
    """Empty string signature should behave like None — no
    signature processing."""
    data = bytearray(b"\x00\x00\x00\x00")
    changes = [{"offset": 0, "original": "00000000", "patched": "ffffffff"}]

    applied, _, _ = _apply_byte_patches(data, changes, signature="")
    assert applied == 1


def test_odd_length_signature_does_not_crash():
    """Hex strings must have even length. An odd-length signature
    raises ValueError in fromhex — must be caught."""
    data = bytearray(b"\x00\x00\x00\x00")
    changes = [{"offset": 0, "original": "00000000", "patched": "ffffffff"}]

    applied, _, _ = _apply_byte_patches(
        data, changes, signature="abc")  # odd length
    assert applied == 1
