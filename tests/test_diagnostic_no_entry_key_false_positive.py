"""Diagnostic: classic v2 byte-patch changes (no 'entry' key) must
not be flagged as "ISSUE: missing 'entry' key".

Bug from lycusz / jscrump1278 on GitHub #56, 2026-04-29: their bug
report on "No Cooldown and Durability for all items" showed:

    Changes: 299
    ISSUE: Change 1: missing 'entry' key
    ISSUE: Change 2: missing 'entry' key
    ISSUE: Change 3: missing 'entry' key
    ... and 296 more invalid changes
    Status: FOUND in 0008/ (exact match)
    Byte Verification:
      Summary: 299 verified, 0 mismatched, 0 skipped

Every change verified against vanilla cleanly, but the diagnostic
shouts "ISSUE" 299 times because none of them have an ``entry``
key. ``entry`` is only required by the v2 entry-anchored format
(JMM V8+ / SWISS Knife style). Classic byte-offset patches with
``offset`` + ``original`` + ``patched`` are fully valid v2 — they
just don't use entry-anchoring.

Fix: ``mod_diagnostics.py:334`` should only flag missing ``entry``
when the change ALSO lacks an ``offset`` (truly indeterminate
target). A change with an ``offset`` is well-formed regardless
of whether ``entry`` is present.
"""
from __future__ import annotations


def test_v2_change_with_offset_no_entry_is_not_flagged():
    """A classic v2 byte-patch change has 'offset' but no 'entry' —
    that's a fully valid format and must not be flagged as 'ISSUE'."""
    from cdumm.engine.mod_diagnostics import _validate_change_structure

    change = {
        "offset": 1241504,
        "original": "f9240100",
        "patched": "ff000000",
        "label": "no-cooldown for some item",
    }
    issues = _validate_change_structure(change, change_index=0)
    assert issues == [], (
        f"Classic v2 byte-patch (offset+original+patched, no entry) "
        f"must not produce diagnostic issues. Got: {issues}")


def test_change_with_entry_no_offset_is_valid():
    """An entry-anchored v2 change has 'entry' + 'rel_offset' but
    no absolute 'offset'. Also valid."""
    from cdumm.engine.mod_diagnostics import _validate_change_structure

    change = {
        "entry": "ThiefGloves",
        "rel_offset": 24,
        "original": "01000000",
        "patched": "00000000",
    }
    issues = _validate_change_structure(change, change_index=0)
    assert issues == []


def test_change_with_neither_offset_nor_entry_is_flagged():
    """A change with neither 'offset' NOR 'entry' has no way to
    locate the target byte. THAT's the real "missing target"
    error — and the diagnostic should still surface it."""
    from cdumm.engine.mod_diagnostics import _validate_change_structure

    change = {
        "original": "01000000",
        "patched": "00000000",
        # no offset, no entry — diagnostic must flag this
    }
    issues = _validate_change_structure(change, change_index=0)
    assert len(issues) >= 1, (
        f"A change without offset OR entry has no resolvable "
        f"target — must be flagged. Got: {issues}")
    # The message should mention what's missing.
    assert any("offset" in i.lower() or "entry" in i.lower()
               or "target" in i.lower() for i in issues), (
        f"Issue message should name what's missing. Got: {issues}")


def test_non_dict_change_still_flagged():
    """Regression guard: a non-dict change is still flagged."""
    from cdumm.engine.mod_diagnostics import _validate_change_structure

    issues = _validate_change_structure("not a dict", change_index=0)
    assert len(issues) >= 1
    assert any("dict" in i.lower() for i in issues)
