"""v3.1.7 follow-up: import must dedupe identical JSON files that
appear at multiple nesting levels.

Real bug from CDInventoryExpander v2.5.0: the source folder shipped
3 nested copies of itself, each containing the same
``CDInventoryExpander.json``. CDUMM's ``rglob("*.json")`` happily
found all three and ``import_from_folder`` created 3 mod rows for
what is one mod.

Dedupe by content hash (SHA-256 of the file bytes). Identical files
collapse to a single mod row regardless of nesting depth. The
shallowest path wins so the user-facing source path is the one
closest to the folder they dropped.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


_PATCH = json.dumps({
    "modinfo": {"title": "Inventory Expander", "version": "2.5.0"},
    "patches": [{
        "game_file": "gamedata/inventory.pabgb",
        "changes": [{
            "offset": 28,
            "label": "x",
            "original": "3200",
            "patched": "c001",
        }],
    }],
})


def test_three_nested_identical_copies_collapse_to_one_patch(tmp_path):
    """The exact CDInventoryExpander layout: three nested copies of
    the same folder, each carrying the same JSON file. The engine
    must hand back ONE patch dict, not three."""
    from cdumm.engine.json_patch_handler import detect_json_patches_all
    drop = tmp_path / "MyMod"
    drop.mkdir()
    (drop / "MyMod.json").write_text(_PATCH, encoding="utf-8")
    inner1 = drop / "MyMod"
    inner1.mkdir()
    (inner1 / "MyMod.json").write_text(_PATCH, encoding="utf-8")
    inner2 = inner1 / "MyMod"
    inner2.mkdir()
    (inner2 / "MyMod.json").write_text(_PATCH, encoding="utf-8")
    jp_list = detect_json_patches_all(drop)
    assert len(jp_list) == 1, (
        f"expected 1 deduped patch; got {len(jp_list)}")
    # Shallowest copy wins so the user sees the path closest to the drop.
    chosen = jp_list[0]["_json_path"]
    assert chosen == drop / "MyMod.json", (
        f"shallowest path should win; got {chosen}")


def test_distinct_content_at_different_depths_kept_separate(tmp_path):
    """Sanity guard: dedupe must not collapse mods whose content
    differs even by a single byte. The Infinite Horse case has 10
    legitimately different JSONs that all need to survive."""
    from cdumm.engine.json_patch_handler import detect_json_patches_all
    drop = tmp_path / "AllVariants"
    drop.mkdir()
    a_dir = drop / "groupA"
    b_dir = drop / "groupB"
    a_dir.mkdir()
    b_dir.mkdir()
    for i, label in enumerate(["10", "25", "50"]):
        # Each gets a unique offset so content hashes differ
        patch = json.dumps({
            "modinfo": {"title": f"variant {label}", "version": "1.0"},
            "patches": [{
                "game_file": "gamedata/skill.pabgb",
                "changes": [{
                    "offset": 1000 + i,
                    "label": "x",
                    "original": "00",
                    "patched": "01",
                }],
            }],
        })
        (a_dir / f"{label}.json").write_text(patch, encoding="utf-8")
        (b_dir / f"{label}.json").write_text(patch, encoding="utf-8")
    # 6 files total: 3 unique contents × 2 copies of each.
    jp_list = detect_json_patches_all(drop)
    assert len(jp_list) == 3, (
        f"expected 3 unique patches after dedupe; got {len(jp_list)}")
