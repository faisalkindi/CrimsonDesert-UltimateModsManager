"""GitHub #379: 'Check for Mod Updates' says everything is up to date.

The update check only looks at mods with a Nexus id, and for manual
downloads that id comes from the filename. Nexus now names downloads

    '{FileName} {mod_id} {version} {YYYY-MM-DDTHH-MMZ} {9-char id}'

(or the same without the timestamp), and ``parse_nexus_filename`` only
knew the dash-separated form, so every such mod was left unlinked and
never checked. Names under an issue number are copied from that bug
report; where it printed the row's stored id (``nexus=<id>``), the
expected id here matches it. The others are made up to pin one rule.
"""
from __future__ import annotations

import pytest

from cdumm.engine.nexus_filename import (
    extract_version_from_filename,
    parse_nexus_filename,
    split_nexus_filename,
)

# ── space-separated, with the timestamp ──────────────────────────────

@pytest.mark.parametrize("name, expected", [
    # #465: the browser's duplicate-download suffix sits against the id.
    ("No Peach Fuzz 3941 1 2026-10-31T18-11Z vEjq5ZqS9(1).zip", (3941, "1")),
    ("No Peach Fuzz 3941 1 2026-10-31T18-11Z vEjq5ZqS9(2).zip", (3941, "1")),
    # #415
    ("CD QOL Suite 1591 2.2 2026-06-09T02-45Z 5M3ctPNGm.zip", (1591, "2.2")),
    # #415: nested-variant drop_name, '||<variant>' appended by CDUMM.
    ("Fat Stacks - Manual 157 2.00.00 2026-08-27T07-04Z LVZD4oiEq.zip||fat_stacks_100x",
     (157, "2.00.00")),
    # #458
    ("Better Inventory and Trade UI 452 3.0 2026-08-26T15-38Z mhw3QMq4Q.zip",
     (452, "3.0")),
    ("Bigger Minimap Fixed Zoom Status Bar Persistent 275 2.8 2026-08-26T12-14Z xBfWAn3OY.zip",
     (275, "2.8")),
    # #458: no extension; that report's row shows nexus=3528.
    ("Barber Unlocked Updated 3528 1.1 2026-09-18T14-52Z ZarUP9Utd",
     (3528, "1.1")),
    # #191, 07-18 list.
    ("No Cooldown Infinite Durability Stamina Spirit 2714 1.13.00 2026-07-06T21-44Z ifG1RWTp5",
     (2714, "1.13.00")),
    # #259: the version keeps its letters.
    ("Cheap Gold Bars 2190 CD1.13.01 2026-07-08T08-25Z 2kQtbOaer.zip",
     (2190, "CD1.13.01")),
    # #259: dots and a dash inside the file name.
    ("Shop Smart. Shop H-Smart. 2626 CD1.13.01 2026-07-08T08-34Z HdJfoBwCG.zip",
     (2626, "CD1.13.01")),
    # #467: a version-looking '1.3.0' inside the file name.
    ("QuestGiversOnMap ResidentsTales 1.3.0 WITHOUT DLC 3719 1.3 2026-10-31T22-18Z oj90sq0xd.zip",
     (3719, "1.3")),
    # #383
    ("Fat Stacks - JSONv3 157 1.18.02 2026-08-17T10-17Z nySpzGLK0.zip||_f3_variants/100x",
     (157, "1.18.02")),
])
def test_space_separated_with_timestamp(name, expected):
    assert parse_nexus_filename(name) == expected


# ── space-separated, without the timestamp ───────────────────────────

@pytest.mark.parametrize("name, expected", [
    # #246, all three 'unlinked' in that report.
    ("NPC Trust Gain 350 1.13 hQ8Cntr88", (350, "1.13")),
    ("Max Inventory Storage 1561 CD1.13.00a V4a9ljfJP", (1561, "CD1.13.00a")),
    ("Bigger Minimap Fixed Zoom 275 2.5 UHbidxlb7", (275, "2.5")),
    # #191, 07-18 list (same file as #254's).
    ("Fat Stacks - JSONv3 157 1.13.00 9eHxTImgy", (157, "1.13.00")),
    # #247: the reporter numbered their files.
    ("26.Even Faster Vanilla Animations Trimmer 774 0.4.4 lwFT8309k.zip",
     (774, "0.4.4")),
    # #254: an id with no capital letter.
    ("Glider Stamina .json 120 1.2.18 g7Owxhj4o.zip", (120, "1.2.18")),
])
def test_space_separated_without_timestamp(name, expected):
    assert parse_nexus_filename(name) == expected


# ── dash-separated names that only failed for their suffix ───────────

