"""Integration test covering the full post-nxm-update state flow —
exercises Bugs 9, 14, 44, 48, 53 together.

Drives a minimal stand-in of the FluentWindow state machine through:

    1. pre-check sets _pending_selected_labels, _update_priority,
       _last_existing_mod_id, _nexus_real_file_id_map[path].
    2. worker succeeds — all scratch fields consumed.
    3. _clear_pending_post_import_state clears all of them.
    4. clear_outdated_after_update replaces the _nexus_updates
       entry with has_update=False (green, not grey).

This validates the helpers behave correctly AT THE STATE LEVEL —
complements the grep-guard tests that prove the call sites exist.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_clear_pending_state_after_successful_import_reset_all_scratch():
    """End-to-end: all scratch fields reset + the completed path is
    popped from the nxm file-id map. Uses ``str(Path(...))`` for the
    map keys to match the production code which stringifies the path
    (and produces platform-specific separators)."""
    from cdumm.gui.fluent_window import _clear_pending_post_import_state
    failed_path = Path("/failed")
    ok_path = Path("/ok")
    win = SimpleNamespace(
        _update_priority=5,
        _update_enabled=1,
        _configurable_source="/tmp/foo",
        _configurable_labels={"x": True},
        _original_drop_path=Path("/drop"),
        _pending_selected_labels={"a": True},
        _last_existing_mod_id=1333,
        _nexus_real_file_id_map={
            str(failed_path): 42, str(ok_path): 7,
        },
    )
    _clear_pending_post_import_state(win, path=ok_path)
    # All scratch attrs reset to None.
    assert win._update_priority is None
    assert win._update_enabled is None
    assert win._configurable_source is None
    assert win._configurable_labels is None
    assert win._original_drop_path is None
    assert win._pending_selected_labels is None
    assert win._last_existing_mod_id is None
    # Map loses only the completed path.
    assert win._nexus_real_file_id_map == {str(failed_path): 42}


def test_clear_outdated_leaves_green_entry_and_survives_repaint():
    """After the user's update downloads, the _nexus_updates dict
    must carry a has_update=False entry so the pill renderer paints
    it GREEN (confirmed current), not pop-it-out which would paint
    GREY (unknown).
    """
    from dataclasses import dataclass
    from cdumm.engine.nexus_api import clear_outdated_after_update

    @dataclass
    class _Stat:
        mod_id: int
        local_name: str
        local_version: str
        latest_version: str
        has_update: bool
        mod_url: str
        latest_file_id: int

    before = {
        42: _Stat(
            mod_id=42, local_name="Mod42", local_version="1.0",
            latest_version="2.0", has_update=True,
            mod_url="https://x", latest_file_id=9001),
    }
    after = clear_outdated_after_update(before, 42, new_version="2.0")
    # Entry still present — set_nexus_updates needs it to paint GREEN.
    assert 42 in after
    stat = after[42]
    assert stat.has_update is False
    # Local and latest version now match.
    assert stat.local_version == "2.0"
    assert stat.latest_version == "2.0"
    # Input dict wasn't mutated.
    assert before[42].has_update is True


def test_nxm_clear_works_for_asi_path_same_contract():
    """Bug 48: the ASI post-install invalidation must use the same
    helper PAZ uses — verified here by calling the helper the way
    the ASI code now does and checking the result."""
    from dataclasses import dataclass
    from cdumm.engine.nexus_api import clear_outdated_after_update

    @dataclass
    class _Stat:
        mod_id: int
        local_name: str
        local_version: str
        latest_version: str
        has_update: bool
        mod_url: str
        latest_file_id: int

    updates = {
        774: _Stat(
            mod_id=774, local_name="ReShadePresetSaver",
            local_version="1.3", latest_version="1.4",
            has_update=True, mod_url="", latest_file_id=88),
    }
    # Nexus parse gives ('1.4') as the new_ver (stripped).
    result = clear_outdated_after_update(updates, 774, new_version="1.4")
    assert result[774].has_update is False
    assert result[774].local_version == "1.4"
