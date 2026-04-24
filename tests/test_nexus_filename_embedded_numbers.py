"""Bug #13: ``parse_nexus_filename`` can capture an embedded number
from the display name as the mod_id.

The non-greedy ``.+?`` at the start of ``_NON_GREEDY`` consumes the
minimum characters before the first ``-``. For a display name that
contains an embedded number, the regex matches the first numeric
fragment of the name as the mod_id.

Real-world example: ``"FX-1000 Patcher-42-1-0-1775958271"`` has
mod_id 42 on Nexus, but the non-greedy match captures 1000 instead.
The year-retry fallback (1900-2099) only catches one specific
pattern; arbitrary embedded numbers slip through.

Fix: prefer the right-anchored greedy regex as the primary match —
it constrains the version to 1-3 numeric segments, so the real
mod_id becomes unambiguous (the integer group directly before the
version).
"""
from __future__ import annotations

import pytest


def test_embedded_number_before_real_mod_id_is_ignored():
    """Display name with an embedded dashed number must not be
    captured as the mod_id."""
    from cdumm.engine.nexus_filename import parse_nexus_filename
    # Real Nexus format: ModName-modID-versionSegments-timestamp.
    # FX-1000 is part of the display name, 42 is the real mod id.
    nid, ver = parse_nexus_filename(
        "FX-1000 Patcher-42-1-0-1775958271")
    assert nid == 42, (
        f"real mod_id is 42 (group before the version); "
        f"got {nid} — the FX-1000 fragment was misidentified")
    assert ver == "1.0"


def test_plain_dashed_name_still_parses_correctly():
    """Regression guard: dashed display names without embedded
    digits still resolve. ``Legendary Bear Without Tack-934-2-1775958271``
    is the docstring's own example."""
    from cdumm.engine.nexus_filename import parse_nexus_filename
    nid, ver = parse_nexus_filename(
        "Legendary Bear Without Tack-934-2-1775958271")
    assert nid == 934
    assert ver == "2"


def test_multi_segment_version_still_parses():
    """Regression guard for ``Better Radial Menus (RAW)-618-1-4-1775912922``
    -> (618, "1.4")."""
    from cdumm.engine.nexus_filename import parse_nexus_filename
    nid, ver = parse_nexus_filename(
        "Better Radial Menus (RAW)-618-1-4-1775912922")
    assert nid == 618
    assert ver == "1.4"


def test_three_segment_version_still_parses():
    """Regression guard for ``No Letterbox (RAW)-208-1-4-2-1775938453``
    -> (208, "1.4.2")."""
    from cdumm.engine.nexus_filename import parse_nexus_filename
    nid, ver = parse_nexus_filename(
        "No Letterbox (RAW)-208-1-4-2-1775938453")
    assert nid == 208
    assert ver == "1.4.2"


def test_name_ending_in_year_still_works():
    """Regression guard for the original year-retry case: a mod
    name ending in a 4-digit year that would otherwise be captured
    as mod_id."""
    from cdumm.engine.nexus_filename import parse_nexus_filename
    # Name ends in "2024"; real mod id is 42.
    nid, ver = parse_nexus_filename("CoolMod 2024-42-1-0-1775958271")
    assert nid == 42
