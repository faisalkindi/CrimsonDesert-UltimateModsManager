"""JMM-parity UX: when JSON patches fail to apply (bytes don't
match the current game version), the per-patch skip details must
flow up to the user-visible warning instead of being silently logged.

Empirical proof from PhorgeForge's Stamina mod #107: JMM prints
"121 applied, 19 skipped" with the specific skill labels;
CDUMM logged the same skip count at DEBUG level and showed
"Apply complete" with no warning, leading users to think the mod
was working when 13% of its patches silently dropped.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.json_patch_handler import _apply_byte_patches


# ── _apply_byte_patches surfaces per-patch skips ────────────────────


def test_skipped_out_records_byte_mismatch():
    """When the bytes at the offset don't match the mod's
    expected ``original``, the skip must be appended with the
    label, expected hex, actual hex, offset, and reason."""
    # vanilla bytes "AB CD" at offset 0; mod expects "11 22"
    data = bytearray(b"\xAB\xCD\xEF\xFF")
    changes = [{
        "offset": 0,
        "original": "1122",
        "patched": "0000",
        "label": "TestEntry.field",
    }]
    skips: list[dict] = []
    applied, mismatched, _relocated = _apply_byte_patches(
        data, changes, skipped_out=skips)
    assert applied == 0
    assert mismatched == 1
    assert len(skips) == 1
    s = skips[0]
    assert s["label"] == "TestEntry.field"
    assert s["expected"] == "1122"
    assert s["actual"] == "abcd"
    assert s["offset"] == 0
    assert "byte mismatch" in s["reason"]


def test_skipped_out_silent_when_omitted():
    """Backward compat: callers that don't pass skipped_out get
    the same behaviour as before — count-only, no list."""
    data = bytearray(b"\xAB\xCD")
    changes = [{
        "offset": 0, "original": "1122", "patched": "0000",
        "label": "X",
    }]
    # No skipped_out=... — should not raise
    applied, mismatched, _r = _apply_byte_patches(data, changes)
    assert applied == 0
    assert mismatched == 1


def test_multiple_skips_in_order():
    data = bytearray(b"\x00\x00\x00\x00\x00\x00")
    changes = [
        {"offset": 0, "original": "AABB", "patched": "1122", "label": "a"},
        {"offset": 2, "original": "CCDD", "patched": "3344", "label": "b"},
        {"offset": 4, "original": "EEFF", "patched": "5566", "label": "c"},
    ]
    skips: list[dict] = []
    _applied, mismatched, _r = _apply_byte_patches(
        data, changes, skipped_out=skips)
    assert mismatched == 3
    assert [s["label"] for s in skips] == ["a", "b", "c"]


def test_skip_uses_entry_when_label_missing():
    data = bytearray(b"\x00\x00")
    changes = [{
        "offset": 0, "original": "AABB", "patched": "1122",
        "entry": "Skill_Foo",
    }]
    skips: list[dict] = []
    _applied, _m, _r = _apply_byte_patches(
        data, changes, skipped_out=skips)
    # Falls back to entry name when no label provided
    assert skips[0]["label"] == "Skill_Foo"


def test_unresolvable_offset_records_skip_with_reason():
    """A change with neither a usable offset NOR a usable
    record_key/entry resolution should record a skip with the
    'unresolvable offset' reason."""
    data = bytearray(b"\x00\x00")
    # No 'offset', no 'record_key', no 'entry' — unresolvable
    changes = [{"original": "AABB", "patched": "1122", "label": "z"}]
    skips: list[dict] = []
    _applied, mismatched, _r = _apply_byte_patches(
        data, changes, skipped_out=skips)
    assert mismatched == 1
    assert len(skips) == 1
    assert skips[0]["reason"] == "unresolvable offset"
    assert skips[0]["label"] == "z"


# ── apply_engine wiring ─────────────────────────────────────────────


def test_apply_engine_emits_warning_on_skipped_patches():
    """ApplyWorker must surface skipped JSON patches via the warning
    signal so on_apply_done can render them in the post-apply
    InfoBar. Source-level check that the wiring exists."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "engine" / "apply_engine.py"
           ).read_text(encoding="utf-8")
    # The aggregator path must allocate a skipped_out list and pass
    # it to process_json_patches_for_overlay.
    assert "patch_skips: list[dict] = []" in src, (
        "apply_engine must allocate a patch_skips list before the "
        "json overlay call so per-patch skip details bubble up")
    assert "skipped_out=patch_skips" in src, (
        "the json overlay call must receive patch_skips as the "
        "skipped_out argument")
    # And it must emit a warning when the list is non-empty.
    assert "if patch_skips:" in src, (
        "apply_engine must check patch_skips and emit a user-facing "
        "warning when any patches were skipped")
    assert "self.warning.emit" in src, (
        "the skip warning must go through the existing warning "
        "signal so on_apply_done renders it in the InfoBar")


def test_apply_engine_warning_includes_skip_details():
    """The warning text must include the skip count + at least
    some per-patch details (label, expected vs actual)."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "engine" / "apply_engine.py"
           ).read_text(encoding="utf-8")
    # Find the skip-surfacing block
    anchor = src.find("if patch_skips:")
    assert anchor != -1
    # Look in the next ~30 lines
    block = src[anchor:anchor + 1500]
    assert "label" in block, (
        "warning must reference the patch label so users know "
        "which entries skipped")
    assert "expected" in block.lower(), (
        "warning must show the expected bytes so users can "
        "diagnose game-version mismatch themselves")
    assert "actual" in block.lower() or "got" in block.lower(), (
        "warning must show the actual bytes for the same reason")
    # And the JMM-parity 'X applied, Y skipped' shape
    assert "skipped" in block.lower()
