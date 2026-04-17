"""NexusMods filename parsing — anchor on rightmost numeric suffix.

Codex caught the regression: the previous non-greedy ``.+?-`` regex
captured the FIRST numeric segment as the mod id, which meant filenames
whose display name ended with a year or other numeric marker
(``MyMod-2024-...``) got the wrong id persisted.
"""
from __future__ import annotations

from cdumm.engine.nexus_filename import parse_nexus_filename


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
