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


@pytest.fixture(autouse=True)
def _isolate_pointer_file(monkeypatch, tmp_path):
    """Sandbox the bootstrap pointer file so any real pointer in
    %LOCALAPPDATA%/cdumm/cdmods_path.txt (left over from a real CDUMM
    install on the test host) cannot bleed into tests that exercise
    get_cdmods_root(None, ...). Tests that explicitly want to drive
    pointer behavior re-monkeypatch the same attribute themselves."""
    from cdumm.engine import cdmods_paths
    monkeypatch.setattr(
        cdmods_paths, "_APP_DATA_DIR", tmp_path / "_appdata_isolate")


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


# ---------------------------------------------------------------------------
# Pointer file (C1): bootstrap fallback when no DB is open yet
# ---------------------------------------------------------------------------


def test_pointer_file_round_trip(tmp_path, monkeypatch):
    """write_cdmods_path_pointer + read_cdmods_path_pointer round-trip
    through %LOCALAPPDATA%/cdumm/cdmods_path.txt."""
    from cdumm.engine import cdmods_paths

    fake_appdata = tmp_path / "appdata"
    monkeypatch.setattr(cdmods_paths, "_APP_DATA_DIR", fake_appdata)

    target = tmp_path / "alt_cdmods"
    target.mkdir()
    cdmods_paths.write_cdmods_path_pointer(target)

    # File created at expected location
    pointer_file = fake_appdata / "cdmods_path.txt"
    assert pointer_file.exists()

    # Read returns the same path
    assert cdmods_paths.read_cdmods_path_pointer() == target


def test_pointer_file_missing_returns_none(tmp_path, monkeypatch):
    from cdumm.engine import cdmods_paths

    fake_appdata = tmp_path / "appdata"
    monkeypatch.setattr(cdmods_paths, "_APP_DATA_DIR", fake_appdata)

    assert cdmods_paths.read_cdmods_path_pointer() is None


def test_pointer_file_with_nonexistent_path_returns_none(tmp_path, monkeypatch):
    """Stale pointer (path no longer exists) returns None so callers
    fall back to the default rather than blindly trusting it."""
    from cdumm.engine import cdmods_paths

    fake_appdata = tmp_path / "appdata"
    fake_appdata.mkdir()
    monkeypatch.setattr(cdmods_paths, "_APP_DATA_DIR", fake_appdata)

    (fake_appdata / "cdmods_path.txt").write_text(
        str(tmp_path / "no_such_dir"), encoding="utf-8")

    assert cdmods_paths.read_cdmods_path_pointer() is None


def test_pointer_file_unreadable_returns_none(tmp_path, monkeypatch):
    """Garbage in the pointer file falls back gracefully — never crash
    bootstrap."""
    from cdumm.engine import cdmods_paths

    fake_appdata = tmp_path / "appdata"
    fake_appdata.mkdir()
    monkeypatch.setattr(cdmods_paths, "_APP_DATA_DIR", fake_appdata)

    (fake_appdata / "cdmods_path.txt").write_text("", encoding="utf-8")
    assert cdmods_paths.read_cdmods_path_pointer() is None


def test_get_cdmods_root_uses_pointer_when_no_config(tmp_path, monkeypatch):
    """When config is None (bootstrap) and a valid pointer file exists,
    get_cdmods_root must honor the pointer, NOT fall straight to the
    game_dir/CDMods default. Without this, post-migration launches
    create an empty CDMods/ at the default and the user's library
    appears wiped."""
    from cdumm.engine import cdmods_paths

    fake_appdata = tmp_path / "appdata"
    monkeypatch.setattr(cdmods_paths, "_APP_DATA_DIR", fake_appdata)

    override = tmp_path / "real_cdmods"
    override.mkdir()
    cdmods_paths.write_cdmods_path_pointer(override)

    game_dir = tmp_path / "fake_game"
    game_dir.mkdir()
    assert cdmods_paths.get_cdmods_root(None, game_dir) == override


def test_get_cdmods_root_pointer_falls_back_when_stale(
        tmp_path, monkeypatch):
    """Stale pointer file (target no longer exists) must NOT cause
    get_cdmods_root to return a broken path; falls back to default."""
    from cdumm.engine import cdmods_paths

    fake_appdata = tmp_path / "appdata"
    fake_appdata.mkdir()
    monkeypatch.setattr(cdmods_paths, "_APP_DATA_DIR", fake_appdata)

    (fake_appdata / "cdmods_path.txt").write_text(
        str(tmp_path / "vanished"), encoding="utf-8")

    game_dir = tmp_path / "fake_game"
    game_dir.mkdir()
    assert (cdmods_paths.get_cdmods_root(None, game_dir)
            == game_dir / "CDMods")


def test_get_cdmods_root_config_takes_priority_over_pointer(
        db, tmp_path, monkeypatch):
    """When BOTH a config and a pointer exist, the config wins (the
    pointer is only the fallback for bootstrap before the DB opens)."""
    from cdumm.engine import cdmods_paths
    from cdumm.storage.config import Config

    fake_appdata = tmp_path / "appdata"
    monkeypatch.setattr(cdmods_paths, "_APP_DATA_DIR", fake_appdata)

    pointer_target = tmp_path / "pointer_target"
    pointer_target.mkdir()
    cdmods_paths.write_cdmods_path_pointer(pointer_target)

    config_target = tmp_path / "config_target"
    config_target.mkdir()
    cfg = Config(db)
    cfg.set("cdmods_path", str(config_target))

    game_dir = tmp_path / "fake_game"
    game_dir.mkdir()
    # Config wins
    assert cdmods_paths.get_cdmods_root(cfg, game_dir) == config_target
