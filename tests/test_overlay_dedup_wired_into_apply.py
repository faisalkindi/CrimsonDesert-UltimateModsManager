"""Wiring guards for the Phase 1b de-dup pass + the loud-error
fallback in json_patch_handler. Follows the pattern established by
``test_no_orphaned_nexus_helpers.py`` — prove the production code
path invokes the new helpers, not just that the helpers exist.
"""
from __future__ import annotations

import re
from pathlib import Path


def _apply_engine_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "engine" / "apply_engine.py").read_text(
                encoding="utf-8")


def _json_handler_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "engine" / "json_patch_handler.py"
            ).read_text(encoding="utf-8")


def test_apply_engine_imports_merge_helper():
    assert "from cdumm.engine.overlay_dedup import" in _apply_engine_src()
    assert "merge_duplicate_overlay_entries" in _apply_engine_src()


def test_dedup_runs_before_phase_1b_overlay_build():
    """The de-dup call must happen BEFORE the Phase 1b log line so
    the overlay builder sees the collapsed list, not the raw
    duplicates."""
    src = _apply_engine_src()
    dedup_pos = src.find("merge_duplicate_overlay_entries(")
    phase_1b_pos = src.find('_phase(f"Phase 1b: Build overlay')
    assert dedup_pos != -1, "de-dup call not wired"
    assert phase_1b_pos != -1, "Phase 1b log line not found"
    assert dedup_pos < phase_1b_pos, (
        "de-dup must run before Phase 1b overlay build — the "
        "builder consumes the collapsed list")


def test_dedup_warnings_surface_to_user():
    """Warnings from the de-dup pass must feed the existing
    _soft_warnings list + warning.emit signal so the GUI InfoBar
    catches them."""
    src = _apply_engine_src()
    i = src.find("merge_duplicate_overlay_entries(")
    assert i != -1
    # Scope: next 2500 chars after the call site.
    scope = src[i:i + 2500]
    assert "_soft_warnings.append" in scope, (
        "de-dup warnings must land in _soft_warnings for GUI surfacing")
    assert "self.warning.emit" in scope, (
        "de-dup warnings must also fire the warning signal")


def test_loud_error_on_non_data_table_mismatch():
    """json_patch_handler must append to errors_out when mismatched
    patches happen on a NON-data-table file (prefabs, XML, etc.) so
    the GUI surfaces the skip instead of silently applying the
    partial overlay."""
    src = _json_handler_src()
    # Anchor on the existing data-table abort block.
    anchor = src.find("aborting overlay for")
    assert anchor != -1, "data-table abort block not found"
    # Scan forward ~1500 chars for the new non-data-table warning.
    scope = src[anchor:anchor + 3000]
    assert re.search(
        r"if\s+mismatched\s*>\s*0\s+and\s+errors_out\s+is\s+not\s+None",
        scope,
    ), ("expected a new 'if mismatched > 0 and errors_out is not None' "
        "branch AFTER the data-table abort so non-data-table "
        "mismatches also surface to the user")
    # And the message must mention disabling / load order, so users
    # know what to do next.
    assert "disabling it or changing load order" in scope
