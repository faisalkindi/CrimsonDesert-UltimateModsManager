"""HIGH #10: per-launch import context snapshot to prevent clobber races.

When mods_page sets `window._update_priority = 5` and calls
_launch_import_worker, a subsequent swap that sets
`window._update_priority = 7` BEFORE the first proc finishes would
clobber the values _on_finished reads. The fix snapshots these
fields into a dict that rides on the QProcess instance so each
proc's handler reads its own context.

This test exercises the snapshot helper directly — Qt process
orchestration is out of scope.
"""
from __future__ import annotations

from types import SimpleNamespace

from cdumm.gui.import_context import (
    snapshot_and_clear_import_context,
    IMPORT_CONTEXT_KEYS,
)


def test_snapshot_captures_all_relevant_fields():
    win = SimpleNamespace(
        _update_priority=3,
        _update_enabled=1,
        _configurable_source="E:/mods/source.rar",
        _configurable_labels={"L1": True},
        _variant_leaf_rel="variantA",
        _original_drop_path="E:/mods/Nexus-123-4-5.rar",
    )
    ctx = snapshot_and_clear_import_context(win)
    assert ctx["update_priority"] == 3
    assert ctx["update_enabled"] == 1
    assert ctx["configurable_source"] == "E:/mods/source.rar"
    assert ctx["configurable_labels"] == {"L1": True}
    assert ctx["variant_leaf_rel"] == "variantA"
    assert ctx["original_drop_path"] == "E:/mods/Nexus-123-4-5.rar"


def test_snapshot_clears_fields_on_window_after_capture():
    win = SimpleNamespace(
        _update_priority=3,
        _update_enabled=1,
        _configurable_source="X",
        _configurable_labels={"L": True},
        _variant_leaf_rel="V",
        _original_drop_path=None,
    )
    snapshot_and_clear_import_context(win)
    assert win._update_priority is None
    assert win._update_enabled is None
    assert win._configurable_source is None
    assert win._configurable_labels is None
    assert win._variant_leaf_rel is None


def test_second_snapshot_is_independent():
    win = SimpleNamespace(
        _update_priority=3, _update_enabled=1,
        _configurable_source=None, _configurable_labels=None,
        _variant_leaf_rel=None, _original_drop_path=None,
    )
    first = snapshot_and_clear_import_context(win)
    # User triggers second swap: set priority again on the SAME window.
    win._update_priority = 99
    second = snapshot_and_clear_import_context(win)

    assert first["update_priority"] == 3, "first snapshot kept its own state"
    assert second["update_priority"] == 99
    # Mutating second must NOT mutate first (independent dicts).
    second["update_priority"] = 777
    assert first["update_priority"] == 3


def test_missing_attrs_snapshot_as_none():
    win = SimpleNamespace()   # no attrs at all
    ctx = snapshot_and_clear_import_context(win)
    for key in IMPORT_CONTEXT_KEYS:
        assert ctx[key] is None, f"missing attr '{key}' should snapshot as None"
