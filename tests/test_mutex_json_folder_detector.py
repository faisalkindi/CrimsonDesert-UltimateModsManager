"""Detect folders whose JSON files are mutually-exclusive alternatives.

When the picked folder (e.g. GildsGear/AbyssGears/) contains JSONs
that all patch overlapping byte offsets, they are NOT independent
siblings — they're 'pick one' alternatives (GildsGear: 7 AbyssGear
JSONs each writing different items to the same 93 shop slots).

Those folders must be imported as ONE variant mod (cog picker)
rather than 7 separate mods.
"""
from __future__ import annotations

import json
from pathlib import Path

from cdumm.engine.mutex_json_folder import (
    detect_mutex_folder_jsons,
    json_offsets,
)


def _write(path: Path, patches: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "name": path.stem,
        "patches": patches,
    }), encoding="utf-8")


def test_overlapping_jsons_detected_as_mutex(tmp_path: Path):
    """Two JSONs writing to the SAME (file, offset) pairs → mutex."""
    _write(tmp_path / "A.json", [{"game_file": "gamedata/x.pabgb", "changes": [
        {"offset": 100, "label": "A-item"},
        {"offset": 200, "label": "A-item2"},
    ]}])
    _write(tmp_path / "B.json", [{"game_file": "gamedata/x.pabgb", "changes": [
        {"offset": 100, "label": "B-item"},
        {"offset": 200, "label": "B-item2"},
    ]}])

    result = detect_mutex_folder_jsons(tmp_path)
    assert result is not None, "mutex not detected"
    assert len(result) == 2
    names = {p.name for p, _ in result}
    assert names == {"A.json", "B.json"}


def test_independent_jsons_not_flagged(tmp_path: Path):
    """Disjoint targets → not mutex, caller should use sibling import."""
    _write(tmp_path / "A.json", [{"game_file": "gamedata/x.pabgb", "changes": [
        {"offset": 100, "label": "A"}]}])
    _write(tmp_path / "B.json", [{"game_file": "gamedata/y.pabgb", "changes": [
        {"offset": 100, "label": "B"}]}])

    assert detect_mutex_folder_jsons(tmp_path) is None


def test_partial_overlap_counts_as_mutex(tmp_path: Path):
    """Even one shared (file, offset) across any pair flags mutex."""
    _write(tmp_path / "A.json", [{"game_file": "gamedata/x.pabgb", "changes": [
        {"offset": 100, "label": "A"}]}])
    _write(tmp_path / "B.json", [{"game_file": "gamedata/x.pabgb", "changes": [
        {"offset": 100, "label": "B"},
        {"offset": 200, "label": "B2"}]}])

    result = detect_mutex_folder_jsons(tmp_path)
    assert result is not None


def test_single_json_never_flagged(tmp_path: Path):
    """A folder with only one JSON isn't a variant choice."""
    _write(tmp_path / "A.json", [{"game_file": "x.pabgb", "changes": [
        {"offset": 100}]}])
    assert detect_mutex_folder_jsons(tmp_path) is None


def test_gildsgear_style_seven_variants_detected(tmp_path: Path):
    """Smoke test mirroring GildsGear AbyssGears: 7 JSONs on same offsets."""
    offsets = [617, 633, 692, 722, 738]
    for name in ("AbyssGear_1", "AbyssGear_2", "AbyssGear_3", "AbyssGear_4",
                 "AbyssGear_Blueprint_1", "AbyssGear_Blueprint_2",
                 "AbyssGear_Blueprint_3"):
        _write(tmp_path / f"{name}.json", [{
            "game_file": "gamedata/storeinfo.pabgb",
            "changes": [{"offset": o, "label": f"{name}-{o}"} for o in offsets],
        }])

    result = detect_mutex_folder_jsons(tmp_path)
    assert result is not None
    assert len(result) == 7


def test_json_offsets_helper_returns_tuples(tmp_path: Path):
    _write(tmp_path / "x.json", [{"game_file": "f.pabgb", "changes": [
        {"offset": 10}, {"offset": 20}]}])
    assert json_offsets(tmp_path / "x.json") == {
        ("f.pabgb", 10), ("f.pabgb", 20),
    }


def test_ignores_changes_without_offset(tmp_path: Path):
    """Changes with no numeric offset (entry-anchored only) don't break detection."""
    _write(tmp_path / "A.json", [{"game_file": "f.pabgb", "changes": [
        {"offset": 10}, {"entry": "Anchor"}]}])
    # Only (f.pabgb, 10) should be returned.
    assert json_offsets(tmp_path / "A.json") == {("f.pabgb", 10)}
