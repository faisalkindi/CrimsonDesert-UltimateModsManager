"""CRITICAL #1: persisted source_path must survive Windows %TEMP% cleanup.

The configurable_scanner stores source_path in mods.source_path and reads it
on every app launch. If the path lives under %TEMP%, Windows Storage Sense
(or a user cleaning Disk Cleanup / CCleaner) will eventually wipe it and the
cog/Configure button silently disappears on next launch.

The fix: clone under CDMods/sources/_swap_cache/<mod_id>/ instead of %TEMP%.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from cdumm.engine.swap_cache import (
    cache_root_for,
    resolve_cfg_src,
)


def _make_game_dir(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    (game / "CDMods" / "sources" / "1180").mkdir(parents=True)
    (game / "CDMods" / "sources" / "1180" / "variant_A").mkdir()
    (game / "CDMods" / "sources" / "1180" / "variant_A" / "patch.json").write_text("{}")
    (game / "CDMods" / "sources" / "1180" / "variant_B").mkdir()
    return game


def test_archive_source_path_returned_verbatim(tmp_path: Path) -> None:
    game = _make_game_dir(tmp_path)
    archive = tmp_path / "MyMod.rar"
    archive.write_bytes(b"rar!\x00\x00\x00\x00")
    cfg = resolve_cfg_src(
        source_path=str(archive),
        sources_dir=game / "CDMods" / "sources" / "1180",
        cache_root=cache_root_for(game, 1180),
    )
    assert cfg == str(archive), "archive sources must be used verbatim"


def test_dir_source_clones_under_permanent_cache_root(tmp_path: Path) -> None:
    """The clone lives under CDMods/_swap_cache, NOT under %TEMP%."""
    game = _make_game_dir(tmp_path)
    sources = game / "CDMods" / "sources" / "1180"
    cache_root = cache_root_for(game, 1180)

    cfg = resolve_cfg_src(
        source_path=None,
        sources_dir=sources,
        cache_root=cache_root,
    )
    assert cfg is not None, "a clone path should be returned when dir source used"
    cfg_path = Path(cfg).resolve()

    # Clone must live UNDER the caller's permanent cache_root (which
    # callers construct via cache_root_for(game_dir, mod_id) pointing at
    # CDMods/sources/_swap_cache/<mod_id>/). That directory is on the
    # game's data drive and is never touched by Windows TEMP cleanup.
    assert cfg_path == (cache_root / sources.name).resolve(), (
        f"clone must be cache_root/<name>; got {cfg_path}"
    )
    # Clone must contain the original contents.
    assert (cfg_path / "variant_A" / "patch.json").exists()
    assert (cfg_path / "variant_B").is_dir()


def test_cache_root_is_under_cdmods_sources(tmp_path: Path) -> None:
    game = tmp_path / "g"
    root = cache_root_for(game, 42)
    assert root == game / "CDMods" / "sources" / "_swap_cache" / "42"


def test_dir_source_overwrites_stale_cache(tmp_path: Path) -> None:
    """Second call for same mod replaces stale clone."""
    game = _make_game_dir(tmp_path)
    sources = game / "CDMods" / "sources" / "1180"
    cache_root = cache_root_for(game, 1180)

    resolve_cfg_src(None, sources, cache_root)
    # mutate sources between calls
    (sources / "variant_C").mkdir()
    cfg = resolve_cfg_src(None, sources, cache_root)
    assert (Path(cfg) / "variant_C").is_dir(), "second call must reflect latest sources"


def test_no_op_when_src_unchanged_between_calls(tmp_path: Path) -> None:
    """B2: second call with identical src must skip the copy entirely."""
    game = _make_game_dir(tmp_path)
    sources = game / "CDMods" / "sources" / "1180"
    cache_root = cache_root_for(game, 1180)

    # First clone
    resolve_cfg_src(None, sources, cache_root)
    clone_dir = cache_root / sources.name
    # Snapshot mtime of a file inside the clone
    probe = clone_dir / "variant_A" / "patch.json"
    first_mtime = probe.stat().st_mtime_ns

    # Second call WITHOUT any src changes — the manifest matches,
    # so no recopy should happen. Probe mtime stays the same.
    import time
    time.sleep(0.05)   # ensure FS mtime resolution would register changes
    resolve_cfg_src(None, sources, cache_root)
    second_mtime = probe.stat().st_mtime_ns
    assert first_mtime == second_mtime, (
        "unchanged src should not trigger a re-copy (would update mtime)")


def test_missing_source_path_returns_none(tmp_path: Path) -> None:
    game = tmp_path / "g"
    (game / "CDMods").mkdir(parents=True)
    cfg = resolve_cfg_src(
        source_path=None,
        sources_dir=None,
        cache_root=cache_root_for(game, 99),
    )
    assert cfg is None
