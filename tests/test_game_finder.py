from pathlib import Path

from cdumm.storage.game_finder import (
    validate_game_directory, _parse_library_folders,
    _scan_for_steam_libraries,
)


def test_validate_game_directory_valid(tmp_path: Path) -> None:
    game_dir = tmp_path / "Crimson Desert"
    (game_dir / "bin64").mkdir(parents=True)
    (game_dir / "bin64" / "CrimsonDesert.exe").touch()
    assert validate_game_directory(game_dir) is True


def test_validate_game_directory_invalid(tmp_path: Path) -> None:
    game_dir = tmp_path / "NotAGame"
    game_dir.mkdir()
    assert validate_game_directory(game_dir) is False


def test_validate_game_directory_nonexistent() -> None:
    assert validate_game_directory(Path("/does/not/exist")) is False


def test_parse_library_folders_vdf(tmp_path: Path) -> None:
    vdf = tmp_path / "libraryfolders.vdf"
    vdf.write_text('''
"libraryfolders"
{
    "0"
    {
        "path"		"C:\\\\Program Files (x86)\\\\Steam"
    }
    "1"
    {
        "path"		"E:\\\\SteamLibrary"
    }
}
''', encoding="utf-8")
    paths = _parse_library_folders(vdf)
    assert len(paths) == 2
    assert Path("C:/Program Files (x86)/Steam") in paths
    assert Path("E:/SteamLibrary") in paths


def test_parse_library_folders_missing_file(tmp_path: Path) -> None:
    paths = _parse_library_folders(tmp_path / "nonexistent.vdf")
    assert paths == []


# ── Direct-scan fallback (issue #43 — Feikaz) ────────────────────────


def test_direct_scan_finds_game_in_secondary_steam_library(
        tmp_path: Path) -> None:
    """Issue #43 (Feikaz, 2026-04-25): user has Steam at
    F:/Steam/steamapps/common/Crimson Desert. The primary Steam install
    on C: doesn't list F:/ in its libraryfolders.vdf (Steam was never
    told about that library), so VDF-based detection misses the game.
    The direct drive scan must catch it independently."""
    fake_lib = tmp_path / "FakeFDrive" / "Steam"
    game_dir = (fake_lib / "steamapps" / "common"
                / "Crimson Desert" / "bin64")
    game_dir.mkdir(parents=True)
    (game_dir / "CrimsonDesert.exe").write_bytes(b"EXE")

    found = _scan_for_steam_libraries([fake_lib])
    assert len(found) == 1
    assert found[0] == (fake_lib / "steamapps" / "common"
                        / "Crimson Desert")


def test_direct_scan_skips_libraries_without_game(tmp_path: Path) -> None:
    """Don't surface every empty Steam library on disk — only
    the ones that actually contain Crimson Desert."""
    empty_lib = tmp_path / "EmptyLib"
    (empty_lib / "steamapps" / "common").mkdir(parents=True)
    found = _scan_for_steam_libraries([empty_lib])
    assert found == []


def test_direct_scan_handles_missing_base_dirs(tmp_path: Path) -> None:
    """Most drive letters don't have a Steam folder. The scan must
    silently skip non-existent base paths instead of raising."""
    missing = tmp_path / "DoesNotExist"
    found = _scan_for_steam_libraries([missing])
    assert found == []


def test_direct_scan_returns_multiple_libraries(tmp_path: Path) -> None:
    """User with Steam libraries on multiple drives gets all hits;
    the caller dedups by resolved path."""
    lib_a = tmp_path / "DriveA" / "SteamLibrary"
    lib_b = tmp_path / "DriveB" / "Steam"
    for base in (lib_a, lib_b):
        gd = base / "steamapps" / "common" / "Crimson Desert" / "bin64"
        gd.mkdir(parents=True)
        (gd / "CrimsonDesert.exe").write_bytes(b"EXE")

    found = _scan_for_steam_libraries([lib_a, lib_b])
    assert len(found) == 2


def test_config_stores_game_directory(db) -> None:
    from cdumm.storage.config import Config
    config = Config(db)
    config.set("game_directory", "E:/SteamLibrary/steamapps/common/Crimson Desert")
    assert config.get("game_directory") == "E:/SteamLibrary/steamapps/common/Crimson Desert"
