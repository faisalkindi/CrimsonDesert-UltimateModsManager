"""When a Format 3 mod's whole-table writer (iteminfo, skill) emits a
single change covering the entire .pabgb body, that change must not
clobber concurrent v2 byte-patches on the same file.

Bug: iteminfo_writer/skill_writer emit `original = full vanilla body`,
`offset = 0`. When v2 byte-patches run first and mutate the buffer,
the whole-table change's verification fails (data != original). Then
the vanilla-remnant fallback at json_patch_handler.py:950 finds that
vanilla matches change.original (because vanilla IS itself), and
OVERWRITES the entire 5MB body with `patched`. V2 byte-patches are
silently discarded. User sees both apply succeed but only the
whole-table writes survive.

This is the exact pattern ZirconX1, Lichtnocht, and others reported:
"applies cleanly with no errors, but doesn't take effect in-game."

Fix: whole-table changes must apply BEFORE per-byte changes so v2
overlays on top, not the other way around.
"""
from __future__ import annotations
import pytest


def test_whole_table_change_does_not_clobber_v2_byte_patches():
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    # Synthetic vanilla buffer: 1024 bytes of 0x00.
    vanilla = bytes(1024)
    data = bytearray(vanilla)

    # Whole-table writer's change: replace entire buffer with 0x55 bytes
    # except byte 100 which is 0xAA (a "format 3 intent" simulated edit).
    whole_table_patched = bytearray(b"\x55" * 1024)
    whole_table_patched[100] = 0xAA

    whole_table_change = {
        "offset": 0,
        "original": vanilla.hex(),
        "patched": bytes(whole_table_patched).hex(),
        "label": "whole-table format 3",
    }

    # V2 byte-patch: 8 bytes at offset 200, change from vanilla bytes
    # (zeros) to 0xBB.
    v2_patched = b"\xbb" * 8
    v2_change = {
        "offset": 200,
        "original": vanilla[200:208].hex(),
        "patched": v2_patched.hex(),
        "label": "v2 byte patch",
    }

    # Apply BOTH changes through the real apply path. Order in the list
    # mirrors how aggregate_json_mods_into_synthetic_patches builds it
    # today: v2 first (from real v2 mods), Format 3 whole-table appended
    # by expand_format3_into_aggregated.
    applied, mismatched, _relocated = _apply_byte_patches(
        data, [v2_change, whole_table_change],
        vanilla_data=vanilla,
    )

    # Both must succeed.
    assert mismatched == 0, (
        f"Expected both changes to apply cleanly, got {mismatched} "
        f"mismatched."
    )
    assert applied == 2

    # The whole-table edit at byte 100 must be present.
    assert data[100] == 0xAA, (
        f"Whole-table edit at byte 100 lost: got {data[100]:#x}, "
        f"expected 0xAA."
    )

    # The v2 byte-patch at offset 200..208 must be present.
    # If whole-table ran AFTER v2, it overwrote 200..208 with 0x55 bytes,
    # discarding the v2 patch. That's the bug.
    assert bytes(data[200:208]) == v2_patched, (
        f"V2 byte-patch at offset 200..208 was clobbered by the "
        f"whole-table change. Got {bytes(data[200:208]).hex()!r}, "
        f"expected {v2_patched.hex()!r}. Whole-table changes must apply "
        f"BEFORE per-byte patches so v2 overlays on top."
    )

    # Bytes outside both edits should be from the whole-table change.
    assert data[0] == 0x55
    assert data[500] == 0x55
    assert data[1000] == 0x55
