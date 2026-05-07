"""End-to-end integration tests for cdmods_path override (Task 3.2).

These tests assert that the refactor wiring actually causes call sites
to consult ``cdmods_path`` in the live ``Config`` object — not just
that ``get_cdmods_root`` returns the right value in isolation.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db(tmp_path):
    from cdumm.storage.database import Database

    d = Database(tmp_path / "test.db")
    d.initialize()
    return d


@pytest.fixture(autouse=True)
def _isolate_pointer_file(monkeypatch, tmp_path):
    """Sandbox the bootstrap pointer file (see test_cdmods_paths.py)."""
    from cdumm.engine import cdmods_paths
    monkeypatch.setattr(
        cdmods_paths, "_APP_DATA_DIR", tmp_path / "_appdata_isolate")


def test_swap_cache_cache_root_for_honors_override(db, tmp_path):
    """``swap_cache.cache_root_for`` must look at the configured override
    when one is provided, not the literal ``game_dir/CDMods``.

    This proves the refactor (Task 3.2) wired the helper through to
    the call site instead of just adding it to the toolbox.
    """
    from cdumm.engine.swap_cache import cache_root_for
    from cdumm.storage.config import Config

    override = tmp_path / "alt_storage"
    override.mkdir()
    cfg = Config(db)
    cfg.set("cdmods_path", str(override))

    game_dir = tmp_path / "fake_game_install"
    game_dir.mkdir()

    result = cache_root_for(game_dir, mod_id=42, config=cfg)
    expected = override / "sources" / "_swap_cache" / "42"
    assert result == expected

    # And without config: original behaviour preserved.
    fallback = cache_root_for(game_dir, mod_id=42, config=None)
    assert fallback == game_dir / "CDMods" / "sources" / "_swap_cache" / "42"


def test_mod_source_path_resolver_honors_override(db, tmp_path):
    """``resolve_mod_source_path`` must resolve the
    ``CDMods/sources/<id>/`` fallback against the configured override
    when one is set."""
    from cdumm.engine.mod_source_path import resolve_mod_source_path
    from cdumm.storage.config import Config

    override = tmp_path / "alt_storage"
    override.mkdir()
    cfg = Config(db)
    cfg.set("cdmods_path", str(override))

    # Create the fallback dir in the OVERRIDE location, not the default.
    fallback = override / "sources" / "7"
    fallback.mkdir(parents=True)

    game_dir = tmp_path / "fake_game_install"
    game_dir.mkdir()

    mod = {"id": 7, "source_path": None}
    result = resolve_mod_source_path(mod, game_dir, config=cfg)
    assert result == fallback


def test_invalidate_apply_fingerprint_honors_override(db, tmp_path):
    """``invalidate_apply_fingerprint`` must look for the fingerprint
    file under the configured override CDMods/, so a re-import after
    setting cdmods_path doesn't silently no-op against a stale path
    in game_dir/CDMods/."""
    from cdumm.engine.apply_engine import invalidate_apply_fingerprint
    from cdumm.storage.config import Config

    override = tmp_path / "alt_storage"
    override.mkdir()
    cfg = Config(db)
    cfg.set("cdmods_path", str(override))

    game_dir = tmp_path / "fake_game_install"
    game_dir.mkdir()

    fp_path = override / ".apply_fingerprint"
    fp_path.write_text("stub-fingerprint", encoding="utf-8")
    assert fp_path.exists()

    invalidate_apply_fingerprint(game_dir, config=cfg)
    assert not fp_path.exists(), (
        "Fingerprint should have been removed from the OVERRIDE "
        "location, not the default CDMods location"
    )

    # Sanity: a fingerprint at the DEFAULT location must NOT be removed
    # when an override is active.
    default_root = game_dir / "CDMods"
    default_root.mkdir(parents=True, exist_ok=True)
    default_fp = default_root / ".apply_fingerprint"
    default_fp.write_text("stub", encoding="utf-8")

    invalidate_apply_fingerprint(game_dir, config=cfg)
    assert default_fp.exists(), (
        "Apply fingerprint at the default game_dir/CDMods location "
        "should NOT be touched when an override is active"
    )
