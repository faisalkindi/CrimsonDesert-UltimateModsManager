"""Regression: when CDUMM has to drop one of N mods touching the
same entry because they change the file size and can't merge, the
user-facing warning must NAME the mods that were dropped — not just
say 'N mods were dropped' (GioGr on Nexus had to dig through the
conflict viewer, and his conflict viewer didn't even show the
silently-lost mod).
"""
from __future__ import annotations

import re
from pathlib import Path


def _src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "engine" / "apply_engine.py"
            ).read_text(encoding="utf-8")


def _find_branch(src: str) -> str:
    """Return the body of the size-changing-drop branch — from the
    'if size_changed:' line through the result.append(...) that
    closes the branch (right before the try: merge_compiled_mod_files
    fallback). Using merge_compiled_mod_files as the end-marker
    avoids prematurely stopping on the inner `if i == winner:
    continue` loop iterator."""
    anchor = src.find("if size_changed:")
    assert anchor != -1, "size_changed branch missing"
    end = src.find("merge_compiled_mod_files", anchor)
    assert end != -1, "expected merge_compiled_mod_files after branch"
    return src[anchor:end]


def test_message_lists_dropped_mod_names():
    body = _find_branch(_src())
    # The branch must build a list of dropped mod names by iterating
    # indices and skipping the winner.
    assert "dropped_names" in body, (
        "the branch must collect dropped mod names into a list "
        "(was previously only counting them)")
    # Must skip the winner so we don't list it among the dropped.
    assert re.search(r"if i == winner:\s*\n\s*continue", body), (
        "must skip the winner index when collecting dropped names")
    # Must read mod_name from the per-mod metadata.
    assert re.search(r'\.get\("mod_name"', body), (
        "must read mod_name from each entry's metadata so the "
        "message names actual mods, not indices")


def test_message_caps_long_list_with_and_more():
    body = _find_branch(_src())
    # Long dropped lists shouldn't blow up the banner. Cap at 5,
    # show "and N more" suffix.
    assert "[:5]" in body, (
        "dropped list must be capped at 5 names to keep banners "
        "readable on huge conflict sets")
    assert "more" in body, (
        "must show an 'and N more' suffix when the list is capped")


def test_message_says_active_and_dropped():
    body = _find_branch(_src())
    # The user-facing message must contain both 'Active:' and
    # 'Dropped:' so it's clear which mod won and which lost.
    assert "Active:" in body, (
        "message must label the winning mod with 'Active:'")
    assert "Dropped:" in body, (
        "message must label the dropped mods with 'Dropped:'")


def test_message_tells_user_how_to_change_winner():
    body = _find_branch(_src())
    # Old message said "use priority to pick a different winner" —
    # the new message must also tell the user the concrete UI
    # action to take.
    assert "load order" in body or "drag" in body.lower(), (
        "message must tell the user the actual UI action — drag "
        "the desired winner to the top of the load order")
