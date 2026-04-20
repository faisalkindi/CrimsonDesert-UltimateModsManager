"""C-H7: label replay must use (patch_idx, change_idx) keys, not label text.

synthesize_merged_json currently filters by `c["label"] in selected_set`
where selected_set is a set of label strings. If two changes share the
same label (common in variant mods that repeat e.g. "Increase stack by
10x" across multiple items), picking one picks BOTH — or dropping one
drops both.

Codex P1 finding. Fix: store picks as (patch_idx, change_idx) tuples
and filter by index position.
"""
from __future__ import annotations

import json
from pathlib import Path

from cdumm.engine.variant_handler import synthesize_merged_json


def _write_variant(vdir: Path, filename: str, patches: list[dict]) -> None:
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / filename).write_text(json.dumps({
        "name": filename.replace(".json", ""),
        "patches": patches,
    }), encoding="utf-8")


def test_duplicate_labels_filter_picks_correct_change(tmp_path: Path):
    """Two changes with identical label; pick ONE; the other must drop."""
    mod_dir = tmp_path / "mod42"
    vdir = mod_dir / "variants"
    _write_variant(vdir, "alt.json", [
        {"game_file": "gamedata/x.pabgb", "changes": [
            {"offset": 100, "label": "Increase stack", "original": "01",
             "patched": "0A"},   # patch 0, change 0
            {"offset": 200, "label": "Increase stack", "original": "02",
             "patched": "14"},   # patch 0, change 1 — SAME label
            {"offset": 300, "label": "Other tweak", "original": "03",
             "patched": "05"},   # patch 0, change 2
        ]},
    ])
    variants = [{"filename": "alt.json", "enabled": True, "group": -1,
                 "label": "alt"}]
    # User picked ONLY the FIRST "Increase stack" (patch 0, change 0).
    # The second change with the same label (patch 0, change 1) should
    # be dropped even though its label is identical.
    label_selections = {"alt.json": [[0, 0], [0, 2]]}  # index-based

    dest = synthesize_merged_json(mod_dir, variants,
                                  label_selections=label_selections)
    merged = json.loads(dest.read_text(encoding="utf-8"))
    # Only the picked changes survive.
    kept_offsets = [
        c["offset"] for p in merged["patches"] for c in p["changes"]
    ]
    assert 100 in kept_offsets, "user-picked change at offset 100 must survive"
    assert 300 in kept_offsets, "user-picked change at offset 300 must survive"
    assert 200 not in kept_offsets, (
        f"change at offset 200 had same label as 100 but was NOT picked — "
        f"must drop; got kept={kept_offsets}")


def test_unlabeled_changes_always_kept(tmp_path: Path):
    """Changes with no label can't be toggled — always kept."""
    mod_dir = tmp_path / "mod42"
    vdir = mod_dir / "variants"
    _write_variant(vdir, "alt.json", [
        {"game_file": "gamedata/x.pabgb", "changes": [
            {"offset": 10, "original": "01", "patched": "02"},  # no label
            {"offset": 20, "label": "Optional", "original": "03",
             "patched": "04"},
        ]},
    ])
    variants = [{"filename": "alt.json", "enabled": True, "group": -1,
                 "label": "alt"}]
    # User picked nothing from "alt.json".
    label_selections = {"alt.json": []}

    dest = synthesize_merged_json(mod_dir, variants,
                                  label_selections=label_selections)
    merged = json.loads(dest.read_text(encoding="utf-8"))
    offsets = [c["offset"] for p in merged["patches"] for c in p["changes"]]
    assert 10 in offsets, "unlabeled change must be kept"
    assert 20 not in offsets, "labeled change user didn't pick must drop"


def test_filename_not_in_selections_keeps_all(tmp_path: Path):
    """If a variant's filename has no entry, ALL its changes are kept
    (backward-compat default)."""
    mod_dir = tmp_path / "mod42"
    vdir = mod_dir / "variants"
    _write_variant(vdir, "a.json", [
        {"game_file": "gamedata/x.pabgb", "changes": [
            {"offset": 10, "label": "L1"},
            {"offset": 20, "label": "L2"},
        ]},
    ])
    variants = [{"filename": "a.json", "enabled": True, "group": -1,
                 "label": "a"}]
    # No entry for a.json → keep everything.
    label_selections = {"b.json": [[0, 0]]}

    dest = synthesize_merged_json(mod_dir, variants,
                                  label_selections=label_selections)
    merged = json.loads(dest.read_text(encoding="utf-8"))
    offsets = [c["offset"] for p in merged["patches"] for c in p["changes"]]
    assert 10 in offsets and 20 in offsets
