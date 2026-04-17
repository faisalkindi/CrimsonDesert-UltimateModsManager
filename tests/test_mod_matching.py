"""Pure duplicate-mod matching — equality on prettified names + token-overlap ratio.

Replaces the substring-based bug in `_find_existing_mod` that caused
DeathZxZ's issue: 'Infinite Stamina' swallowed 'Infinite Stamina (All
Skills Horse Spirit)' as a duplicate.
"""
from __future__ import annotations

from cdumm.engine.mod_matching import is_same_mod, token_overlap_ratio


# ── Strict equality ──────────────────────────────────────────────────


def test_exact_match_after_prettify_returns_true():
    assert is_same_mod("Better Trade Menu", "Better Trade Menu")


def test_same_mod_with_version_suffix_still_matches():
    # prettify strips '-v2' and trailing version numbers
    assert is_same_mod("Better Trade Menu v2", "Better Trade Menu")


def test_same_mod_with_different_version_variants():
    # '-1.03.00' and 'v2.1' both stripped → equality
    assert is_same_mod("Trust Me v2", "Trust Me v2.1")


def test_substring_near_match_must_NOT_be_equal():
    # The DeathZxZ bug: short name MUST NOT swallow longer name
    assert not is_same_mod(
        "Infinite Stamina",
        "Infinite Stamina All Skills Horse Spirit")


def test_totally_different_names_not_equal():
    assert not is_same_mod("Barber Unlocked", "Faster Interactions All")


def test_mod_name_with_file_stem_strips_nexus_suffix():
    # NexusMods download filename → prettify strips -350--5-1775316604
    assert is_same_mod(
        "BetterInventoryUI-350--5-1775316604.zip",
        "Better Inventory UI")


# ── Token overlap ratio ──────────────────────────────────────────────


def test_token_overlap_ratio_identical_is_one():
    assert token_overlap_ratio("Better Trade Menu", "Better Trade Menu") == 1.0


def test_token_overlap_ratio_disjoint_is_zero():
    assert token_overlap_ratio("Foo Bar", "Baz Qux") == 0.0


def test_token_overlap_ratio_near_match_is_in_trigger_band():
    # 'Infinite Stamina' (2 tokens) vs 'Infinite Stamina All Skills Horse
    # Spirit' (6 tokens) → intersection=2, union=6 → 0.333.
    # Short/long pairs fall below the 0.6 threshold — which is CORRECT:
    # the user explicitly labeled one as 'All Skills Horse Spirit'
    # scope. Don't prompt "update?", let them add as new mod cleanly.
    r = token_overlap_ratio(
        "Infinite Stamina",
        "Infinite Stamina All Skills Horse Spirit")
    assert 0.0 < r < 0.6


def test_token_overlap_ratio_one_extra_token_triggers_near_match():
    # 'Better Trade Menu' (3) vs 'Better Trade Menu Comp' (4)
    # intersection=3, union=4 → 0.75 → near-match dialog fires
    r = token_overlap_ratio("Better Trade Menu", "Better Trade Menu Comp")
    assert r >= 0.6


def test_token_overlap_ratio_symmetric():
    a = token_overlap_ratio("Foo Bar Baz", "Bar Baz Qux")
    b = token_overlap_ratio("Bar Baz Qux", "Foo Bar Baz")
    assert a == b


def test_empty_name_ratio_is_zero():
    assert token_overlap_ratio("", "Anything") == 0.0
    assert token_overlap_ratio("Anything", "") == 0.0
