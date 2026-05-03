"""GitHub #63 (AwkwardOrpheus, 2026-05-02): add a CLI flag that
auto-applies enabled mods and launches Crimson Desert in one shot,
so users on handheld devices (Steam Deck, ROG Ally) can register
CDUMM as a non-Steam launcher and press Play once.

The launch logic itself lives in fluent_window._on_launch_game
today (steam:// URI, Xbox shell URI, or direct exe). Extract it
to a pure function in engine/launcher.py so the CLI subcommand
can reuse it without importing Qt.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def _make_fake_game_dir(tmp_path: Path) -> Path:
    game_dir = tmp_path / "game"
    bin64 = game_dir / "bin64"
    bin64.mkdir(parents=True)
    (bin64 / "CrimsonDesert.exe").write_bytes(b"\x4d\x5a")  # MZ header
    return game_dir


def test_launch_game_uses_steam_uri_for_steam_install(tmp_path: Path,
                                                     monkeypatch):
    """When the install is detected as Steam, launch_game must invoke
    the steam:// URI handler (os.startfile on Windows, equivalent
    elsewhere)."""
    game_dir = _make_fake_game_dir(tmp_path)

    from cdumm.engine import launcher
    import cdumm.storage.game_finder as gf
    import cdumm.engine.game_monitor as gm

    monkeypatch.setattr(gf, "is_steam_install", lambda d: True)
    monkeypatch.setattr(gf, "is_xbox_install", lambda d: False)
    monkeypatch.setattr(gm, "get_steam_app_id", lambda d: 3321460)

    calls: list[str] = []
    monkeypatch.setattr(
        launcher, "_open_uri",
        lambda uri: calls.append(uri))
    monkeypatch.setattr(
        launcher, "_run_exe",
        lambda exe, cwd: calls.append(f"EXE:{exe}"))

    launcher.launch_game(game_dir)

    assert any("steam://rungameid/3321460" in c for c in calls), (
        f"Expected steam URI launch, got {calls!r}")


def test_launch_game_uses_direct_exe_when_no_steam_or_xbox(tmp_path: Path,
                                                          monkeypatch):
    """When neither Steam nor Xbox detection matches, fall back to
    direct exe launch."""
    game_dir = _make_fake_game_dir(tmp_path)

    from cdumm.engine import launcher
    import cdumm.storage.game_finder as gf

    monkeypatch.setattr(gf, "is_steam_install", lambda d: False)
    monkeypatch.setattr(gf, "is_xbox_install", lambda d: False)

    calls: list[str] = []
    monkeypatch.setattr(launcher, "_open_uri",
                        lambda uri: calls.append(uri))
    monkeypatch.setattr(launcher, "_run_exe",
                        lambda exe, cwd: calls.append(f"EXE:{exe}"))

    launcher.launch_game(game_dir)

    assert any(c.startswith("EXE:") and "CrimsonDesert.exe" in c
               for c in calls), (
        f"Expected direct exe launch, got {calls!r}")


def test_launch_game_raises_when_exe_missing(tmp_path: Path):
    """If neither CrimsonDesert.exe nor crimsondesert.exe is found
    in bin64/, launcher must raise FileNotFoundError so CLI callers
    can exit non-zero."""
    game_dir = tmp_path / "game"
    (game_dir / "bin64").mkdir(parents=True)
    # No exe present

    from cdumm.engine import launcher
    with pytest.raises(FileNotFoundError):
        launcher.launch_game(game_dir)
