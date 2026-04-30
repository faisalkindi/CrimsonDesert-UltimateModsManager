"""allow_partial_apply must skip individual mismatched patches at
apply time, not nuke the whole pabgb file.

Bug from Faisal 2026-04-29 (No Cooldown for All Items mod):
"Apply Completed with Warnings" surfaces 80 of 201 patches as
mismatched on iteminfo.pabgb. The strict mount-time guard at
json_patch_handler.py:2259 fires `continue` on any pabgb with
even one mismatch, skipping the ENTIRE file. So the 121 verified
patches also don't land. The mod is "imported" but applies zero
changes in-game.

The v3.2.3 `allow_partial_apply` opt-in fixes this AT IMPORT
TIME (`_should_reject_partial_pabgb` honors the flag), but the
mount-time apply guard ignores it. Mod authors who set the flag
get past import but still hit the apply-time wall.

Fix: have the mount-time guard call `_should_reject_partial_pabgb`
too, threading patch_data through. With the flag set, partial
data table apply skips just the mismatched entries and lets the
rest land.
"""
from __future__ import annotations


def test_should_reject_partial_pabgb_honors_flag_at_apply_time():
    """The shared decision helper must produce the same answer
    regardless of which site (import or apply) calls it."""
    from cdumm.engine.json_patch_handler import _should_reject_partial_pabgb

    # Without flag: reject (strict default).
    assert _should_reject_partial_pabgb(
        "gamedata/iteminfo.pabgb",
        applied=121, mismatched=80,
        patch_data={"game_version": "old"},
    ) is True

    # With flag: allow partial apply.
    assert _should_reject_partial_pabgb(
        "gamedata/iteminfo.pabgb",
        applied=121, mismatched=80,
        patch_data={"game_version": "old", "allow_partial_apply": True},
    ) is False


def test_mount_time_guard_uses_shared_decision_helper():
    """Static-source check: the mount-time guard at json_patch_handler.py
    must consult `_should_reject_partial_pabgb` so the
    allow_partial_apply flag is respected at apply time too.

    Inspect the source file for the marker string at line ~2259
    and verify the guard calls the shared helper rather than just
    checking ``mismatched > 0 and is_data_table``."""
    from pathlib import Path
    src = Path(__file__).parent.parent / "src" / "cdumm" / "engine" / "json_patch_handler.py"
    text = src.read_text(encoding="utf-8")
    # Find the mount-time guard. It uniquely contains "mount-time:
    # aborting overlay".
    assert "mount-time: aborting overlay" in text, (
        "Could not locate the mount-time guard.")
    idx = text.index("mount-time: aborting overlay")
    # Slice 800 chars around it (must contain the if-condition).
    window = text[max(0, idx - 600):idx + 200]
    assert "_should_reject_partial_pabgb" in window, (
        f"The mount-time guard must consult _should_reject_partial_pabgb "
        f"so the allow_partial_apply opt-in is honored. Window:\n{window}")
