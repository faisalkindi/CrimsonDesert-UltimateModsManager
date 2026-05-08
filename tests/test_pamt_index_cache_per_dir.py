"""PAMT index disk cache must NOT collide between vanilla_dir and game_dir.

Bug 2026-05-08 (Democles85, GitHub #81): Importing Character Creator-837
fails with 'Target game file(s) not found: gamedata/characterinfo.pabgb'
even though the file exists in the live game directory at 0008/0.paz.

Root cause: _get_pamt_index(game_dir) and _get_pamt_index(vanilla_dir)
both write to and read from <cdmods>/.pamt_index.cache. Whichever runs
first wins. Subsequent calls for the other dir get entries whose paz_file
field points at the wrong directory, so os.path.exists() check fails on
the live PAZ that's actually present.

Fix: cache filename must include a stable identifier of the directory it
was built for, so vanilla and game caches don't overwrite each other.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class _FakeEntry:
    path: str
    paz_file: str
    offset: int = 0
    comp_size: int = 4
    orig_size: int = 4
    compression_type: int = 0
    flags: int = 0
    paz_index: int = 0
    encrypted: bool = False


def _setup_dirs_with_fake_pamt(tmp_path, monkeypatch):
    """Create a vanilla and game tree with a fake 0008/0.pamt.

    Both dirs have a 0.pamt file (so _get_pamt_index iterates them),
    but the actual PAMT parsing is monkey-patched to return entries
    that are CLEARLY from the dir we asked about, so the test can tell
    a leak.
    """
    from cdumm.engine import json_patch_handler as jph

    cdmods_root = tmp_path / "CDMods"
    vanilla_dir = cdmods_root / "vanilla"
    game_dir = tmp_path / "game"

    for d in (vanilla_dir / "0008", game_dir / "0008"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "0.pamt").write_bytes(b"\x00")
        (d / "0.paz").write_bytes(b"\x00")

    def fake_parse_pamt(pamt_path: str, paz_dir: str):
        return [_FakeEntry(
            path="gamedata/foo.pabgb",
            paz_file=str(Path(paz_dir) / "0.paz"),
        )]

    monkeypatch.setattr(jph, "parse_pamt", fake_parse_pamt)
    monkeypatch.setattr(
        jph, "get_cdmods_root",
        lambda config, gdir: cdmods_root)

    return cdmods_root, vanilla_dir, game_dir


def test_vanilla_then_game_indexes_do_not_collide(tmp_path, monkeypatch):
    from cdumm.engine import json_patch_handler as jph
    jph._pamt_index_cache.clear()

    cdmods_root, vanilla_dir, game_dir = _setup_dirs_with_fake_pamt(
        tmp_path, monkeypatch)

    v_idx = jph._get_pamt_index(vanilla_dir)
    assert Path(v_idx["gamedata/foo.pabgb"].paz_file).parent == \
        vanilla_dir / "0008"

    jph._pamt_index_cache.clear()

    g_idx = jph._get_pamt_index(game_dir)
    g_paz_parent = Path(g_idx["gamedata/foo.pabgb"].paz_file).parent
    assert g_paz_parent == game_dir / "0008", (
        f"PAMT index for game_dir leaked vanilla paths. "
        f"Expected entry under {game_dir / '0008'}, got {g_paz_parent}. "
        f"Cache files for vanilla and game collided."
    )


def test_game_then_vanilla_indexes_do_not_collide(tmp_path, monkeypatch):
    from cdumm.engine import json_patch_handler as jph
    jph._pamt_index_cache.clear()

    cdmods_root, vanilla_dir, game_dir = _setup_dirs_with_fake_pamt(
        tmp_path, monkeypatch)

    g_idx = jph._get_pamt_index(game_dir)
    assert Path(g_idx["gamedata/foo.pabgb"].paz_file).parent == \
        game_dir / "0008"

    jph._pamt_index_cache.clear()

    v_idx = jph._get_pamt_index(vanilla_dir)
    v_paz_parent = Path(v_idx["gamedata/foo.pabgb"].paz_file).parent
    assert v_paz_parent == vanilla_dir / "0008", (
        f"PAMT index for vanilla_dir leaked game paths. "
        f"Expected entry under {vanilla_dir / '0008'}, got {v_paz_parent}. "
        f"Cache files for vanilla and game collided."
    )


def test_two_different_game_installs_do_not_collide(tmp_path, monkeypatch):
    """Edge: a user has cdmods_path pointed somewhere shared by two
    Crimson Desert installs (e.g. testing). Different install paths
    must not collide either."""
    from cdumm.engine import json_patch_handler as jph
    jph._pamt_index_cache.clear()

    cdmods_root = tmp_path / "CDMods"
    install_a = tmp_path / "install_a"
    install_b = tmp_path / "install_b"
    for base in (install_a, install_b):
        d = base / "0008"
        d.mkdir(parents=True, exist_ok=True)
        (d / "0.pamt").write_bytes(b"\x00")
        (d / "0.paz").write_bytes(b"\x00")

    def fake_parse_pamt(pamt_path: str, paz_dir: str):
        return [_FakeEntry(
            path="gamedata/foo.pabgb",
            paz_file=str(Path(paz_dir) / "0.paz"),
        )]

    monkeypatch.setattr(jph, "parse_pamt", fake_parse_pamt)
    monkeypatch.setattr(
        jph, "get_cdmods_root",
        lambda config, gdir: cdmods_root)

    a_idx = jph._get_pamt_index(install_a)
    assert Path(a_idx["gamedata/foo.pabgb"].paz_file).parent == \
        install_a / "0008"

    jph._pamt_index_cache.clear()

    b_idx = jph._get_pamt_index(install_b)
    b_paz_parent = Path(b_idx["gamedata/foo.pabgb"].paz_file).parent
    assert b_paz_parent == install_b / "0008", (
        f"Two different game installs sharing a cdmods root collided "
        f"on the cache. Expected {install_b / '0008'}, got {b_paz_parent}."
    )
