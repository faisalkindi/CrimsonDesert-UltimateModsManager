"""Picker label disambiguation when multiple presets share `data.name`.

Bug from ZapZockt 2026-04-26 (GitHub #49, Character Creator v4.9):
The picker showed three rows all labeled "Female Animations" because
the JSON's internal name field was identical across HumanFemale,
OrcFemale, and GoblinFemale. User couldn't tell which was which.

Fix: extract a pure helper `compute_picker_labels(presets)` that
returns one label per preset. When labels collide, append a
distinguishing parent-folder hint so the user sees:

    Female Animations (HumanFemale)
    Female Animations (OrcFemale)
    Female Animations (GoblinFemale)
"""
from __future__ import annotations
from pathlib import Path


def test_unique_names_unchanged():
    """When every preset has a unique `name`, labels pass through."""
    from cdumm.gui.preset_picker import compute_picker_labels

    presets = [
        (Path("A.json"), {"name": "Mod Alpha"}),
        (Path("B.json"), {"name": "Mod Beta"}),
        (Path("C.json"), {"name": "Mod Gamma"}),
    ]
    labels = compute_picker_labels(presets)
    assert labels == ["Mod Alpha", "Mod Beta", "Mod Gamma"]


def test_colliding_names_get_parent_folder_hint():
    """The Character Creator case: same `name` across 3 races."""
    from cdumm.gui.preset_picker import compute_picker_labels

    presets = [
        (Path("CharacterCreatorFemale/HumanFemale/FemaleAnimations.json"),
         {"name": "Female Animations"}),
        (Path("CharacterCreatorFemale/OrcFemale/FemaleAnimations.json"),
         {"name": "Female Animations"}),
        (Path("CharacterCreatorFemale/GoblinFemale/FemaleAnimations.json"),
         {"name": "Female Animations"}),
    ]
    labels = compute_picker_labels(presets)
    assert len(set(labels)) == 3, (
        f"Expected 3 distinct labels, got: {labels}")
    # Each label should retain the base name and gain a hint.
    for lbl in labels:
        assert "Female Animations" in lbl
    # The race name should distinguish them.
    joined = " ".join(labels).lower()
    assert "human" in joined
    assert "orc" in joined
    assert "goblin" in joined


def test_falls_back_to_path_stem_when_name_missing():
    """JSONs without a `name` field fall back to the file stem."""
    from cdumm.gui.preset_picker import compute_picker_labels

    presets = [
        (Path("a/x.json"), {}),
        (Path("b/y.json"), {}),
    ]
    labels = compute_picker_labels(presets)
    assert labels == ["x", "y"]


def test_windows_drive_root_excluded_from_hints():
    """Path parts on Windows include 'C:\\\\' as a part. Disambiguation
    must NOT produce labels like 'Foo (C:\\\\)' since drive letters carry
    no meaning to the user. The filter should strip drive roots."""
    from cdumm.gui.preset_picker import compute_picker_labels

    presets = [
        (Path(r"C:\tmp\extracted\Female\FemaleAnimations.json"),
         {"name": "Female Animations"}),
        (Path(r"C:\tmp\extracted\Male\FemaleAnimations.json"),
         {"name": "Female Animations"}),
    ]
    labels = compute_picker_labels(presets)
    for lbl in labels:
        assert ":" not in lbl, (
            f"Drive root leaked into label: {lbl!r}")
        assert "C:" not in lbl, (
            f"Drive root leaked into label: {lbl!r}")
