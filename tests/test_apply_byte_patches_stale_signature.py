"""Stale-signature fallback: when a mod ships a `signature` field but
its offsets are actually absolute, apply must still work.

Bug from ecbrown777 / jeffersonalves71-png on GitHub (issues #54 and
#53), 2026-04-28: Max Inventory Storage v1.04.02 imports cleanly in
JMM but CDUMM rejects it with "9 byte patches don't match — game
data has changed". The user insists the offsets are valid for the
current game.

Phase 1 investigation (verified against the user's actual mod and
live vanilla iteminfo.pabgb):

  * The mod's expected `original` bytes match vanilla EXACTLY at
    every absolute offset (3218→0x3200, 3220→0xf000, etc).
  * The mod's JSON includes a `signature` field
    "02000900000043686172616374657200" (the "Character\\0" entry
    header).
  * That signature is found at byte 391 in vanilla.
  * `_apply_byte_patches` honors the signature when present and
    treats every offset as relative to the end of the match,
    landing every patch ~407 bytes past the intended target —
    where the bytes don't match anything.

Conclusion: the mod author intended absolute offsets but left a
stale `signature` field in the JSON. JMM evidently doesn't apply
the signature mode (or applies it differently). CDUMM should detect
this case and fall back to absolute offsets when signature-relative
produces zero applies but absolute would succeed.

Fix design:
  1. Run signature-relative as today (preserve current behavior
     for mods that genuinely use anchored offsets).
  2. If applied == 0 AND mismatched > 0 AND len(data) is large
     enough, retry the same changes with signature=None (absolute).
  3. If the absolute retry produces a strictly better result
     (more applied, fewer mismatched), keep that result and log a
     "stale-signature fallback" warning naming the mod.
  4. Otherwise return the original signature-relative result.
"""
from __future__ import annotations

import struct

from cdumm.engine.json_patch_handler import _apply_byte_patches


def _build_vanilla_with_signature_at(sig_pos: int, payload_size: int = 4000) -> bytearray:
    """Build a synthetic vanilla buffer with a 'Character\\0' signature
    at `sig_pos` and known byte values at later offsets so we can
    verify which interpretation (absolute vs sig-relative) lands
    correctly."""
    buf = bytearray(payload_size)
    # Random-ish noise so the signature isn't accidentally produced
    # elsewhere by zeros.
    for i in range(payload_size):
        buf[i] = (i * 37 + 11) & 0xFF
    sig = bytes.fromhex("02000900000043686172616374657200")
    buf[sig_pos:sig_pos + len(sig)] = sig
    return buf


def test_stale_signature_falls_back_to_absolute_when_all_sig_relative_fail():
    """Mod ships a signature but its offsets are actually absolute.
    Signature-relative interpretation produces 0 matches; absolute
    matches all 3 patches. Must apply via absolute fallback."""
    sig_pos = 391
    data = _build_vanilla_with_signature_at(sig_pos, payload_size=5000)

    # Pick three offsets WAY past the signature so absolute and
    # sig-relative interpretations land on different bytes.
    abs_offsets = [3218, 3220, 4108]
    expected_at_abs = [data[o:o+2].hex() for o in abs_offsets[:2]] + [
        data[abs_offsets[2]:abs_offsets[2]+4].hex()]

    changes = [
        {"offset": 3218, "original": expected_at_abs[0], "patched": "5802"},
        {"offset": 3220, "original": expected_at_abs[1], "patched": "bc02"},
        {"offset": 4108, "original": expected_at_abs[2], "patched": "b004b004"},
    ]

    # The mod ships a signature that matches the in-data marker. With
    # current code, signature-relative would land at sig_end + 3218 etc.
    # — bytes that don't match. The fallback must rescue this.
    sig_hex = "02000900000043686172616374657200"
    applied, mismatched, _ = _apply_byte_patches(
        bytearray(data), changes, signature=sig_hex)

    assert applied == 3, (
        f"All 3 patches must apply via absolute-offset fallback. "
        f"Got applied={applied}, mismatched={mismatched}.")
    assert mismatched == 0


