"""v3.1.7 follow-up: picker must find JSON presets nested deeper than
two folders.

Real bug from "Infinite Horse - All Variants (JSMM - CDUMM)" on Nexus:

    Infinite Horse .../          ← drop folder
    └── Infinite Horse/          ← level 1
        ├── CDUMM v2.4.2/        ← level 2 (5 JSON variants)
        └── JSMM & CDUMM v3+/    ← level 2 (5 JSON variants)

The patch JSONs sit at LEVEL 3. ``find_json_presets`` only globbed
``*.json`` at depth 1, then ``*/*.json`` at depth 2 — both empty for
this layout — so the picker was bypassed and ``import_from_folder``
silently created 10 mod cards via ``rglob``.

These tests pin a deeper search so the picker fires for legitimate
multi-variant authoring layouts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_patch(path: Path, title: str) -> None:
    """Stage a minimal valid patch JSON the picker recognises."""
    path.write_text(
        json.dumps({
            "modinfo": {"title": title, "version": "1.0"},
            "patches": [{
                "game_file": "gamedata/skill.pabgb",
                "changes": [{
                    "offset": 100,
                    "label": "x",
                    "original": "00",
                    "patched": "01",
                }],
            }],
        }),
        encoding="utf-8",
    )


def test_find_json_presets_picks_up_files_at_depth_3(tmp_path):
    """The picker must surface all 10 variants for the Infinite Horse
    layout (10 unique presets across two depth-2 subfolders)."""
    from cdumm.gui.preset_picker import find_json_presets
    drop = tmp_path / "Infinite Horse - All Variants"
    drop.mkdir()
    sub = drop / "Infinite Horse"
    sub.mkdir()
    (sub / "CDUMM v2.4.2").mkdir()
    (sub / "JSMM & CDUMM v3+").mkdir()
    for label in ("10%", "25%", "50%", "75%", "Unlimited"):
        _write_patch(
            sub / "CDUMM v2.4.2" / f"Infinite Horse (CDUMM) ({label}) (skill).json",
            f"Infinite Horse (CDUMM) ({label}) (skill)")
        _write_patch(
            sub / "JSMM & CDUMM v3+" / f"Infinite Horse (JSMM) ({label}) (skill).json",
            f"Infinite Horse (JSMM) ({label}) (skill)")
    presets = find_json_presets(drop)
    assert len(presets) == 10, (
        f"expected 10 presets discovered at depth 3; got {len(presets)}")


def test_find_json_presets_still_returns_empty_for_single_aio_at_depth_3(tmp_path):
    """The single-AIO optimisation in ``find_json_presets`` (no parse
    when ≤1 candidate, so the GUI thread doesn't burn 10s on a huge
    file the picker won't display) must survive the deeper scan."""
    from cdumm.gui.preset_picker import find_json_presets
    drop = tmp_path / "DropRoot"
    drop.mkdir()
    deep = drop / "wrap" / "deeper"
    deep.mkdir(parents=True)
    _write_patch(deep / "single_aio.json", "Solo")
    presets = find_json_presets(drop)
    assert presets == [], (
        f"≤1 candidate should short-circuit (no parse); got {presets}")


def test_find_json_presets_skips_extracted_vanilla_paz_dirs(tmp_path):
    """Deep search must skip NNNN/ and meta/ subtrees so we don't
    false-positive on extracted vanilla PAZ content that happens to
    contain JSON-shaped blobs. Same filter ``detect_json_patches_all``
    uses already."""
    from cdumm.gui.preset_picker import find_json_presets
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "0008").mkdir()  # numeric 4-digit name = vanilla PAZ dir
    (drop / "meta").mkdir()
    _write_patch(drop / "0008" / "fake_vanilla.json", "junk1")
    _write_patch(drop / "meta" / "fake_vanilla.json", "junk2")
    # Also add two LEGIT presets at depth 3
    real = drop / "VariantsHere" / "subset"
    real.mkdir(parents=True)
    _write_patch(real / "real_a.json", "real a")
    _write_patch(real / "real_b.json", "real b")
    presets = find_json_presets(drop)
    titles = sorted(p[1].get("modinfo", {}).get("title", "") for p in presets)
    assert titles == ["real a", "real b"], (
        f"expected only the two real presets; got {titles}")
