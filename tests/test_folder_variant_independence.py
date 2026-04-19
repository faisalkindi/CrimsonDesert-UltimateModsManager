"""B: detect whether folder-variants are mutually exclusive or independent.

Variants like Vaxis LoD (ExtraShadows vs NoExtraShadows) target the same
game files — pick one, the other wins. Mega-packs like GildsGear's
AbyssGears/Armors/Weapons/... each target different game files and can
coexist. The picker UX should distinguish:

  * Mutually exclusive → radio group (single pick).
  * Independent → checkbox group (pick any combo).

This test exercises the pure detector. GUI wiring is separate.
"""
from __future__ import annotations

import json
from pathlib import Path

from cdumm.gui.preset_picker import (
    folder_variant_game_files,
    folders_are_independent,
)


def _make_json(path: Path, game_file: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "name": path.stem,
        "patches": [{"game_file": game_file, "changes": []}],
    }), encoding="utf-8")


def test_overlapping_variants_are_not_independent(tmp_path: Path):
    """Vaxis-style: both variants patch the same .pabgb → mutually exclusive."""
    _make_json(tmp_path / "ExtraShadows" / "mod.json", "gamedata/envinfo.pabgb")
    _make_json(tmp_path / "NoExtraShadows" / "mod.json", "gamedata/envinfo.pabgb")

    folders = [tmp_path / "ExtraShadows", tmp_path / "NoExtraShadows"]
    gf = folder_variant_game_files(folders)
    assert not folders_are_independent(gf), (
        "Variants targeting the same game_file MUST be mutually exclusive")


def test_non_overlapping_categories_are_independent(tmp_path: Path):
    """GildsGear-style: different target files → user can install all."""
    _make_json(tmp_path / "AbyssGears" / "a.json", "gamedata/storeinfo.pabgb")
    _make_json(tmp_path / "Armors" / "b.json", "gamedata/itemarmor.pabgb")
    _make_json(tmp_path / "Weapons" / "c.json", "gamedata/itemweapon.pabgb")

    folders = [tmp_path / n for n in ("AbyssGears", "Armors", "Weapons")]
    gf = folder_variant_game_files(folders)
    assert folders_are_independent(gf), (
        f"Non-overlapping folders must be independent; got {gf}")


def test_partial_overlap_is_not_independent(tmp_path: Path):
    """If ANY pair overlaps, the whole set is mutex (can't split arbitrarily)."""
    _make_json(tmp_path / "A" / "a.json", "gamedata/x.pabgb")
    _make_json(tmp_path / "B" / "b.json", "gamedata/y.pabgb")
    _make_json(tmp_path / "C" / "c.json", "gamedata/x.pabgb")  # overlaps A

    folders = [tmp_path / n for n in ("A", "B", "C")]
    gf = folder_variant_game_files(folders)
    assert not folders_are_independent(gf)


def test_empty_folder_target_set_stays_independent(tmp_path: Path):
    """A folder with no JSON game_files contributes nothing — not a conflict."""
    (tmp_path / "Empty").mkdir()
    _make_json(tmp_path / "Real" / "r.json", "gamedata/z.pabgb")

    folders = [tmp_path / "Empty", tmp_path / "Real"]
    gf = folder_variant_game_files(folders)
    assert folders_are_independent(gf)


def test_multiple_jsons_in_one_folder_aggregate(tmp_path: Path):
    """A category folder with many JSONs contributes the UNION of their targets."""
    _make_json(tmp_path / "A" / "one.json", "gamedata/x.pabgb")
    _make_json(tmp_path / "A" / "two.json", "gamedata/y.pabgb")
    _make_json(tmp_path / "B" / "b.json", "gamedata/z.pabgb")

    folders = [tmp_path / "A", tmp_path / "B"]
    gf = folder_variant_game_files(folders)
    assert gf[folders[0]] == {"gamedata/x.pabgb", "gamedata/y.pabgb"}
    assert gf[folders[1]] == {"gamedata/z.pabgb"}
    assert folders_are_independent(gf)
