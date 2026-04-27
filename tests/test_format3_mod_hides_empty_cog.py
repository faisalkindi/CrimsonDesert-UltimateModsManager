"""Format 3 mods must not show an empty config cog.

Bug from Faisal 2026-04-27: NoCooldownForALLItems imported
successfully (validator + apply path now work after the prefix-
fallback fix). The mod card shows a config cog (gear icon), but
clicking it opens an EMPTY config panel — the cog has nothing to
offer because Format 3 mods carry `intents`, not `patches`, and
the panel renderer at mods_page.py:1376 reads
``source_data.get("patches", [])`` which returns ``[]``.

The cog visibility logic at mods_page.py:660-663 auto-flags ANY
mod with a ``json_source`` as configurable. That was right for v2
byte-patch JSON mods (which DO have per-change toggles). It's
wrong for Format 3 mods.

Fix: detect Format 3 (file has ``"format": 3`` or ``"intents"``
key) and exclude from the auto-config-flag. The cog only appears
when the mod has REAL configurable content.
"""
from __future__ import annotations

import json
from pathlib import Path

from cdumm.gui.pages.mods_page import _json_source_has_configurable_content


def test_format3_json_source_returns_false(tmp_path):
    """A Format 3 mod (has `intents`, no `patches`) has nothing
    meaningful to configure → cog must not appear."""
    mod_path = tmp_path / "format3_mod.json"
    mod_path.write_text(json.dumps({
        "modinfo": {"name": "NoCooldownForALL"},
        "format": 3,
        "target": "iteminfo.pabgb",
        "intents": [
            {"entry": "X", "key": 1, "field": "cooltime",
             "op": "set", "new": 0}
        ]
    }))
    assert _json_source_has_configurable_content(str(mod_path)) is False, (
        "Format 3 mods carry `intents` not `patches`; the existing "
        "panel renderer at mods_page.py:1376 produces an empty toggle "
        "list. Cog must hide so users don't click into nothing.")


def test_v2_with_same_bracket_prefix_returns_false(tmp_path):
    """Bug from Faisal 2026-04-27 (round 2): Better Radial Menus,
    CD Inventory Expander, Even Faster Vanilla Animations Trimmer
    all have 2 changes that share ONE bracket prefix
    (`[Trust] ...`, `Character defaultSlot ...`, `[FASTER VANILLA]
    ...`). Those are TWO PARTS OF ONE FEATURE, not independent
    toggles. Cog must hide.

    Existing `preset_picker.has_labeled_changes` already encodes this
    rule. The cog visibility helper must defer to the same rule.
    """
    mod_path = tmp_path / "single_feature.json"
    mod_path.write_text(json.dumps({
        "patches": [
            {
                "game_file": "iteminfo.pabgb",
                "changes": [
                    {"label": "[Trust] Talk Gain 5 -> 50",
                     "offset": 0, "original": "05", "patched": "32"},
                    {"label": "[Trust] Other Talk Gain",
                     "offset": 4, "original": "06", "patched": "33"},
                ]
            }
        ]
    }))
    assert _json_source_has_configurable_content(str(mod_path)) is False, (
        "Two changes sharing one bracket prefix = parts of one "
        "feature, not configurable. Cog must hide.")


def test_v2_with_unlabeled_changes_returns_false(tmp_path):
    """A v2 mod with no labels (or all empty labels) has nothing
    meaningful to display — cog must hide."""
    mod_path = tmp_path / "unlabeled.json"
    mod_path.write_text(json.dumps({
        "patches": [
            {
                "game_file": "iteminfo.pabgb",
                "changes": [
                    {"offset": 0, "original": "01", "patched": "ff"},
                    {"offset": 4, "original": "02", "patched": "ff"},
                ]
            }
        ]
    }))
    assert _json_source_has_configurable_content(str(mod_path)) is False


def test_v2_single_patch_with_many_distinct_bracket_groups_returns_false(tmp_path):
    """Bug from Faisal 2026-04-27 round 3: Infinite Horse has 1 patch,
    24 changes, 15 distinct bracket prefixes (`[Horse]`, `[HorseRush]`,
    `[HorseSwim]`, ...). The existing `has_labeled_changes` returns
    True because of the 15 distinct prefixes, but the user views them
    as ONE FEATURE ('infinite horse'), not 15 independent toggles.

    The cog visibility helper must follow the same mental model the
    user expects: cog = explicit preset/variant/mutex CHOICE, not
    'this mod happens to have multiple labeled changes I could
    disable individually'.

    Single-patch mods don't qualify for the cog regardless of how
    many bracket prefixes they have. Multi-patch mods with distinct
    bracket-prefix groups still do (preset_groups pattern 1).
    """
    mod_path = tmp_path / "single_patch_many_groups.json"
    changes = [
        {"label": f"[{group}] change {i}", "offset": i * 4,
         "original": "00", "patched": "01"}
        for i, group in enumerate([
            "Horse", "HorseRush", "HorseSwim", "HorseFly",
            "HorseSit", "HorseDrift", "HorseKick", "HorseDouble"
        ])
    ]
    mod_path.write_text(json.dumps({
        "patches": [{"game_file": "skill.pabgb", "changes": changes}]
    }))
    assert _json_source_has_configurable_content(str(mod_path)) is False, (
        "Single-patch mod with N distinct bracket prefixes is one "
        "feature with N parts, not N independent toggles. "
        "Cog must hide.")


def test_v2_multi_patch_with_distinct_bracket_groups_returns_true(tmp_path):
    """Multi-patch mod where each patch has a different bracket-group
    prefix IS a real preset choice (preset_groups pattern 1). The
    user picks which patch(es) to apply. Regression guard."""
    mod_path = tmp_path / "multi_patch_groups.json"
    mod_path.write_text(json.dumps({
        "patches": [
            {
                "game_file": "iteminfo.pabgb",
                "changes": [
                    {"label": "[Easy] tier 1", "offset": 0,
                     "original": "01", "patched": "ff"},
                ]
            },
            {
                "game_file": "iteminfo.pabgb",
                "changes": [
                    {"label": "[Hard] tier 2", "offset": 8,
                     "original": "02", "patched": "ff"},
                ]
            },
        ]
    }))
    assert _json_source_has_configurable_content(str(mod_path)) is True


def test_v2_with_empty_patches_array_returns_false(tmp_path):
    """A JSON mod with ``patches: []`` has nothing to show — also no cog."""
    mod_path = tmp_path / "empty_v2.json"
    mod_path.write_text(json.dumps({"patches": []}))
    assert _json_source_has_configurable_content(str(mod_path)) is False


def test_missing_file_returns_false(tmp_path):
    """Defensive: missing json_source file must not crash; treat as
    no configurable content."""
    assert _json_source_has_configurable_content(
        str(tmp_path / "missing.json")) is False


def test_malformed_json_returns_false(tmp_path):
    """Defensive: malformed JSON must not crash."""
    mod_path = tmp_path / "bad.json"
    mod_path.write_text("{not valid json")
    assert _json_source_has_configurable_content(str(mod_path)) is False
