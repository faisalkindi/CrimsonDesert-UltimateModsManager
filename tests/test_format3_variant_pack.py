"""Multi-Format-3 packs are LOADOUT VARIANTS, not separate mods.

Bug from Faisal 2026-04-27 (gleglezao / Nexus): CrimsonWings ships
five Format 3 JSONs in one ZIP — `CrimsonWings_10pct.field.json`,
`_25pct`, `_50pct`, `_75pct`, `_infinite`. CDUMM's import handler
at import_handler.py:1806-1813 rejects the whole pack with
"please import them one at a time so each gets its own row in the
mod list."

That's wrong for variant packs. The user picks ONE level — the
five JSONs are alternatives to each other, not five independent
mods. Same architectural shape as multi-variant loose-file mods,
which we already handle via `find_loose_file_variants` + the
folder picker dialog.

Fix: detect F3 variant packs (2+ Format 3 JSONs sharing a common
stem prefix, all targeting the same .pabgb table, with similar
intent counts), surface them through a new `find_format3_variants`
public API. The GUI code that already calls
`find_loose_file_variants` after archive extraction will check
this too and show the same folder picker.

If detection fails (truly N independent F3 mods), fall back to the
existing "import one at a time" rejection.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_f3_json(folder: Path, name: str, target: str = "skill.pabgb",
                  intent_count: int = 365) -> Path:
    """Write a synthetic Format 3 JSON file."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(json.dumps({
        "modinfo": {"name": name},
        "format": 3,
        "target": target,
        "intents": [
            {"entry": f"E{i}", "key": i, "field": "x",
             "op": "set", "new": i}
            for i in range(intent_count)
        ],
    }))
    return p


def test_crimsonwings_5pack_detected_as_variants(tmp_path):
    """The exact bug: 5 JSONs sharing `CrimsonWings_` stem, same
    target, same intent count. Must be detected as a variant pack."""
    from cdumm.engine.import_handler import find_format3_variants

    src = tmp_path / "CrimsonWings_FieldJson"
    for level in ("10pct", "25pct", "50pct", "75pct", "infinite"):
        _make_f3_json(src, f"CrimsonWings_{level}.field.json")

    variants = find_format3_variants(tmp_path)
    assert len(variants) == 5, (
        f"5-JSON CrimsonWings pack must yield 5 variants; got "
        f"{len(variants)}: {[v.get('id') for v in variants]}")
    # Each variant dict must have the picker-required fields.
    for v in variants:
        assert "id" in v
        assert "_base_dir" in v
        assert isinstance(v["_base_dir"], Path)
        assert v["_base_dir"].is_dir(), (
            f"_base_dir must point to an actual folder for the picker; "
            f"got {v['_base_dir']!r}")


def test_two_unrelated_f3_mods_are_not_variants(tmp_path):
    """2 Format 3 JSONs with DIFFERENT targets are independent
    mods — not a variant pack. find_format3_variants must return
    empty so the existing reject-with-message gate fires."""
    from cdumm.engine.import_handler import find_format3_variants

    src = tmp_path / "two_mods"
    _make_f3_json(src, "FastSwim.field.json", target="swim.pabgb",
                  intent_count=100)
    _make_f3_json(src, "FastClimb.field.json", target="climb.pabgb",
                  intent_count=200)

    variants = find_format3_variants(tmp_path)
    assert variants == [], (
        f"Different targets = not a variant pack; got {variants}")


def test_two_f3_same_target_no_common_prefix_not_variants(tmp_path):
    """Same target but no shared stem → still not variants. Mod
    authors might bundle two unrelated patches that happen to touch
    the same table."""
    from cdumm.engine.import_handler import find_format3_variants

    src = tmp_path / "bundle"
    _make_f3_json(src, "AlphaModForFoo.field.json",
                  target="iteminfo.pabgb", intent_count=50)
    _make_f3_json(src, "BetaModForFoo.field.json",
                  target="iteminfo.pabgb", intent_count=50)

    variants = find_format3_variants(tmp_path)
    assert variants == [], (
        f"No common stem prefix → not a variant pack; got {variants}")


def test_two_f3_wildly_different_intent_counts_not_variants(tmp_path):
    """Same prefix and target, but one mod has 10 intents and the
    other has 1000. Almost certainly not the same author shipping
    variants — probably an accidental bundle. Not variants."""
    from cdumm.engine.import_handler import find_format3_variants

    src = tmp_path / "uneven"
    _make_f3_json(src, "MyMod_lite.field.json", intent_count=10)
    _make_f3_json(src, "MyMod_full.field.json", intent_count=1000)

    variants = find_format3_variants(tmp_path)
    assert variants == [], (
        f"Intent ratio > 2x → not variants; got {variants}")


def test_single_f3_returns_empty(tmp_path):
    """One F3 JSON → not a variant pack (it's just one mod). The
    caller already imports a single F3 directly."""
    from cdumm.engine.import_handler import find_format3_variants

    _make_f3_json(tmp_path, "SoloMod.field.json")
    assert find_format3_variants(tmp_path) == []


def test_no_f3_json_returns_empty(tmp_path):
    """Tree with zero Format 3 JSONs → empty result."""
    from cdumm.engine.import_handler import find_format3_variants

    (tmp_path / "irrelevant.txt").write_text("hi")
    assert find_format3_variants(tmp_path) == []


def test_variant_dirs_each_contain_exactly_one_f3_json(tmp_path):
    """Each variant's _base_dir must contain exactly ONE Format 3
    JSON, so when the user picks a variant the F3 import path sees
    a clean single-mod tree."""
    from cdumm.engine.import_handler import find_format3_variants
    from cdumm.engine.json_patch_handler import is_natt_format_3

    src = tmp_path / "pack"
    for level in ("low", "med", "high"):
        _make_f3_json(src, f"Pack_{level}.field.json")

    variants = find_format3_variants(tmp_path)
    assert len(variants) == 3
    for v in variants:
        base = v["_base_dir"]
        f3_jsons = [p for p in base.rglob("*.json") if is_natt_format_3(p)]
        assert len(f3_jsons) == 1, (
            f"Variant {v['id']} must isolate one F3 JSON; "
            f"its base_dir contains {len(f3_jsons)}")


def test_variant_id_matches_per_variant_distinguishing_part(tmp_path):
    """The variant `id` must be the per-variant distinguishing piece
    (without the common stem) so the picker labels are useful:
      'CrimsonWings_10pct.field.json' → '10pct'
    Otherwise the user sees five identical 'CrimsonWings_*' rows."""
    from cdumm.engine.import_handler import find_format3_variants

    src = tmp_path / "p"
    for level in ("10pct", "25pct", "50pct", "75pct", "infinite"):
        _make_f3_json(src, f"CrimsonWings_{level}.field.json")

    variants = find_format3_variants(tmp_path)
    ids = sorted(v["id"] for v in variants)
    # The distinguishing parts. Strip the shared prefix and the
    # `.field.json` / `.json` suffix.
    expected = sorted(["10pct", "25pct", "50pct", "75pct", "infinite"])
    assert ids == expected, (
        f"Variant ids must isolate the distinguishing piece; "
        f"got {ids}, expected {expected}")
