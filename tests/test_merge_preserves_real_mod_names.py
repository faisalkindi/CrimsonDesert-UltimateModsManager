"""When semantic-merge or byte-merge resolves a conflict between two
mods on the same entry_path, the resulting merged delta must NOT
overwrite mod_name with a synthetic label like "semantic_merge" or
"byte_merge". Doing so leaks the synthetic label into downstream
size-merge warnings via the v3.2.7 mod_name propagation fix
(apply_engine.py:2253-2262), so users see "Active: 'byte_merge'"
instead of the real winning mod name.

This regresses the DerBambusbjoern fix from v3.2.7. Internal merge-
type tracking should use the `_semantic_merged` / `_byte_merged`
boolean flags (already present), not the user-facing `mod_name`
field. The composed mod_name should reflect the real contributing
mod names so warnings remain informative.
"""
from __future__ import annotations
import pytest


def test_byte_merge_does_not_set_synthetic_mod_name():
    """Direct check: when the byte-merge fallback emits a merged delta,
    the resulting dict's `mod_name` field must NOT equal the literal
    'byte_merge' string. It should reflect the real contributing
    mods (e.g. 'Mod A + Mod B' or 'merge of N mods')."""
    import re
    src_path = (
        "C:/Users/faisa/Ai/Mods Dev/CrimsonDesert-Mods/"
        "CrimsonDesert-ModManager/src/cdumm/engine/apply_engine.py"
    )
    with open(src_path, "r", encoding="utf-8") as f:
        text = f.read()
    # Search for the literal assignment patterns that bake in synthetic
    # labels. These fail the test if found.
    forbidden = [
        r'merged_d\[\s*"mod_name"\s*\]\s*=\s*"byte_merge"',
        r'merged_d\[\s*"mod_name"\s*\]\s*=\s*"semantic_merge"',
    ]
    found = []
    for pat in forbidden:
        if re.search(pat, text):
            found.append(pat)
    assert not found, (
        f"apply_engine.py contains synthetic mod_name assignments that "
        f"leak into user-facing warnings: {found!r}. Use "
        f"`merged_d['_byte_merged'] = True` (or `_semantic_merged`) "
        f"to track merge type internally, and compose `mod_name` from "
        f"the real contributing mod names so the size-merge warning "
        f"shows actual names instead of placeholders."
    )


def test_compose_merged_mod_name_dedupes_and_caps():
    from cdumm.engine.apply_engine import _compose_merged_mod_name

    # Single contributor
    assert _compose_merged_mod_name(["Mod A"], "byte merge") == "Mod A"

    # Two contributors joined with `+`
    assert (
        _compose_merged_mod_name(["Mod A", "Mod B"], "byte merge")
        == "Mod A + Mod B"
    )

    # Three contributors all visible
    assert (
        _compose_merged_mod_name(
            ["Mod A", "Mod B", "Mod C"], "semantic merge"
        )
        == "Mod A + Mod B + Mod C"
    )

    # Four contributors collapse to 3 + count
    out = _compose_merged_mod_name(
        ["Mod A", "Mod B", "Mod C", "Mod D"], "semantic merge"
    )
    assert out == "Mod A + Mod B + Mod C + 1 more"

    # Dedupe + skip unknown
    out = _compose_merged_mod_name(
        ["Mod A", "Mod A", "unknown", "", "Mod B"], "byte merge"
    )
    assert out == "Mod A + Mod B"

    # All-unknown / empty falls back to a labeled placeholder
    out = _compose_merged_mod_name(["unknown", "", None], "byte merge")
    assert "byte merge" in out
    assert "unidentified" in out
