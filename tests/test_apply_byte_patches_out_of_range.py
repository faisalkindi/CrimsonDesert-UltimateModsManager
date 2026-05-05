"""Found via /systematic-debugging sweep: ``_apply_byte_patches``
silently skips changes whose offset+length exceeds the buffer,
without recording a skip entry in ``skipped_out``. That means:

1. The all-or-nothing per-mod filter doesn't see the failure, so
   the mod isn't tainted and other (valid) changes from the same
   mod still apply.
2. The user gets no SKIPPED badge, no toast, no log entry showing
   the change failed to apply.

Fix: record an out-of-range change in skipped_out so downstream
machinery can surface it just like a byte-mismatch.
"""
from __future__ import annotations


def test_out_of_range_offset_records_skip_entry():
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00" * 16)
    changes = [
        {"label": "off-end", "offset": 100, "original": "aa",
         "patched": "bb"},
    ]
    skipped: list[dict] = []
    applied, mismatched, _relocated = _apply_byte_patches(
        data, changes, signature=None, skipped_out=skipped)
    assert applied == 0
    assert len(skipped) == 1, (
        f"out-of-range offset must add a skip entry, got "
        f"{len(skipped)} entries: {skipped!r}")
    entry = skipped[0]
    assert entry["label"] == "off-end"
    assert "exceed" in (entry.get("reason") or "").lower() \
        or "out of range" in (entry.get("reason") or "").lower()


def test_missing_patched_field_records_skip_entry():
    """Sister bug to out-of-range: a change with no 'patched' field
    was logging-only without recording a skip. Same family of silent
    failure."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(b"\x00" * 16)
    changes = [
        {"label": "no-patched", "offset": 0, "original": "00"},
        # 'patched' missing
    ]
    skipped: list[dict] = []
    _apply_byte_patches(
        data, changes, signature=None, skipped_out=skipped)
    assert len(skipped) == 1
    assert "patched" in (skipped[0].get("reason") or "").lower()


def test_out_of_range_taints_mod_via_filter():
    """End-to-end: a mod with a single out-of-range change must be
    fully tainted by filter_changes_by_tainted_mods, not silently
    pass through."""
    from cdumm.engine.json_patch_handler import (
        filter_changes_by_tainted_mods,
    )

    changes = [
        {"label": "off-end", "offset": 100, "original": "aa",
         "patched": "bb", "_source_mod_id": 1,
         "_target_file": "fake.pabgb"},
    ]
    skipped: list[dict] = []
    clean = filter_changes_by_tainted_mods(
        changes, b"\x00" * 16, signature=None, skipped_out=skipped)
    assert clean == [], (
        "out-of-range offset must taint the mod (clean list empty)")
    assert any(s.get("_source_mod_id") == 1 for s in skipped), (
        "out-of-range mod must record a skip entry attributing to "
        "mod_id=1")
