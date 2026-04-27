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


def test_v2_byte_patch_with_changes_returns_true(tmp_path):
    """A v2 byte-patch mod with `patches[*].changes` IS configurable —
    each change becomes a per-row toggle. Regression guard."""
    mod_path = tmp_path / "v2_mod.json"
    mod_path.write_text(json.dumps({
        "patches": [
            {
                "label": "Stack 999",
                "changes": [
                    {"label": "Item A", "rel_offset": 0,
                     "original": "01", "patched": "ff"},
                    {"label": "Item B", "rel_offset": 4,
                     "original": "02", "patched": "ff"},
                ]
            }
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