@pytest.mark.parametrize("name, expected", [
    # #147: row shows nexus=2406. Chromium's ' (1)'.
    ("Healthbar Always On and multi target from Caites-2406-3-1778494982 (1).zip",
     (2406, "3")),
    # #49: Firefox's '(1)'.
    ("Skip Cube Rush Loading Scene-774-0-2-2-1775574026(1).zip", (774, "0.2.2")),
    # #91 / #163: nested-variant drop_names, rows show nexus=2387 / 837.
    ("Kliff - Effects-2387-1-1778230616.zip||_f3_variants/Reed_Devil_Shadow_Effect",
     (2387, "1")),
    ("Character Creator-837-6-0-1779534841.zip||CharacterCreator/HumanFemale",
     (837, "6.0")),
])
def test_dash_separated_with_suffixes(name, expected):
    assert parse_nexus_filename(name) == expected


# ── names that must stay unlinked ────────────────────────────────────

@pytest.mark.parametrize("name", [
    # #466: last word is nine letters long, like the id.
    "1 - Copper replaces Abyssal Artifacts",
    # #246: '9999999' is part of the name.
    "No Cooldown or Durability 9999999 Stacks",
    # #246: linked as nexus=2365 by another route; the name carries no id.
    "All Mounts LvL 5 All Stats 1.12.zip",
    # #188: another site's download name; 5015 is not a Nexus id.
    "ggmods-5015-faster-npc-animations-1.3.9.zip",
    # #128, #221
    "Better Radial Menus 1.8.1 (con controlli pad).zip",
    "Refinement Cost 1 All v2.4.0-1.11.00.zip",
    "Max Inventory Storage (1).zip",
    "Fat Stacks - Manual.zip||fat_stacks_999999",
    "0c6ca753-82bd-411a-8907-33be77e15b67",
    # #224: Nexus's id token but no mod id in the name. Nothing to link.
    "Female_Armor_Module_No_Icon_1_S2hMkXZPl.zip",
])
def test_non_nexus_names_from_reports_stay_unparsed(name):
    assert parse_nexus_filename(name) == (None, "")


@pytest.mark.parametrize("name", [
    "Mod Pack 2 1.0 Overhauls",       # capitalised word
    "Mod Pack 2 1.0 OVERHAULS",       # all-caps word
    "Mod Pack 2 1.0 Version22",       # word then digits
    "Mod Pack 2 1.0 123456789",       # all digits
    "Mod Pack 2 1.0 hQ8Cntr8",        # 8 characters, not 9
    "Some Mod 12 beta hQ8Cntr88",     # 'version' without a digit
])
def test_untimestamped_form_needs_an_id_shaped_last_word(name):
    """Without the timestamp, only the 9-character id marks a name as
    Nexus's, so ordinary words in that position must not count."""
    assert parse_nexus_filename(name) == (None, "")


@pytest.mark.parametrize("name", [
    "X 1234567 1.0 2026-10-31T18-11Z vEjq5ZqS9",
    "X 0 1.0 2026-10-31T18-11Z vEjq5ZqS9",
])
def test_space_separated_mod_id_range_matches_dash_form(name):
    """Mod ids are 1..999999, as for the dash-separated form."""
    assert parse_nexus_filename(name) == (None, "")


# ── file-name part, used by the update check's renamed-mod match ─────

@pytest.mark.parametrize("name, expected", [
    ("No Peach Fuzz 3941 1 2026-10-31T18-11Z vEjq5ZqS9(2).zip",
     ("No Peach Fuzz", 3941, "1")),
    ("Fat Stacks - Manual 157 2.00.00 2026-08-27T07-04Z LVZD4oiEq.zip||fat_stacks_100x",
     ("Fat Stacks - Manual", 157, "2.00.00")),
    ("NPC Trust Gain 350 1.13 hQ8Cntr88", ("NPC Trust Gain", 350, "1.13")),
    # #458, dash-separated: the name keeps its own '4.0'.
    ("Barber Unlocked 4.0-591-4-0-1775807101.zip",
     ("Barber Unlocked 4.0", 591, "4.0")),
    # Year-retry path keeps the year in the name.
    ("MyMod-2024-207-1-1775958271", ("MyMod-2024", 207, "1")),
])
def test_split_returns_the_file_name_part(name, expected):
    assert tuple(split_nexus_filename(name)) == expected


def test_split_returns_none_for_non_nexus_names():
    assert split_nexus_filename("random_file_name") is None


# ── version extraction now reads the Nexus slot ──────────────────────

def test_extract_version_reads_the_version_slot():
    assert extract_version_from_filename(
        "No Peach Fuzz 3941 1 2026-10-31T18-11Z vEjq5ZqS9") == "1"


def test_extract_version_no_longer_reads_the_id_token():
    """The 'v'-prefix fallback used to find 'V4' at the start of the
    id 'V4a9ljfJP' and report version '4' (#246)."""
    assert extract_version_from_filename(
        "Max Inventory Storage 1561 CD1.13.00a V4a9ljfJP") == "CD1.13.00a"
