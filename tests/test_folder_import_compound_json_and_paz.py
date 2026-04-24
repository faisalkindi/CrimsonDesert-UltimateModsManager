"""C1: folder-import must not short-circuit the PAZ-dir flow when a
folder has BOTH sibling .json patches AND NNNN game directories.

Issue #34 (kori228, Character Creator - Female and Male): a bodytype
preset folder shipped the mesh/texture data as NNNN PAZ dirs AND a
separate FemaleAnimations.json. CDUMM saw the JSON, imported only
the animations, and silently dropped the preset data. The user's
workaround was to physically move the JSON out, re-import the rest,
then re-import the JSON separately.

Fix: detect the compound case — folder has JSONs AND NNNN/0.paz
siblings — and route through the PAZ-dir flow, importing JSON
siblings via ``_import_sibling_json_patches`` afterwards.

This is a wiring guard. The fix is a routing decision in
import_from_folder, and the real test is a functional end-to-end
test of the Character Creator shape. We pin the guard here so the
routing doesn't regress; a functional test can be added separately.
"""
from __future__ import annotations

import re
from pathlib import Path


def _src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "engine" / "import_handler.py").read_text(
                encoding="utf-8")


def test_import_from_folder_detects_compound_layout():
    """Before the JSON-only branch, import_from_folder must check
    whether the folder ALSO contains NNNN/0.paz sibling dirs. If yes,
    skip the JSON-only short-circuit and let the PAZ-dir path handle
    the primary import."""
    src = _src()
    anchor = src.find("jp_list = detect_json_patches_all(folder_path)")
    assert anchor != -1, "JSON-patch branch anchor not found"
    # Scope: 2000 chars before the JSON branch — compound detection
    # must land here so we can skip the branch when needed.
    prelude = src[max(0, anchor - 2000):anchor + 200]
    # A comment or variable naming the compound case must be present.
    assert re.search(
        r"compound|has_paz_dirs|has_nnnn|_has_numbered_game_dirs",
        prelude, re.IGNORECASE), (
        "import_from_folder must detect compound (JSON + PAZ-dir) "
        "layouts before entering the JSON-only branch. Issue #34 "
        "(Character Creator - Female and Male) silently dropped the "
        "preset data because we short-circuited on JSON presence.")


def test_compound_branch_defers_json_siblings_to_helper():
    """When the compound case is detected, JSON patches must be
    imported via ``_import_sibling_json_patches`` AFTER the primary
    PAZ-dir import completes (same pattern CB-mode uses)."""
    src = _src()
    anchor = src.find("def import_from_folder(")
    assert anchor != -1
    body = src[anchor:anchor + 16000]
    # Must reference the sibling helper in the compound branch.
    sibling_call_count = body.count("_import_sibling_json_patches(")
    # Pre-C1 the file had ONE call (the CB branch). Post-C1 must
    # have TWO — the CB branch plus the new compound branch.
    assert sibling_call_count >= 2, (
        "compound-layout branch must call _import_sibling_json_patches "
        "to import the .json siblings as separate mods after the "
        "primary PAZ-dir import succeeds")


def test_single_json_only_folder_still_uses_json_branch():
    """Regression guard: a folder that's ONLY JSONs (no NNNN dirs)
    must still go through the existing JSON-only branch. The
    compound detection is additive, not a replacement."""
    src = _src()
    # The JSON-only import path must still exist unchanged.
    assert "jp_list = detect_json_patches_all(folder_path)" in src
    assert "if jp_list:" in src
    # And the early-return on primary_result must still be there
    # (that short-circuit is correct when there are no NNNN dirs).
    anchor = src.find("jp_list = detect_json_patches_all(folder_path)")
    assert anchor != -1
    branch = src[anchor:anchor + 3500]
    assert "return primary_result" in branch, (
        "JSON-only folders must still return early after importing "
        "their JSON patches")
