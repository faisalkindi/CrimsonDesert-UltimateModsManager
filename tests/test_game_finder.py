from pathlib import Path

from cdumm.storage.game_finder import validate_game_directory, _parse_library_folders


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


def test_config_stores_game_directory(db) -> None:
    from cdumm.storage.config import Config
    config = Config(db)
    config.set("game_directory", "E:/SteamLibrary/steamapps/common/Crimson Desert")
    assert config.get("game_directory") == "E:/SteamLibrary/steamapps/common/Crimson Desert"
