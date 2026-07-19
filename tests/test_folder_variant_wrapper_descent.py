"""Wrapper-nested folder variants must still fire the import picker (#302).

Character Creator (Nexus 837) ships its 8 race/gender variant folders
inside a single ``Character Creator/`` wrapper directory, alongside a few
shared loose files (a ``.field.json`` animation module, an ``.asi``,
``mod.json``). ``find_folder_variants`` only inspects the level it is
given, so on the extraction root it saw the lone wrapper and returned 0 --
the import variant loop then broke immediately and the race/gender picker
never appeared (@lurkser on #302). ``descend_to_folder_variants`` walks
single wrapper dirs down to the level that actually holds the variants.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.gui.preset_picker import (
    find_folder_variants, descend_to_folder_variants,
)


def _variant(folder: Path, name: str) -> None:
    d = folder / name / "0009"
    d.mkdir(parents=True)
    (d / "0.paz").write_bytes(b"\x00" * 16)


def _cc_pack(root: Path) -> Path:
    """The Character Creator shape: one wrapper dir with 8 race/gender
    variant folders plus shared loose files. The wrapper name is NOT the
    archive stem, so the long-path wrapper-collapse (#191) does not fire."""
    wrap = root / "Character Creator"
    wrap.mkdir(parents=True)
    for race in ("Human Female", "Human Male", "Orc Female", "Orc Male",
                 "Goblin Female", "Goblin Male", "Dwarf Female", "Dwarf Male"):
        _variant(wrap, race)
    (wrap / "mod.json").write_text('{"title": "Character Creator"}')
    (wrap / "Female Animations.field.json").write_text(
        '{"format": 3, "intents": []}')
    (wrap / "CharacterCreatorHead.asi").write_bytes(b"MZ")
    return wrap


def test_wrapped_variants_are_hidden_at_the_extraction_root(tmp_path):
    """The bug: the extraction root shows only the wrapper, so the picker
    detector finds no variants and the loop breaks before offering one."""
    _cc_pack(tmp_path)
    assert len(find_folder_variants(tmp_path)) < 2


def test_descend_reaches_the_variant_level(tmp_path):
    wrap = _cc_pack(tmp_path)
    reached = descend_to_folder_variants(tmp_path)
    assert reached == wrap, "did not descend the single wrapper folder"
    assert len(find_folder_variants(reached)) == 8, (
        "all 8 race/gender variants must be visible at the descended level")


def test_descend_is_a_noop_when_variants_sit_at_the_passed_level(tmp_path):
    """A pack whose variants are already at the passed level is returned
    unchanged -- no over-descending into one of the variants."""
    _variant(tmp_path, "VariantA")
    _variant(tmp_path, "VariantB")
    assert descend_to_folder_variants(tmp_path) == tmp_path
    assert len(find_folder_variants(tmp_path)) == 2


def test_descend_is_a_noop_for_a_plain_single_folder_mod(tmp_path):
    """A normal mod that is just one folder of game files (no sibling
    variants) must not be descended into and mis-offered as a picker."""
    d = tmp_path / "SomeMod" / "0009"
    d.mkdir(parents=True)
    (d / "0.paz").write_bytes(b"\x00" * 16)
    assert descend_to_folder_variants(tmp_path) == tmp_path
    assert len(find_folder_variants(tmp_path)) < 2


def test_descend_stops_at_numbered_paz_dirs(tmp_path):
    """The variant folders themselves contain NNNN game-data dirs; the
    descent must not treat those as a wrapper to walk into."""
    wrap = _cc_pack(tmp_path)
    # Descending from the wrapper returns the wrapper (variants are here),
    # never one of the race folders or its 0009/ data dir.
    assert descend_to_folder_variants(wrap) == wrap
