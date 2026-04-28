"""Format 3 mods target by basename — vanilla extraction must
resolve to the full PAMT path.

Bug from Matrixz on Nexus 2026-04-28: a CrimsonGameMods v3 export
targeting `iteminfo.pabgb` failed apply with
"Format 3 mod 'Buffs' produced 0 byte changes: could not extract
vanilla bytes for 'iteminfo.pabgb'". Bug report attached
confirmed game files are intact (208/208 vanilla matches), CDUMM
sees the game directory, snapshot is fresh.

Root cause traced by reading the apply_engine.py code path:

  1. ``_vanilla_extractor`` at line 241 calls
     ``_find_pamt_entry(target, game_dir)`` with target =
     ``"iteminfo.pabgb"``.
  2. ``_find_pamt_entry`` does both exact match AND basename
     match (json_patch_handler.py:1456-1465), so the basename
     ``"iteminfo.pabgb"`` resolves to the PAMT entry whose actual
     path is ``"gamedata/iteminfo.pabgb"``. Returns the entry.
  3. ``_vanilla_extractor`` then calls
     ``get_vanilla_entry_content(file_path, target)`` with
     ``target = "iteminfo.pabgb"``.
  4. ``_get_vanilla_entry_content`` at apply_engine.py:2698
     iterates PAMT entries comparing ``e.path == entry_path``
     where ``entry_path = "iteminfo.pabgb"``. The actual PAMT
     entries have ``e.path = "gamedata/iteminfo.pabgb"``. Exact
     match fails for every entry → returns None.
  5. ``_vanilla_extractor`` returns None.
  6. Format 3 apply: "vanilla extraction failed".

Same shape failure at ``_extract_sibling_entry`` (line 2516)
when the apply tries to read the sibling ``iteminfo.pabgh``
header.

Fix: ``_vanilla_extractor`` already has the resolved entry from
``_find_pamt_entry`` step. Pass ``entry.path`` (the full
"gamedata/..." path) to ``get_vanilla_entry_content`` and use
the same prefix when computing the sibling ``.pabgh`` path.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest


def _make_minimal_pamt_pair(tmp_path: Path) -> Path:
    """Build a tiny game-dir-shape with one 0008/ subfolder containing
    a PAMT entry at path 'gamedata/iteminfo.pabgb'. Returns the
    game_dir root."""
    # We don't need a real working PAZ. The bug is purely in the path
    # comparison logic — we just need a PAMT that resolves
    # 'iteminfo.pabgb' (basename) to 'gamedata/iteminfo.pabgb' (full).
    # Skip if our test PAMT writer isn't available.
    pytest.importorskip("cdumm.archive.paz_parse")
    return tmp_path  # placeholder — real test below uses live game dir


def test_get_vanilla_entry_content_basename_match_falls_back():
    """Direct unit test of the inner helper. When a Format 3 mod
    targets a file by basename, ``_get_vanilla_entry_content`` must
    accept either the exact PAMT path OR the basename, mirroring
    ``_find_pamt_entry``'s behavior."""
    # Run against the live vanilla install if present; skip otherwise.
    game_dir = Path("E:/SteamLibrary/steamapps/common/Crimson Desert")
    pamt = game_dir / "0008" / "0.pamt"
    if not pamt.exists():
        pytest.skip(
            f"Live game dir not available at {game_dir} — "
            "this test verifies the resolver against real PAMT bytes.")

    from cdumm.engine.apply_engine import ApplyWorker
    # Minimal stub: only need _vanilla_dir and _game_dir for the helper.
    worker = ApplyWorker.__new__(ApplyWorker)
    worker._vanilla_dir = game_dir / "CDMods" / "vanilla"  # may not exist
    worker._game_dir = game_dir

    # The actual PAMT entry path is "gamedata/iteminfo.pabgb".
    # When called with a basename, this MUST still resolve.
    body = worker._get_vanilla_entry_content(
        "0008/0.paz", "iteminfo.pabgb")
    assert body is not None and len(body) > 0, (
        "_get_vanilla_entry_content called with the bare basename "
        "'iteminfo.pabgb' must still find the PAMT entry whose "
        "stored path is 'gamedata/iteminfo.pabgb'. Currently it "
        "uses exact match only, so Format 3 mods that target by "
        "basename can't extract vanilla bytes.")


def test_extract_sibling_entry_basename_match_falls_back():
    """Same shape failure at ``_extract_sibling_entry``: must accept
    'iteminfo.pabgh' even when PAMT stores the full
    'gamedata/iteminfo.pabgh' path."""
    game_dir = Path("E:/SteamLibrary/steamapps/common/Crimson Desert")
    pamt = game_dir / "0008" / "0.pamt"
    if not pamt.exists():
        pytest.skip(f"Live game dir not available at {game_dir}")

    from cdumm.engine.apply_engine import ApplyWorker
    worker = ApplyWorker.__new__(ApplyWorker)
    worker._vanilla_dir = game_dir / "CDMods" / "vanilla"
    worker._game_dir = game_dir

    body = worker._extract_sibling_entry("0008", "iteminfo.pabgh")
    assert body is not None and len(body) > 0, (
        "_extract_sibling_entry called with bare basename "
        "'iteminfo.pabgh' must resolve to the PAMT entry whose "
        "stored path is 'gamedata/iteminfo.pabgh'. Format 3 mods "
        "compute the sibling header path from `target` and hit "
        "this path.")


def test_full_extraction_for_format3_target_succeeds():
    """End-to-end: the apply_engine's _vanilla_extractor closure
    (built inside _expand_format3_into_synth_data) must return a
    non-None (body, header) tuple for target='iteminfo.pabgb'."""
    game_dir = Path("E:/SteamLibrary/steamapps/common/Crimson Desert")
    if not (game_dir / "0008" / "0.pamt").exists():
        pytest.skip(f"Live game dir not available at {game_dir}")

    from cdumm.engine.apply_engine import ApplyWorker
    worker = ApplyWorker.__new__(ApplyWorker)
    worker._vanilla_dir = game_dir / "CDMods" / "vanilla"
    worker._game_dir = game_dir

    # Build the same closure shape _expand_format3_into_synth_data
    # creates (apply_engine.py:241).
    from cdumm.engine.json_patch_handler import _find_pamt_entry
    from cdumm.engine.apply_engine import _expand_format3_into_synth_data

    # Reach into the helper by inlining the same closure logic.
    def _vanilla_extractor(target):
        from cdumm.engine.json_patch_handler import _derive_pamt_dir
        entry = _find_pamt_entry(target, worker._vanilla_dir)
        if entry is None:
            entry = _find_pamt_entry(target, worker._game_dir)
        if entry is None:
            return None
        pamt_dir = _derive_pamt_dir(entry.paz_file)
        if not pamt_dir:
            return None
        file_path = f"{pamt_dir}/{Path(entry.paz_file).name}"
        body = worker._get_vanilla_entry_content(file_path, target)
        if body is None:
            return None
        header_path = target
        if header_path.endswith(".pabgb"):
            header_path = header_path[:-len(".pabgb")] + ".pabgh"
        header = worker._extract_sibling_entry(pamt_dir, header_path)
        if header is None:
            return None
        return body, header

    result = _vanilla_extractor("iteminfo.pabgb")
    assert result is not None, (
        "Format 3 vanilla extraction for target='iteminfo.pabgb' "
        "must succeed. This is the exact failure Matrixz reported "
        "in his bug report on Nexus 2026-04-28.")
    body, header = result
    assert len(body) > 0
    assert len(header) > 0
