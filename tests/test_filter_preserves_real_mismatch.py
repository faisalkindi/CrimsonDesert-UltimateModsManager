"""H1: filter_changes_by_tainted_mods must preserve the real
offset/actual/expected detail from the dry-run for the change that
actually triggered the taint, not replace every change with a
synthetic 'another patch in this mod did not match' stub.

Pre-fix: dry-run real skip entries land in a local ``test_skipped``
list, which is discarded; ALL of the tainted mod's changes get
synthesized entries with offset=-1 and actual="". Logs and the
SKIPPED-badge tooltip lose every clue to which patch tripped.

Post-fix: the change that genuinely mismatched keeps its real
offset/actual/expected/reason. Drag-along changes (the matching
ones from the same tainted mod) still get synthetic entries so the
'whole mod skipped' count stays accurate.
"""
from __future__ import annotations

from cdumm.engine.json_patch_handler import (
    filter_changes_by_tainted_mods,
)


def _vanilla_with_marker_bytes() -> bytes:
    # offsets 0/4/8 = 0xAA; offsets 12/16 = 0xBB; rest 0x00.
    buf = bytearray(b"\x00" * 32)
    buf[0] = 0xAA
    buf[4] = 0xAA
    buf[8] = 0xAA
    buf[12] = 0xBB
    buf[16] = 0xBB
    return bytes(buf)


def test_real_trigger_mismatch_kept_with_offset_and_actual():
    """Mod 1 has 3 changes. A2 mismatches (expects 0xff at offset 4
    where vanilla has 0xaa). The real skip entry for A2 must land in
    skipped_out with offset=4 and actual='aa', NOT a generic
    'another patch in this mod did not match' stub."""
    vanilla = _vanilla_with_marker_bytes()
    changes = [
        {"label": "A1", "offset": 0, "original": "aa", "patched": "11",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
        {"label": "A2", "offset": 4, "original": "ff", "patched": "22",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
        {"label": "A3", "offset": 8, "original": "aa", "patched": "33",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
    ]
    skipped: list[dict] = []
    clean = filter_changes_by_tainted_mods(
        changes, vanilla, signature=None, skipped_out=skipped)

    assert clean == [], (
        "Mod 1 must be fully tainted, no changes survive into clean")
    a2_real = [s for s in skipped if s.get("label") == "A2"]
    assert len(a2_real) == 1, f"expected one A2 skip, got {a2_real!r}"
    a2 = a2_real[0]
    assert a2.get("offset") == 4, (
        f"A2 trigger entry must keep its real offset (4), got "
        f"{a2.get('offset')!r}. Pre-fix bug: synthetic stub overwrites "
        f"with -1.")
    assert a2.get("actual") == "aa", (
        f"A2 trigger entry must keep the real actual bytes from the "
        f"dry-run (vanilla had 0xaa), got {a2.get('actual')!r}. "
        f"Pre-fix bug: synthetic stub overwrites with empty string.")
    assert "did not match" not in (a2.get("reason") or ""), (
        f"A2 is the trigger , its reason should be the real "
        f"byte-mismatch reason from _apply_byte_patches, NOT the "
        f"drag-along synthetic stub. Got reason={a2.get('reason')!r}")
    assert a2.get("_source_mod_id") == 1
    assert a2.get("_target_file") == "fake.pabgb"


def test_drag_along_changes_still_recorded_as_skipped():
    """The matching changes (A1, A3) didn't really mismatch, but the
    all-or-nothing rule means they get dropped. They still need an
    entry in skipped_out so the badge count stays correct."""
    vanilla = _vanilla_with_marker_bytes()
    changes = [
        {"label": "A1", "offset": 0, "original": "aa", "patched": "11",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
        {"label": "A2", "offset": 4, "original": "ff", "patched": "22",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
        {"label": "A3", "offset": 8, "original": "aa", "patched": "33",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
    ]
    skipped: list[dict] = []
    filter_changes_by_tainted_mods(
        changes, vanilla, signature=None, skipped_out=skipped)
    labels = sorted(s.get("label") for s in skipped)
    assert labels == ["A1", "A2", "A3"], (
        f"All three of mod 1's changes must appear in skipped_out so "
        f"the badge count and tooltip reflect the full taint scope. "
        f"Got labels={labels!r}")


def test_drag_along_entries_marked_with_clear_reason():
    """A1 and A3 didn't trigger the taint; their reason should make
    that explicit so a future debugger can tell the trigger from the
    drag-alongs."""
    vanilla = _vanilla_with_marker_bytes()
    changes = [
        {"label": "A1", "offset": 0, "original": "aa", "patched": "11",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
        {"label": "A2", "offset": 4, "original": "ff", "patched": "22",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
        {"label": "A3", "offset": 8, "original": "aa", "patched": "33",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
    ]
    skipped: list[dict] = []
    filter_changes_by_tainted_mods(
        changes, vanilla, signature=None, skipped_out=skipped)
    a1 = next(s for s in skipped if s.get("label") == "A1")
    a3 = next(s for s in skipped if s.get("label") == "A3")
    for entry in (a1, a3):
        assert "did not match" in (entry.get("reason") or ""), (
            f"Drag-along entry should keep the synthetic 'another "
            f"patch in this mod did not match' reason. Got "
            f"{entry.get('reason')!r}")


def test_no_dry_run_token_leaks_into_skipped_out():
    """The implementation may use a temporary `_dry_run_token` field
    to match real entries to their source change. Whatever token is
    used internally MUST be stripped before the entry lands in
    skipped_out so downstream code (DB persist, badge tooltip) sees
    only the public schema."""
    vanilla = _vanilla_with_marker_bytes()
    changes = [
        {"label": "A2", "offset": 4, "original": "ff", "patched": "22",
         "_source_mod_id": 1, "_target_file": "fake.pabgb"},
    ]
    skipped: list[dict] = []
    filter_changes_by_tainted_mods(
        changes, vanilla, signature=None, skipped_out=skipped)
    for s in skipped:
        assert "_dry_run_token" not in s, (
            f"Internal dry-run token leaked into skipped_out: {s!r}")
