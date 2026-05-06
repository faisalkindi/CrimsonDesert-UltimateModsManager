"""Skipped-mod-badge plumbing, chunk 1A: when the JSON mod aggregator
folds multiple mods' patches into one synthetic patch list, each
individual change must carry a `_source_mod_id` field so downstream
skip-recording can attribute byte-mismatch failures back to the
specific mod that supplied the change.

Without this, a partial-skip apply only knows "N patches skipped
total" but cannot tell which mod's patches failed, so the post-apply
UI can't badge the responsible mod card.
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest


def _seed_json_mod(db, tmp_path: Path, mod_id: int, name: str,
                   patches: list[dict]) -> Path:
    """Insert a JSON mod row + write its source JSON. Returns json path."""
    json_path = tmp_path / f"mod_{mod_id}.json"
    json_path.write_text(
        json.dumps({"patches": patches}), encoding="utf-8")
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, "
        "json_source) VALUES (?, ?, 'paz', 1, ?, ?)",
        (mod_id, name, mod_id, str(json_path)))
    db.connection.commit()
    return json_path


def test_aggregator_tags_each_change_with_source_mod_id(tmp_path: Path):
    from cdumm.engine.apply_engine import (
        aggregate_json_mods_into_synthetic_patches)
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()

    # Mod 1 contributes 2 changes to game_file 'A.pabgb'
    _seed_json_mod(db, tmp_path, 1, "Mod A", [{
        "game_file": "A.pabgb",
        "changes": [
            {"label": "A1", "offset": 100, "original": "00", "patched": "01"},
            {"label": "A2", "offset": 200, "original": "00", "patched": "02"},
        ],
    }])
    # Mod 2 contributes 1 change to the same game_file
    _seed_json_mod(db, tmp_path, 2, "Mod B", [{
        "game_file": "A.pabgb",
        "changes": [
            {"label": "B1", "offset": 300, "original": "00", "patched": "03"},
        ],
    }])

    synth_data, summary = aggregate_json_mods_into_synthetic_patches(db)

    # Find the patch for A.pabgb in the synth output
    patch = next(p for p in synth_data["patches"]
                 if p["game_file"] == "A.pabgb")

    assert len(patch["changes"]) == 3, (
        f"Expected 3 aggregated changes, got {len(patch['changes'])}")

    # Each change must carry _source_mod_id pointing back to the mod
    # that contributed it.
    by_label = {c["label"]: c for c in patch["changes"]}
    assert by_label["A1"].get("_source_mod_id") == 1, (
        f"A1 missing _source_mod_id=1: {by_label['A1']!r}")
    assert by_label["A2"].get("_source_mod_id") == 1
    assert by_label["B1"].get("_source_mod_id") == 2, (
        f"B1 missing _source_mod_id=2: {by_label['B1']!r}")

    db.close()
