"""Variant disambiguation when multiple presets share a basename.

Bug from ZapZockt 2026-04-26 (GitHub #49, Character Creator v4.9):
The Character Creator zip ships six JSONs all named the same in
different parent folders:

    CharacterCreatorFemale/HumanFemale/FemaleAnimations.json
    CharacterCreatorFemale/OrcFemale/FemaleAnimations.json
    CharacterCreatorFemale/GoblinFemale/FemaleAnimations.json
    CharacterCreatorMale/HumanMale/MaleAnimations.json
    ...

`copy_variants_to_mod_dir` was using `dest = vdir / src_path.name`,
so every FemaleAnimations.json overwrote the previous one. Only the
LAST file survived. The variants metadata still listed all 3 with
`filename = "FemaleAnimations.json"`, but they all pointed at the
same on-disk file.

Result: switching variants in the cog panel did nothing because
they all reference the same overwritten file.

Fix: when multiple presets share a basename, prefix with the
parent folder(s) to make each on-disk filename unique. Update the
variants metadata's `filename` field to match the on-disk name.
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest


def _make_json_file(p: Path, name: str = "Test Mod") -> dict:
    """Write a minimal valid JSON-mod file at p, return its parsed data."""
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "patches": [{"game_file": "test.pabgb",
                     "changes": [{"offset": 0, "patched": "00"}]}],
    }
    p.write_text(json.dumps(data), encoding="utf-8")
    return data


def test_copy_variants_disambiguates_basename_collisions(tmp_path):
    """Two presets named 'FemaleAnimations.json' under different
    parent folders must both survive the copy."""
    from cdumm.engine.variant_handler import copy_variants_to_mod_dir

    # Build the Character Creator-style layout
    src = tmp_path / "src"
    p1 = src / "CharacterCreatorFemale" / "HumanFemale" / "FemaleAnimations.json"
    p2 = src / "CharacterCreatorFemale" / "OrcFemale" / "FemaleAnimations.json"
    p3 = src / "CharacterCreatorFemale" / "GoblinFemale" / "FemaleAnimations.json"
    d1 = _make_json_file(p1, name="Female Animations")
    d2 = _make_json_file(p2, name="Female Animations")
    d3 = _make_json_file(p3, name="Female Animations")
    # Tag each file with a distinct marker so we can check survival
    p1.write_text(json.dumps({**d1, "_marker": "human"}), encoding="utf-8")
    p2.write_text(json.dumps({**d2, "_marker": "orc"}), encoding="utf-8")
    p3.write_text(json.dumps({**d3, "_marker": "goblin"}), encoding="utf-8")

    presets = [(p1, d1), (p2, d2), (p3, d3)]
    mod_dir = tmp_path / "mod"
    vdir = copy_variants_to_mod_dir(presets, mod_dir)

    # All three files must exist on disk after the copy.
    files_on_disk = sorted(vdir.glob("*.json"))
    assert len(files_on_disk) == 3, (
        f"Expected 3 distinct files, got {len(files_on_disk)}: "
        f"{[f.name for f in files_on_disk]}"
    )

    # All three markers must survive (none overwritten).
    markers = set()
    for f in files_on_disk:
        d = json.loads(f.read_text(encoding="utf-8"))
        if "_marker" in d:
            markers.add(d["_marker"])
    assert markers == {"human", "orc", "goblin"}, (
        f"Expected all 3 markers to survive, got {markers}")


def test_build_variants_metadata_filename_matches_on_disk(tmp_path):
    """The `filename` field in variants metadata must reference the
    actual on-disk filename after collision-resolved copy. Otherwise
    `synthesize_merged_json` can't find the file."""
    from cdumm.engine.variant_handler import (
        copy_variants_to_mod_dir, build_variants_metadata,
    )

    src = tmp_path / "src"
    p1 = src / "Female" / "FemaleAnimations.json"
    p2 = src / "Male" / "MaleAnimations.json"
    p3 = src / "Female" / "Goblin" / "FemaleAnimations.json"
    d1 = _make_json_file(p1, name="Female Animations Human")
    d2 = _make_json_file(p2, name="Male Animations Human")
    d3 = _make_json_file(p3, name="Female Animations Goblin")

    presets = [(p1, d1), (p2, d2), (p3, d3)]
    mod_dir = tmp_path / "mod"
    vdir = copy_variants_to_mod_dir(presets, mod_dir)
    meta = build_variants_metadata(presets, initial_selection=None)

    # Each filename in metadata must point at a real file.
    for v in meta:
        assert (vdir / v["filename"]).exists(), (
            f"Variant metadata {v['filename']} not found on disk in "
            f"{[f.name for f in vdir.iterdir()]}")


def test_build_variants_metadata_label_disambiguates_collisions(tmp_path):
    """REGRESSION: when multiple variants share `data.name`, the
    persisted `label` field must include a parent-folder hint so the
    cog panel can tell them apart. Picker shows disambiguated labels;
    the cog panel must too. Otherwise users see three identical
    'Female Animations' rows in the side panel and can't pick the
    right one."""
    from cdumm.engine.variant_handler import build_variants_metadata

    src = tmp_path / "src"
    p1 = src / "CharacterCreatorFemale" / "HumanFemale" / "FemaleAnimations.json"
    p2 = src / "CharacterCreatorFemale" / "OrcFemale" / "FemaleAnimations.json"
    p3 = src / "CharacterCreatorFemale" / "GoblinFemale" / "FemaleAnimations.json"
    d1 = _make_json_file(p1, name="Female Animations")
    d2 = _make_json_file(p2, name="Female Animations")
    d3 = _make_json_file(p3, name="Female Animations")

    presets = [(p1, d1), (p2, d2), (p3, d3)]
    meta = build_variants_metadata(presets, initial_selection=None)

    labels = [v["label"] for v in meta]
    assert len(set(labels)) == 3, (
        f"Cog panel labels must be distinct, got: {labels}")
    joined = " ".join(labels).lower()
    assert "human" in joined and "orc" in joined and "goblin" in joined, (
        f"Race hint missing from labels: {labels}")
