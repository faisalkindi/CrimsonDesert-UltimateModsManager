"""GitHub #186 (lupo1190): the Steam launch-method 'Direct (-applaunch)'
option logged 'steam.exe not found (steam_root=None); falling back to
URI'. Their Steam lives at C:\\Games\\Steam (game at
C:\\Games\\Steam\\steamapps\\common\\Crimson Desert), but
_find_steam_root only probes <drive>:/Steam and <drive>:/SteamLibrary,
so a nested / custom install returns None and the applaunch fallback
never ran.

The game is always at <steam_root>/steamapps/common/Crimson Desert, so
steam.exe can be derived from game_dir directly (game_dir.parents[2] /
steam.exe) without guessing default paths. _resolve_steam_exe tries the
derived path first, then falls back to _find_steam_root.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.gui.fluent_window import _resolve_steam_exe


def _make_game_dir(tmp_path: Path, *, with_steam_exe: bool) -> Path:
    """Build <tmp>/Games/Steam/steamapps/common/Crimson Desert and
    optionally drop a steam.exe at the derived steam root."""
    steam_root = tmp_path / "Games" / "Steam"
    game_dir = steam_root / "steamapps" / "common" / "Crimson Desert"
    game_dir.mkdir(parents=True)
    if with_steam_exe:
        (steam_root / "steam.exe").write_bytes(b"MZ")  # stub
    return game_dir


def test_derives_steam_exe_from_game_dir(tmp_path: Path):
    """#186: steam.exe is found at game_dir.parents[2] even when
    _find_steam_root can't locate a custom install (returns None)."""
    game_dir = _make_game_dir(tmp_path, with_steam_exe=True)
    resolved = _resolve_steam_exe(game_dir, find_steam_root=lambda: None)
    assert resolved is not None
    assert resolved.name == "steam.exe"
    assert resolved.exists()
    # It must be the one next to the game's steamapps, not a guess.
    assert resolved.parent == tmp_path / "Games" / "Steam"


def test_falls_back_to_find_steam_root(tmp_path: Path):
    """When the derived path has no steam.exe but _find_steam_root
    locates one, use that."""
    game_dir = _make_game_dir(tmp_path, with_steam_exe=False)
    alt_root = tmp_path / "AltSteam"
    alt_root.mkdir()
    (alt_root / "steam.exe").write_bytes(b"MZ")
    resolved = _resolve_steam_exe(
        game_dir, find_steam_root=lambda: alt_root)
    assert resolved == alt_root / "steam.exe"


def test_returns_none_when_no_steam_exe_anywhere(tmp_path: Path):
    game_dir = _make_game_dir(tmp_path, with_steam_exe=False)
    resolved = _resolve_steam_exe(game_dir, find_steam_root=lambda: None)
    assert resolved is None


def test_derived_path_wins_over_find_steam_root(tmp_path: Path):
    """The game-relative derivation is authoritative — if both exist,
    use the one next to the actual game so we never launch the wrong
    Steam client on a multi-Steam machine."""
    game_dir = _make_game_dir(tmp_path, with_steam_exe=True)
    alt_root = tmp_path / "AltSteam"
    alt_root.mkdir()
    (alt_root / "steam.exe").write_bytes(b"MZ")
    resolved = _resolve_steam_exe(
        game_dir, find_steam_root=lambda: alt_root)
    assert resolved.parent == tmp_path / "Games" / "Steam"
