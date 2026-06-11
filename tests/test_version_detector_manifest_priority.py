"""Steam build-id detection must target the game's own appmanifest.

``get_steam_build_id`` used to grep every ``appmanifest_*.acf`` for
the string "Crimson Desert" and return the first buildid found. A
DLC/soundtrack manifest containing the same string could glob first
and win, fingerprinting the wrong product. The canonical manifest is
``appmanifest_3321460.acf`` (same precise lookup language.py uses);
the name grep remains only as a fallback when that file is absent.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.version_detector import get_steam_build_id


def _write_acf(path: Path, name: str, buildid: str) -> None:
    path.write_text(
        '"AppState"\n'
        '{\n'
        f'\t"name"\t\t"{name}"\n'
        f'\t"buildid"\t\t"{buildid}"\n'
        '}\n',
        encoding="utf-8")


def _make_install(tmp_path: Path) -> tuple[Path, Path]:
    steamapps = tmp_path / "steamapps"
    game_dir = steamapps / "common" / "Crimson Desert"
    game_dir.mkdir(parents=True)
    return steamapps, game_dir


def test_canonical_manifest_wins_over_soundtrack(tmp_path: Path) -> None:
    steamapps, game_dir = _make_install(tmp_path)
    # Lower-numbered manifest globs FIRST and contains the game name;
    # the old name-grep returned its buildid.
    _write_acf(steamapps / "appmanifest_1111111.acf",
               "Crimson Desert Original Soundtrack", "999999")
    _write_acf(steamapps / "appmanifest_3321460.acf",
               "Crimson Desert", "424242")

    assert get_steam_build_id(game_dir) == "424242", (
        "a DLC/soundtrack manifest won over appmanifest_3321460.acf")


def test_name_grep_fallback_when_canonical_absent(tmp_path: Path) -> None:
    steamapps, game_dir = _make_install(tmp_path)
    _write_acf(steamapps / "appmanifest_777.acf",
               "Crimson Desert", "131313")

    assert get_steam_build_id(game_dir) == "131313"


def test_returns_none_when_no_manifest(tmp_path: Path) -> None:
    _, game_dir = _make_install(tmp_path)
    assert get_steam_build_id(game_dir) is None
