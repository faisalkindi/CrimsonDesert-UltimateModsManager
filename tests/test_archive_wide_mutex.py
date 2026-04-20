"""D: detect when an entire archive is one big mutex set across folders.

GildsGear has 10 category folders (Abyss Gears, Armors, Weapons, ...)
and each folder has multiple JSONs. EVERY JSON in every folder patches
the same 93 shop slots, just with different items. So the whole
archive is one big 'pick one of ~70' mutex set — the folder structure
is just organisation.

When detected, the picker should be skipped and every JSON across all
folders becomes one cog-style variant list with folder-prefixed names.
"""
from __future__ import annotations

import json
from pathlib import Path

from cdumm.engine.mutex_json_folder import (
    collect_archive_mutex_jsons,
)


def _write(path: Path, offsets: list[int], tag: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "name": path.stem,
        "patches": [{
            "game_file": "gamedata/storeinfo.pabgb",
            "changes": [
                {"offset": o, "label": f"{tag}-{o}"} for o in offsets
            ],
        }],
    }), encoding="utf-8")


def test_all_folders_mutex_returns_every_json_flat(tmp_path: Path):
    """GildsGear-style: all folders share offsets → flat list."""
    offsets = [617, 633, 692]
    for folder in ("AbyssGears", "Armors", "Weapons"):
        for n in range(1, 4):
            _write(tmp_path / folder / f"{folder}_{n}.json", offsets,
                   f"{folder}-{n}")

    result = collect_archive_mutex_jsons(tmp_path)
    assert result is not None, "archive-wide mutex not detected"
    assert len(result) == 9, f"expected 9 JSONs, got {len(result)}"
    # Folder prefix (prettified) should be in each variant's label so
    # the cog can group or distinguish them.
    labels = [lbl for _p, _d, lbl in result]
    assert any(lbl.startswith("Abyss Gears") for lbl in labels)
    assert any(lbl.startswith("Armors") for lbl in labels)
    assert any(lbl.startswith("Weapons") for lbl in labels)


def test_disjoint_folders_return_none(tmp_path: Path):
    """Truly independent folders (different files) should NOT be flattened."""
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "a.json").write_text(json.dumps({
        "patches": [{"game_file": "gamedata/x.pabgb",
                     "changes": [{"offset": 10}]}]}))
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "b.json").write_text(json.dumps({
        "patches": [{"game_file": "gamedata/y.pabgb",
                     "changes": [{"offset": 10}]}]}))
    assert collect_archive_mutex_jsons(tmp_path) is None


def test_single_folder_not_flattened(tmp_path: Path):
    """One folder with multiple mutex JSONs uses the per-folder path,
    not the archive-wide path (the latter is for multi-folder packs)."""
    _write(tmp_path / "Only" / "a.json", [10, 20], "A")
    _write(tmp_path / "Only" / "b.json", [10, 20], "B")
    # Single folder → the archive-wide detector returns None so the
    # caller falls through to detect_mutex_folder_jsons.
    assert collect_archive_mutex_jsons(tmp_path) is None


def test_folder_prefix_in_variant_labels(tmp_path: Path):
    """The returned label should be PRETTIFIED: 'AbyssGears/AbyssGear_1'
    collapses to 'Abyss Gears / Abyss Gear 1' (CamelCase split, underscore
    to space, title case)."""
    _write(tmp_path / "AbyssGears" / "AbyssGear_1.json", [10, 20], "abyss1")
    _write(tmp_path / "Armors" / "AllArmor_1.json", [10, 20], "armor1")

    result = collect_archive_mutex_jsons(tmp_path)
    assert result is not None
    label_set = {lbl for _p, _d, lbl in result}
    assert "Abyss Gears / Abyss Gear 1" in label_set
    assert "Armors / All Armor 1" in label_set
