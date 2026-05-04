"""Behavior change requested by Faisal 2026-05-04: when a mod has any
byte-mismatch on Apply, the WHOLE mod must be skipped, not just the
mismatching patches. Mods often coordinate multiple patches (max
value at offset A + drain rate at offset B + regen rate at offset C),
landing only some leaves the mod in a half-baked state that's worse
than not applying it at all.

Implementation: pre-validate per-mod against a scratch copy of
vanilla. Any mod whose changes don't all match becomes tainted, and
ALL of its changes are recorded as skipped before the real apply
runs. Other mods are unaffected.

Untagged changes (Format 3 whole-table merged dispatch, no
``_source_mod_id``) keep the per-change policy , there's no mod
attribution to taint with.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_one_mismatch_skips_all_patches_from_same_mod():
    """Mod A has 3 changes: change 1 matches, change 2 mismatches,
    change 3 matches. Mod B has 2 changes, both match.

    Pre-fix: 1 + 3 + B's 2 land, change 2 skipped. Mod A is partially
    applied.

    Post-fix: NONE of mod A's 3 changes land; all 3 recorded as
    skipped. Mod B's 2 changes land normally. Mod A is fully
    skipped, mod B untouched."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    # 32-byte vanilla buffer with known content
    vanilla = bytearray(b"\x00" * 32)
    vanilla[0] = 0xAA
    vanilla[4] = 0xAA
    vanilla[8] = 0xAA
    vanilla[12] = 0xBB
    vanilla[16] = 0xBB

    # Pre-pass simulation lives in process_json_patches_for_overlay,
    # but the contract we want to pin here is at the same call site
    # the wrapper exposes: given a `changes` list with mixed
    # _source_mod_id values, the apply path should drop every change
    # belonging to a mod that has any mismatch.
    #
    # We simulate the wrapper's filter by importing the helper that
    # will own the new pre-validation, then checking the post-filter
    # apply outcome.
    from cdumm.engine.json_patch_handler import (
        filter_changes_by_tainted_mods,
    )

    changes = [
        # mod A
        {"label": "A1", "offset": 0,  "original": "aa", "patched": "11",
         "_source_mod_id": 1},
        {"label": "A2", "offset": 4,  "original": "ff", "patched": "22",
         "_source_mod_id": 1},  # MISMATCH (vanilla is aa, expects ff)
        {"label": "A3", "offset": 8,  "original": "aa", "patched": "33",
         "_source_mod_id": 1},
        # mod B
        {"label": "B1", "offset": 12, "original": "bb", "patched": "44",
         "_source_mod_id": 2},
        {"label": "B2", "offset": 16, "original": "bb", "patched": "55",
         "_source_mod_id": 2},
    ]

    # Filter call must:
    # 1. Detect mod 1 as tainted (A2 mismatches)
    # 2. Return a clean list excluding all mod 1 changes
    # 3. Append skip records for all mod 1 changes into skipped_out
    skipped_out: list[dict] = []
    clean = filter_changes_by_tainted_mods(
        changes, bytes(vanilla), signature=None,
        skipped_out=skipped_out)

    # Mod A fully recorded as skipped (3 entries)
    a_skips = [s for s in skipped_out if s.get("_source_mod_id") == 1]
    assert len(a_skips) == 3, (
        f"All 3 of mod A's changes must be recorded as skipped when "
        f"any of them mismatches. Got {len(a_skips)}: {a_skips!r}"
    )
    # Mod B untouched in skipped_out
    b_skips = [s for s in skipped_out if s.get("_source_mod_id") == 2]
    assert b_skips == [], (
        f"Mod B has no mismatches and must not appear in skipped_out. "
        f"Got: {b_skips!r}"
    )
    # Clean list contains only mod B's changes
    clean_mods = {c.get("_source_mod_id") for c in clean}
    assert clean_mods == {2}, (
        f"Filter must drop every mod-A change. Clean list mod ids: "
        f"{clean_mods!r}"
    )
    assert len(clean) == 2

    # Now actually apply the clean changes and check buffer state
    modified = bytearray(vanilla)
    apply_skips: list[dict] = []
    _apply_byte_patches(
        modified, clean, signature=None, skipped_out=apply_skips)

    # Mod A's offsets stay vanilla
    assert modified[0] == 0xAA, "Mod A change 1 must NOT land"
    assert modified[4] == 0xAA, "Mod A change 2 (mismatch) must NOT land"
    assert modified[8] == 0xAA, "Mod A change 3 must NOT land"
    # Mod B's offsets get patched
    assert modified[12] == 0x44, "Mod B change 1 must land"
    assert modified[16] == 0x55, "Mod B change 2 must land"


def test_untagged_changes_keep_per_change_policy():
    """Format 3 whole-table merged changes carry no _source_mod_id
    (deferred attribution). They must keep the per-change skip
    policy, untouched mods can still apply alongside an untagged
    mismatch."""
    from cdumm.engine.json_patch_handler import (
        filter_changes_by_tainted_mods,
    )

    vanilla = bytearray(b"\x00" * 16)
    vanilla[0] = 0xAA
    vanilla[4] = 0xCC

    changes = [
        # Untagged change (mismatches)
        {"label": "untagged", "offset": 0, "original": "ff",
         "patched": "11"},
        # Tagged mod B (matches)
        {"label": "B1", "offset": 4, "original": "cc", "patched": "22",
         "_source_mod_id": 7},
    ]

    skipped_out: list[dict] = []
    clean = filter_changes_by_tainted_mods(
        changes, bytes(vanilla), signature=None,
        skipped_out=skipped_out)

    # Untagged stays in the clean list (apply-time will skip it via
    # the existing _record_skip path) , filter does not pre-skip it.
    assert any(c.get("label") == "untagged" for c in clean)
    # Tagged mod is clean
    assert any(c.get("label") == "B1" for c in clean)
    # No pre-skip recorded for untagged
    assert skipped_out == [], (
        f"Pre-validation must not pre-skip untagged changes. "
        f"Got: {skipped_out!r}"
    )


def test_clean_mod_with_no_mismatches_passes_through():
    """A mod whose every change matches vanilla must come back
    unfiltered with no skips recorded."""
    from cdumm.engine.json_patch_handler import (
        filter_changes_by_tainted_mods,
    )

    vanilla = bytearray(b"\xAA" * 16)
    changes = [
        {"label": "C1", "offset": 0, "original": "aa", "patched": "11",
         "_source_mod_id": 9},
        {"label": "C2", "offset": 4, "original": "aa", "patched": "22",
         "_source_mod_id": 9},
    ]

    skipped: list[dict] = []
    clean = filter_changes_by_tainted_mods(
        changes, bytes(vanilla), signature=None, skipped_out=skipped)

    assert len(clean) == 2
    assert skipped == []
