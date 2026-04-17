"""validate_game_directory accepts Xbox-style layouts without bin64/exe.

Tunsi82 (Xbox Game Pass) reported that CDUMM rejected his install
directory because ``bin64/CrimsonDesert.exe`` didn't exist under
``C:/XboxGames/Crimson Desert/Content/packages``. The PAZ files were
there — the exe just lives in a Microsoft Store registration outside
the game-data tree.

Validation now accepts either:
  (a) the Steam/Epic layout (bin64 exe present), OR
  (b) an Xbox-path install where the numbered PAZ directories exist
      (0008/0.paz + meta/0.papgt).
"""
from __future__ import annotations

from pathlib import Path

from cdumm.storage.game_finder import (
    _looks_like_game_root,
    is_xbox_install,
    validate_game_directory,
)


def _make_steam_root(tmp_path: Path) -> Path:
    game = tmp_path / "steamapps" / "common" / "Crimson Desert"
    (game / "bin64").mkdir(parents=True)
    (game / "bin64" / "CrimsonDesert.exe").write_bytes(b"stub")
    (game / "0008").mkdir()
    (game / "0008" / "0.paz").write_bytes(b"stub")
    (game / "meta").mkdir()
    (game / "meta" / "0.papgt").write_bytes(b"stub")
    return game


def _make_xbox_root(tmp_path: Path) -> Path:
    """Mimic Xbox: path contains 'XboxGames' but no bin64 exe."""
    game = tmp_path / "XboxGames" / "Crimson Desert" / "Content" / "packages"
    (game / "0008").mkdir(parents=True)
    (game / "0008" / "0.paz").write_bytes(b"stub")
    (game / "meta").mkdir()
    (game / "meta" / "0.papgt").write_bytes(b"stub")
    return game


# ── _looks_like_game_root ────────────────────────────────────────────


def test_looks_like_game_root_true_when_paz_and_papgt_present(tmp_path):
    game = _make_xbox_root(tmp_path)
    assert _looks_like_game_root(game) is True


def test_looks_like_game_root_false_when_paz_missing(tmp_path):
    game = tmp_path / "empty"
    game.mkdir()
    assert _looks_like_game_root(game) is False


def test_looks_like_game_root_false_when_only_paz_no_papgt(tmp_path):
    game = tmp_path / "half"
    (game / "0008").mkdir(parents=True)
    (game / "0008" / "0.paz").write_bytes(b"x")
    # no meta/0.papgt
    assert _looks_like_game_root(game) is False


# ── validate_game_directory ──────────────────────────────────────────


def test_steam_install_with_exe_is_valid(tmp_path):
    game = _make_steam_root(tmp_path)
    assert validate_game_directory(game) is True


def test_xbox_install_without_exe_but_with_paz_is_valid(tmp_path):
    game = _make_xbox_root(tmp_path)
    assert is_xbox_install(game)
    assert validate_game_directory(game) is True


def test_xbox_install_without_paz_or_exe_is_invalid(tmp_path):
    game = tmp_path / "XboxGames" / "Crimson Desert" / "Content" / "empty"
    game.mkdir(parents=True)
    assert is_xbox_install(game)
    assert validate_game_directory(game) is False


def test_non_xbox_path_without_exe_is_invalid_even_with_paz(tmp_path):
    # Generic path (no XboxGames / WindowsApps marker) without the exe
    # must NOT be accepted — could be a partial manual copy.
    game = tmp_path / "SomeOtherFolder"
    (game / "0008").mkdir(parents=True)
    (game / "0008" / "0.paz").write_bytes(b"x")
    (game / "meta").mkdir()
    (game / "meta" / "0.papgt").write_bytes(b"x")
    assert not is_xbox_install(game)
    assert validate_game_directory(game) is False
