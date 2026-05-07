"""Regression: PAMT index cache must land at the cdmods_path override
location, not stray into game_dir/ when override is active (I1).

Before the fix, ``json_patch_handler._get_pamt_index`` called
``get_cdmods_root(None, game_dir)`` which returned ``game_dir/CDMods``
when no pointer / config was visible. The follow-up fallback
(``game_dir.parent / 'CDMods' if name == 'vanilla' else game_dir``)
then wrote ``.pamt_index.cache`` to ``game_dir/`` when called with
the real game_dir , leaking one cache file per rebuild into the
game install root.

After the fix:

  * Real game_dir + pointer file set: cache lands at the override.
  * vanilla_dir input: cache lands at vanilla_dir.parent (= the
    CDMods root, override-aware automatically).
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_pamt_cache_uses_pointer_for_real_game_dir(tmp_path, monkeypatch):
    """When called with the real game_dir and the pointer file points
    at an override location, the cache must land at the override , NOT
    in game_dir/ or game_dir/CDMods/."""
    from cdumm.engine import cdmods_paths
    from cdumm.engine.json_patch_handler import _get_pamt_index

    monkeypatch.setattr(
        cdmods_paths, "_APP_DATA_DIR", tmp_path / "appdata")

    # Set pointer to the override
    override = tmp_path / "alt_cdmods"
    override.mkdir()
    cdmods_paths.write_cdmods_path_pointer(override)

    # Real game dir with no PAMTs (so the index ends up empty, but
    # the cache file is still written , that's what we're checking).
    game_dir = tmp_path / "fake_game"
    game_dir.mkdir()

    _get_pamt_index(game_dir)

    expected = override / ".pamt_index.cache"
    assert expected.exists(), (
        "cache file should land at the override location when "
        "cdmods_path pointer is set; instead saw nothing at %s"
        % expected)

    # Cache must NOT have been dropped at the default location.
    bad_default = game_dir / "CDMods" / ".pamt_index.cache"
    assert not bad_default.exists(), (
        "cache leaked into default game_dir/CDMods location even "
        "though override is active")
    bad_root = game_dir / ".pamt_index.cache"
    assert not bad_root.exists(), (
        "cache leaked into game_dir root , this was the I1 bug "
        "from the stale-CDMods-literal fallback")


def test_pamt_cache_uses_parent_when_called_with_vanilla(
        tmp_path, monkeypatch):
    """When called with vanilla_dir (= <cdmods>/vanilla), the cache
    must land at the parent (= the CDMods root)."""
    from cdumm.engine import cdmods_paths
    from cdumm.engine.json_patch_handler import _get_pamt_index

    monkeypatch.setattr(
        cdmods_paths, "_APP_DATA_DIR", tmp_path / "appdata")

    cdmods_root = tmp_path / "alt_cdmods"
    vanilla_dir = cdmods_root / "vanilla"
    vanilla_dir.mkdir(parents=True)

    _get_pamt_index(vanilla_dir)

    expected = cdmods_root / ".pamt_index.cache"
    assert expected.exists()
    # Must NOT be inside vanilla_dir itself.
    assert not (vanilla_dir / ".pamt_index.cache").exists()