def test_genuine_signature_relative_still_works():
    """Regression guard: when a mod's offsets ARE intended to be
    signature-relative (no absolute interpretation matches), the
    existing behavior must be preserved."""
    sig_pos = 100
    data = _build_vanilla_with_signature_at(sig_pos, payload_size=2000)
    sig_len = 16

    # Place a known byte sequence at a position relative to the
    # signature. Then craft a change with the relative offset.
    rel_offset = 50
    abs_pos = sig_pos + sig_len + rel_offset
    data[abs_pos:abs_pos + 4] = b"\x11\x22\x33\x44"
    # Critical: make sure ABSOLUTE offset rel_offset has DIFFERENT bytes
    # so a fallback to absolute would NOT match. (Otherwise the test
    # can't distinguish which mode applied.)
    data[rel_offset:rel_offset + 4] = b"\xaa\xbb\xcc\xdd"

    changes = [{"offset": rel_offset, "original": "11223344", "patched": "ffffffff"}]
    sig_hex = "02000900000043686172616374657200"

    applied, mismatched, _ = _apply_byte_patches(
        bytearray(data), changes, signature=sig_hex)
    assert applied == 1, (
        f"Sig-relative apply must still work for mods that genuinely "
        f"use anchored offsets. applied={applied} mismatched={mismatched}")
    assert mismatched == 0


def test_no_signature_unchanged_behavior():
    """Regression guard: when no signature is provided, behavior is
    unchanged — absolute offsets, no fallback logic engaged."""
    data = bytearray(b"\x00" * 100)
    data[10:14] = b"\x11\x22\x33\x44"

    changes = [{"offset": 10, "original": "11223344", "patched": "ffffffff"}]
    applied, mismatched, _ = _apply_byte_patches(bytearray(data), changes)
    assert applied == 1
    assert mismatched == 0


def test_partial_signature_match_does_not_trigger_fallback():
    """If signature-relative produces SOME matches (even if not all),
    don't trigger the fallback — that signature was clearly intentional.
    Only the all-fail case warrants overriding the author's choice."""
    sig_pos = 200
    data = _build_vanilla_with_signature_at(sig_pos, payload_size=3000)
    sig_len = 16

    # One change matches sig-relative, one doesn't (and absolute won't
    # match either). Result: applied=1, mismatched=1. No fallback.
    abs_for_match = sig_pos + sig_len + 50
    data[abs_for_match:abs_for_match + 4] = b"\x55\x66\x77\x88"

    abs_for_miss = sig_pos + sig_len + 100
    # Set bytes that don't match anything we'll try
    data[abs_for_miss:abs_for_miss + 4] = b"\xde\xad\xbe\xef"

    changes = [
        {"offset": 50, "original": "55667788", "patched": "ffffffff"},
        # This one expects "12345678" (won't match anywhere) — won't
        # apply via either interpretation.
        {"offset": 100, "original": "12345678", "patched": "ffffffff"},
    ]
    sig_hex = "02000900000043686172616374657200"

    applied, mismatched, _ = _apply_byte_patches(
        bytearray(data), changes, signature=sig_hex)
    # Sig-relative: change[0] applies, change[1] mismatches → applied=1, mismatched=1
    # Should NOT fall back to absolute (the partial sig-relative result
    # is the legitimate one).
    assert applied == 1, f"Expected applied=1 (partial sig success); got {applied}"


def test_stale_signature_with_absolute_partial_match_picks_better():
    """When sig-relative produces 0/N applied but absolute produces M/N
    where M > 0, the fallback must run AND keep the absolute results
    (better than nothing)."""
    sig_pos = 500
    data = _build_vanilla_with_signature_at(sig_pos, payload_size=5000)

    # Two patches with absolute interpretations matching, one whose
    # absolute interpretation does NOT match.
    data[1000:1002] = b"\xaa\xbb"
    data[2000:2002] = b"\xcc\xdd"
    # 1500 has random noise, won't match anything specific

    changes = [
        {"offset": 1000, "original": "aabb", "patched": "0011"},
        {"offset": 1500, "original": "deadbeef", "patched": "00112233"},
        {"offset": 2000, "original": "ccdd", "patched": "0022"},
    ]
    sig_hex = "02000900000043686172616374657200"

    applied, mismatched, _ = _apply_byte_patches(
        bytearray(data), changes, signature=sig_hex)
    # Sig-relative: 0/3 apply (offsets land in random noise). Fallback
    # to absolute: 2/3 apply (1000 and 2000 match, 1500 doesn't). Keep
    # absolute since 2 > 0.
    assert applied == 2, (
        f"Fallback should keep the absolute interpretation (2 applies) "
        f"because it's strictly better than sig-relative (0 applies). "
        f"Got applied={applied}.")
