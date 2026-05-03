"""When two mods each ship a numbered directory (e.g. 0036/) that
DOESN'T exist in vanilla, both currently get imported at the same
path. On Apply, last-wins on the entire archive — one mod's content
silently overwrites the other.

GitHub #59 (DoRoon, 2026-05-01) reports SwapButcherWithBarber +
Character Creator Female (Nexus mod 837) can't coexist:
- SwapButcherWithBarber/0036/0.paz contains 3 sequencer entries
- CharacterCreatorFemale/HumanFemale/0036/0.paz contains 14 UI XML
  entries
- Vanilla has no 0036/ directory at all
- They share zero entry paths, so they SHOULD coexist

Root cause: _detect_standalone_mod's check at import_handler.py:1479
skips dirs whose vanilla counterpart doesn't exist:

    if not game_pamt.exists():
        continue  # no vanilla dir = truly new, handled elsewhere

But the "elsewhere" path (_match_game_files regular fallback) keeps
the original `0036/` path, so both mods end up writing the same
target. Fix: treat truly-new mod dirs as standalone too — each gets
a unique remapped directory number, and both apply independently.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def _make_fake_paz_pair(dir_path: Path, paz_size: int = 4096,
                        pamt_size: int = 256) -> None:
    """Drop a fake (but plausibly-sized) 0.paz + 0.pamt pair."""
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "0.paz").write_bytes(b"X" * paz_size)
    (dir_path / "0.pamt").write_bytes(b"P" * pamt_size)


def test_standalone_remap_for_truly_new_dir(tmp_path: Path):
    """A mod shipping 0036/0.paz + 0036/0.pamt where vanilla 0036/
    doesn't exist must be remapped to a unique directory number so two
    such mods can coexist."""
    from cdumm.engine.import_handler import (
        _detect_standalone_mod, clear_assigned_dirs)

    clear_assigned_dirs()

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    # Drop a couple of dummy vanilla dirs so _next_paz_directory has
    # something to count past, but NOT 0036.
    (game_dir / "0030").mkdir()
    (game_dir / "0035").mkdir()

    extracted_dir = tmp_path / "extracted"
    _make_fake_paz_pair(extracted_dir / "0036")

    remap = _detect_standalone_mod(extracted_dir, game_dir, snapshot=None)

    assert remap is not None, (
        "Truly-new dir was skipped by standalone detection. "
        "Two mods shipping the same brand-new dir number will collide."
    )
    assert "0036" in remap, (
        f"Expected '0036' to be remapped, got {remap!r}"
    )
    assert remap["0036"].isdigit() and len(remap["0036"]) == 4, (
        f"Remap target must be a 4-digit dir number, got {remap['0036']!r}"
    )


def test_two_mods_with_same_truly_new_dir_get_different_numbers(tmp_path: Path):
    """Sequential _detect_standalone_mod calls on two extracted mods
    that both ship 0036/0.paz must produce DIFFERENT remap targets so
    the mods don't overwrite each other on apply.
    """
    from cdumm.engine.import_handler import (
        _detect_standalone_mod, clear_assigned_dirs, _assigned_dirs)

    clear_assigned_dirs()

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "0035").mkdir()

    # Mod A
    mod_a = tmp_path / "mod_a"
    _make_fake_paz_pair(mod_a / "0036")
    remap_a = _detect_standalone_mod(mod_a, game_dir, snapshot=None)

    assert remap_a is not None and "0036" in remap_a
    target_a = remap_a["0036"]

    # Reserve mod A's slot like a real import would. _detect_standalone_mod
    # adds to _assigned_dirs internally via _next_paz_directory, so this
    # check confirms the assignment is recorded.
    assert int(target_a) in _assigned_dirs, (
        f"Mod A's assigned dir {target_a} not tracked in _assigned_dirs; "
        f"mod B will pick the same number and the two mods will collide."
    )

    # Mod B with same source dir name
    mod_b = tmp_path / "mod_b"
    _make_fake_paz_pair(mod_b / "0036")
    remap_b = _detect_standalone_mod(mod_b, game_dir, snapshot=None)

    assert remap_b is not None and "0036" in remap_b
    target_b = remap_b["0036"]

    assert target_a != target_b, (
        f"Both mods got the same remap target {target_a!r}. "
        f"On apply, the second mod's archive will overwrite the first's. "
        f"This is the GitHub #59 (DoRoon) symptom."
    )
