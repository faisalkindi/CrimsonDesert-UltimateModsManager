"""NexusMods filename parsing — anchor on rightmost numeric suffix.

Codex caught the regression: the previous non-greedy ``.+?-`` regex
captured the FIRST numeric segment as the mod id, which meant filenames
whose display name ended with a year or other numeric marker
(``MyMod-2024-...``) got the wrong id persisted.
"""
from __future__ import annotations

from cdumm.engine.nexus_filename import (
    extract_version_from_filename, parse_nexus_filename,
)


# ── extract_version_from_filename (fallback chain) ───────────────────


def test_extract_version_nexus_timestamped():
    assert extract_version_from_filename(
        "Better Radial Menus (RAW)-618-1-4-1775912922") == "1.4"


def test_extract_version_v_prefixed_with_dots():
    assert extract_version_from_filename("stamina_v1.02.00_infinite") == "1.02.00"


def test_extract_version_v_prefixed_no_dots():
    # Authors who ship 'v107' meaning 'v1.07' get the raw '107' back.
    assert extract_version_from_filename("NSLWInventoryMod_v107_BagBoost") == "107"


def test_extract_version_bare_dotted():
    assert extract_version_from_filename(
        "Even Faster Vanilla Animations Trimmer 1.03.00") == "1.03.00"


def test_extract_version_parens_dotted():
    assert extract_version_from_filename(
        "Glider Stamina Unlimited (1.03.00)") == "1.03.00"


def test_extract_version_space_v_prefixed():
    assert extract_version_from_filename("Trust Me v2.1 Patched") == "2.1"


def test_extract_version_single_digit_int_does_not_match():
    # Bare single int with no decimal shouldn't be confused with version
    assert extract_version_from_filename("SomeMod 7 Fix") == ""


def test_extract_version_no_version_anywhere():
    assert extract_version_from_filename("Faster Interactions All") == ""


def test_extract_version_does_not_misfire_on_words_with_v():
    # Words like 'Save', 'Cave', 'Over' embed 'v' but no digits follow
    assert extract_version_from_filename("Cave Overhaul Mod") == ""


# ── parse_nexus_filename (unchanged legacy contract) ─────────────────


def test_standard_mod_with_single_digit_version():
    assert parse_nexus_filename(
        "Legendary Bear Without Tack-934-2-1775958271") == (934, "2")


def test_standard_mod_with_dotted_version():
    assert parse_nexus_filename(
        "Better Radial Menus (RAW)-618-1-4-1775912922") == (618, "1.4")


def test_standard_mod_with_three_part_version():
    assert parse_nexus_filename(
        "No Letterbox (RAW)-208-1-4-2-1775938453") == (208, "1.4.2")


def test_filename_with_year_prefix_does_not_capture_year_as_mod_id():
    # Codex P2 regression case: 'MyMod 2024' as display name encodes
    # as 'MyMod-2024' in the filename. Old regex grabbed 2024 as mod_id.
    assert parse_nexus_filename(
        "MyMod-2024-207-1-1775958271") == (207, "1")


def test_filename_with_leading_dot_version_double_dash():
    # Version '.5' encodes as '--5' in the filename (the first dash is
    # the separator, the second dash is the literal leading dot).
    assert parse_nexus_filename(
        "MyMod-350--5-1775316604") == (350, ".5")


def test_filename_with_multi_word_name_including_digits():
    # 'CD Loot Multiplier 10x' → 'CD-Loot-Multiplier-10x' is blocked
    # by the 10x token (not purely numeric), so the next numeric
    # segment (mod_id) is unambiguous.
    assert parse_nexus_filename(
        "CD-Inventory-Expander-54-1-1775123456") == (54, "1")


def test_non_matching_returns_none():
    assert parse_nexus_filename("random_file_name") == (None, "")


def test_mod_id_out_of_range_returns_none():
    # Mod IDs are 1..999999 per Nexus convention
    assert parse_nexus_filename("X-9999999-1-1775123456") == (None, "")


def test_timestamp_not_ten_digits_does_not_match():
    assert parse_nexus_filename("X-500-1-17751234") == (None, "")
