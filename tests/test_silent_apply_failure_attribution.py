"""When all enabled JSON mods produce 0 overlay entries at apply,
the warning shown to the user must name the specific mods AND the
target files they tried to patch, not just say "X mods produced 0
changes" without attribution.

Bug from Robhood19 (Nexus, 2026-04-29): two JSON mods enabled,
"Apply Completed with warnings - 2 JSON mods were enabled but
produced no game changes". The user couldn't tell which mods
failed or what to fix. Fix Everything didn't help (probably
because Fix Everything rebuilds the snapshot but if the mod's
JSON itself targets bytes that no longer match the current game,
no rebuild helps; the mod is outdated for the game version).

The user-facing warning needs to:
  1. Name each contributing mod
  2. Show the target files for each
  3. Explain the likely cause (game-version drift) and what to do
"""
from __future__ import annotations


def test_warning_names_each_mod_and_target_files():
    from cdumm.engine.apply_engine import (
        _build_silent_apply_failure_message,
    )
    mod_summary = [
        {"mod_id": 25, "mod_name": "Dark Mode Map",
         "priority": 11, "targets": ["0012/4.paz"],
         "change_count": 5},
        {"mod_id": 37, "mod_name": "Resource Costs Json",
         "priority": 10, "targets": ["0008/0.paz", "0010/3.paz"],
         "change_count": 12},
    ]
    msg = _build_silent_apply_failure_message(mod_summary)
    assert "Dark Mode Map" in msg
    assert "Resource Costs Json" in msg
    assert "0012/4.paz" in msg
    assert "0008/0.paz" in msg
    assert "0010/3.paz" in msg


def test_warning_with_zero_change_mods_skipped_from_attribution():
    """A mod with change_count == 0 didn't actually contribute any
    patches to the synth (e.g. all changes disabled via per-patch
    toggle); naming it in the failure attribution is misleading."""
    from cdumm.engine.apply_engine import (
        _build_silent_apply_failure_message,
    )
    mod_summary = [
        {"mod_id": 1, "mod_name": "Live", "priority": 1,
         "targets": ["a.paz"], "change_count": 5},
        {"mod_id": 2, "mod_name": "Empty", "priority": 2,
         "targets": [], "change_count": 0},
    ]
    msg = _build_silent_apply_failure_message(mod_summary)
    assert "Live" in msg
    assert "Empty" not in msg, (
        "Mods with 0 contributions shouldn't be in the failure list")


def test_warning_explains_cause_and_fix_path():
    """Beyond just naming mods, the message must point users to a
    concrete next step: typically a game-version-drift issue, with
    Fix Everything as the suggested action."""
    from cdumm.engine.apply_engine import (
        _build_silent_apply_failure_message,
    )
    mod_summary = [
        {"mod_id": 1, "mod_name": "X", "priority": 1,
         "targets": ["a.paz"], "change_count": 1},
    ]
    msg = _build_silent_apply_failure_message(mod_summary)
    msg_lower = msg.lower()
    assert "fix everything" in msg_lower
    assert ("game version" in msg_lower or "outdated" in msg_lower
            or "doesn't match" in msg_lower or "drift" in msg_lower)


def test_warning_handles_empty_mod_summary():
    """Edge case: somehow no mods contributed; message should still
    be a valid string and not crash."""
    from cdumm.engine.apply_engine import (
        _build_silent_apply_failure_message,
    )
    msg = _build_silent_apply_failure_message([])
    assert isinstance(msg, str)
    assert len(msg) > 0
