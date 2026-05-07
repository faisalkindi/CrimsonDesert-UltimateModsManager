"""Tests for cdmods_paths.get_cdmods_root (Task 3.1)."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db(tmp_path):
    from cdumm.storage.database import Database
    d = Database(tmp_path / "test.db")
    d.initialize()
    return d


def test_falls_back_to_game_dir_when_no_config():
    from cdumm.engine.cdmods_paths import get_cdmods_root

    game_dir = Path("E:/SteamLibrary/Crimson Desert")
    assert get_cdmods_root(None, game_dir) == game_dir / "CDMods"


def test_falls_back_to_game_dir_when_config_missing_key(db):
    from cdumm.engine.cdmods_paths import get_cdmods_root
    from cdumm.storage.config import Config

    cfg = Config(db)
    game_dir = Path("E:/SteamLibrary/Crimson Desert")
    assert get_cdmods_root(cfg, game_dir) == game_dir / "CDMods"


def test_uses_config_override_when_set(db, tmp_path):
    from cdumm.engine.cdmods_paths import get_cdmods_root
    from cdumm.storage.config import Config

    override = tmp_path / "alternate_cdmods"
    override.mkdir()
    cfg = Config(db)
    cfg.set("cdmods_path", str(override))
    game_dir = Path("E:/SteamLibrary/Crimson Desert")
    assert get_cdmods_root(cfg, game_dir) == override


def test_falls_back_when_override_path_doesnt_exist(db, tmp_path):
    """A configured path that doesn't exist falls back to default
    rather than returning a broken path."""
    from cdumm.engine.cdmods_paths import get_cdmods_root
    from cdumm.storage.config import Config

    cfg = Config(db)
    cfg.set("cdmods_path", str(tmp_path / "nonexistent" / "doesnt_exist"))
    game_dir = Path("E:/SteamLibrary/Crimson Desert")
    assert get_cdmods_root(cfg, game_dir) == game_dir / "CDMods"


def test_falls_back_when_override_is_empty_string(db):
    """Empty string config value falls back."""
    from cdumm.engine.cdmods_paths import get_cdmods_root
    from cdumm.storage.config import Config

    cfg = Config(db)
    cfg.set("cdmods_path", "")
    game_dir = Path("E:/SteamLibrary/Crimson Desert")
    assert get_cdmods_root(cfg, game_dir) == game_dir / "CDMods"


def test_returns_path_object_not_string(db, tmp_path):
    from cdumm.engine.cdmods_paths import get_cdmods_root
    from cdumm.storage.config import Config

    override = tmp_path / "alternate_cdmods"
    override.mkdir()
    cfg = Config(db)
    cfg.set("cdmods_path", str(override))
    result = get_cdmods_root(cfg, Path("E:/anything"))
    assert isinstance(result, Path)
