"""Audit finding C6 (2026-06-10): the incremental overlay rebuild's
cache-hit branch `continue`d past the PABGH auto-include. Apply 1
built the .pabgb + companion .pabgh correctly; Apply 2 with the same
unchanged mod got a cache hit on the .pabgb and shipped an overlay
with NO .pabgh at all, so the game fell back to the vanilla index
whose offsets are stale exactly when the table structure changed.
The mod silently broke on every apply after the first.
"""
from __future__ import annotations

from pathlib import Path

import cdumm.archive.overlay_builder as ob


_PABGH_BYTES = b"\x01\x00" + b"\x10\x27\x00\x00\x00\x00\x00\x00"


def _entries():
    metadata = {
        "entry_path": "gamedata/iteminfo.pabgb",
        "pamt_dir": "0008",
        "compression_type": 2,
        "encrypted": False,
        "delta_hash": "stable-hash-1",
    }
    return [(b"PABGB-TABLE-BYTES" * 64, metadata)]


def _build(monkeypatch, tmp_path: Path, preloaded_cache):
    monkeypatch.setattr(
        ob, "_get_vanilla_pabgh",
        lambda pamt_dir, entry_path, game_dir: _PABGH_BYTES)
    return ob.build_overlay(
        _entries(),
        game_dir=tmp_path,  # truthy so the auto-include runs
        preloaded_cache=preloaded_cache,
        vanilla_pathc_path=None,
    )


def _names(overlay_entries):
    return [e.filename for e in overlay_entries]


def test_first_build_includes_pabgh(monkeypatch, tmp_path: Path):
    _, _, entries1 = _build(monkeypatch, tmp_path, preloaded_cache=None)
    assert "iteminfo.pabgb" in _names(entries1)
    assert "iteminfo.pabgh" in _names(entries1), (
        "fresh build lost the companion index")


def test_cache_hit_build_still_includes_pabgh(monkeypatch, tmp_path: Path):
    """Apply 2 (cache hit on the unchanged .pabgb) must still carry
    the companion .pabgh."""
    paz1, _, entries1 = _build(monkeypatch, tmp_path, preloaded_cache=None)

    # Synthesize the cache the second build would load: manifest keyed
    # by entry_path with the segment geometry of the first build.
    pabgb1 = next(e for e in entries1 if e.filename == "iteminfo.pabgb")
    manifest = {
        "gamedata/iteminfo.pabgb": {
            "offset": pabgb1.paz_offset,
            "comp_size": pabgb1.comp_size,
            "decomp_size": pabgb1.decomp_size,
            "flags": pabgb1.flags,
            "delta_hash": "stable-hash-1",
        }
    }
    preloaded = (manifest, paz1, None)

    _, _, entries2 = _build(monkeypatch, tmp_path, preloaded_cache=preloaded)
    assert "iteminfo.pabgb" in _names(entries2)
    assert "iteminfo.pabgh" in _names(entries2), (
        "cache-hit rebuild dropped the companion .pabgh "
        "(audit C6 regression)")
