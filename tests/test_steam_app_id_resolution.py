"""GitHub #186 (lupo1190): the Launch button fired
steam://rungameid/332146 and Steam answered 'Game configuration
unavailable'. The real Crimson Desert Steam AppID is 3321460 (seven
digits); 332146 is that id with the trailing 0 chopped off.

By elimination, get_steam_app_id can only return: a steam_appid.txt
value, the Crimson Desert appmanifest's filename id, or the hard-coded
fallback 3321460. lupo1190's install has appmanifest_3321460.acf, so
332146 had to come from a stale / corrupt steam_appid.txt that was
being preferred over the authoritative appmanifest.

The appmanifest is Steam's own record of the installed game and the id
it uses for steam://rungameid. When a Crimson Desert appmanifest
exists, its id must win over steam_appid.txt. These tests pin that
precedence plus the existing fallbacks.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.game_monitor import get_steam_app_id, FALLBACK_APP_ID


def _make_steam_layout(tmp_path: Path) -> Path:
    """Build <tmp>/steamapps/common/Crimson Desert and return game_dir.
    get_steam_app_id resolves appmanifests via game_dir.parent.parent."""
    game_dir = tmp_path / "steamapps" / "common" / "Crimson Desert"
    game_dir.mkdir(parents=True)
    (game_dir / "bin64").mkdir()
    return game_dir


def test_appmanifest_wins_over_stale_steam_appid_txt(tmp_path: Path):
    """#186: a steam_appid.txt with the wrong (truncated) id must NOT
    override the authoritative Crimson Desert appmanifest."""
    game_dir = _make_steam_layout(tmp_path)
    steamapps = game_dir.parent.parent
    (steamapps / "appmanifest_3321460.acf").write_text(
        '"AppState"\n{\n  "appid"  "3321460"\n  "name"  "Crimson Desert"\n}\n',
        encoding="utf-8")
    # The stale / corrupt file the bug exposed:
    (game_dir / "steam_appid.txt").write_text("332146", encoding="utf-8")

    assert get_steam_app_id(game_dir) == "3321460"


def test_steam_appid_txt_used_when_no_appmanifest(tmp_path: Path):
    """With no Crimson Desert appmanifest, a clean steam_appid.txt is
    still a valid source (unchanged behaviour for that case)."""
    game_dir = _make_steam_layout(tmp_path)
    (game_dir / "steam_appid.txt").write_text("3321460\n", encoding="utf-8")
    assert get_steam_app_id(game_dir) == "3321460"


def test_appmanifest_without_crimson_desert_name_is_ignored(tmp_path: Path):
    """A neighbouring appmanifest for a DIFFERENT game must not be
    mistaken for Crimson Desert; fall through to steam_appid.txt."""
    game_dir = _make_steam_layout(tmp_path)
    steamapps = game_dir.parent.parent
    (steamapps / "appmanifest_220.acf").write_text(
        '"AppState"\n{\n  "appid"  "220"\n  "name"  "Half-Life 2"\n}\n',
        encoding="utf-8")
    (game_dir / "steam_appid.txt").write_text("3321460", encoding="utf-8")
    assert get_steam_app_id(game_dir) == "3321460"


def test_fallback_when_nothing_present(tmp_path: Path):
    game_dir = _make_steam_layout(tmp_path)
    assert get_steam_app_id(game_dir) == FALLBACK_APP_ID


def test_corrupt_steam_appid_with_no_appmanifest_falls_back(tmp_path: Path):
    """If steam_appid.txt is the only source but is non-numeric junk,
    sanitise drops it and the known-good fallback wins rather than a
    broken id reaching Steam."""
    game_dir = _make_steam_layout(tmp_path)
    (game_dir / "steam_appid.txt").write_text("not-a-number", encoding="utf-8")
    assert get_steam_app_id(game_dir) == FALLBACK_APP_ID
