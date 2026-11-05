"""Steam appmanifest lookups on native macOS (GitHub #457, #466).

The Steam build id (the main input to the game-version fingerprint), the
Steam language and the Steam app id are all read from
``appmanifest_3321460.acf`` in the library's ``steamapps`` folder. All
three lookups took that folder to be ``game_dir.parent.parent``, which
holds on Windows and Linux (``steamapps/common/Crimson Desert``).

On native macOS the game directory CDUMM stores is the ``packages`` folder
that ``_resolve_macos_game_dir`` returns, inside the ``.app`` bundle or next
to it (#463). Two levels up from there is the bundle's ``Contents`` folder
or ``steamapps/common``, so the manifest was never found, and with no
``bin64/CrimsonDesert.exe`` either, ``detect_game_version`` returned None.
#457's bug report shows ``Game Version Hash: (not detected)``; #466 reports
every rescan ending in "Snapshot Done — Game Version Unknown".

The macOS game directories below come from the real resolver rather than a
hand-written path, so these tests check the directory CDUMM actually
stores. The Windows and no-steamapps cases pin that those layouts still
read the folder they read before.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from cdumm.engine import game_monitor
from cdumm.engine.game_monitor import get_steam_app_id
from cdumm.engine.language import detect_steam_language
from cdumm.engine.snapshot_manager import SnapshotWorker
from cdumm.engine.version_detector import (
    backfill_stored_fingerprints,
    detect_game_version,
    get_steam_build_id,
)
from cdumm.gui.apply_watchdog import is_apply_blocked_by_live_game_change
from cdumm.storage import game_finder
from cdumm.storage.config import Config
from cdumm.storage.database import Database
from cdumm.storage.game_finder import steamapps_dir_for

LAYOUTS = ["windows", "macos_in_bundle", "macos_sibling_packages", "no_steamapps"]
MACOS_LAYOUTS = ["macos_in_bundle", "macos_sibling_packages"]


def _write_manifest(folder: Path, buildid: str = "424242",
                    language: str = "german") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "appmanifest_3321460.acf").write_text(
        '"AppState"\n'
        '{\n'
        '\t"appid"\t\t"3321460"\n'
        '\t"name"\t\t"Crimson Desert"\n'
        '\t"installdir"\t\t"Crimson Desert"\n'
        f'\t"buildid"\t\t"{buildid}"\n'
        '\t"UserConfig"\n'
        '\t{\n'
        f'\t\t"language"\t\t"{language}"\n'
        '\t}\n'
        '}\n',
        encoding="utf-8")


def _game_files(folder: Path) -> None:
    """The two files ``_looks_like_game_root`` requires, plus a PAMT so
    the snapshot worker has something to hash."""
    (folder / "0008").mkdir(parents=True)
    (folder / "0008" / "0.paz").write_bytes(b"PAZ" + b"\x00" * 80)
    (folder / "0008" / "0.pamt").write_bytes(b"PAMT" + b"\x00" * 50)
    (folder / "meta").mkdir()
    (folder / "meta" / "0.papgt").write_bytes(b"stub")


def _layout(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Build one install. Returns (game_dir, folder the manifest lives in)."""
    if name == "windows":
        steamapps = tmp_path / "SteamLibrary" / "steamapps"
        game_dir = steamapps / "common" / "Crimson Desert"
        _game_files(game_dir)
        return game_dir, steamapps
    if name == "macos_in_bundle":
        steamapps = tmp_path / "Steam" / "steamapps"
        app = steamapps / "common" / "Crimson Desert" / "CrimsonDesert_Steam.app"
        _game_files(app / "Contents" / "Resources" / "packages")
        game_dir = game_finder._resolve_macos_game_dir(app)
        assert game_dir == app / "Contents" / "Resources" / "packages"
        return game_dir, steamapps
    if name == "macos_sibling_packages":
        steamapps = tmp_path / "Steam" / "steamapps"
        wrapper = steamapps / "common" / "Crimson Desert"
        (wrapper / "CrimsonDesert_Steam.app" / "Contents").mkdir(parents=True)
        _game_files(wrapper / "packages")
        game_dir = game_finder._resolve_macos_game_dir(wrapper)
        assert game_dir == wrapper / "packages"
        return game_dir, steamapps
    if name == "no_steamapps":
        # Xbox-style: no steamapps anywhere above, so the lookups keep
        # using game_dir.parent.parent, and that is where this test puts
        # the manifest.
        game_dir = tmp_path / "XboxGames" / "Crimson Desert" / "Content"
        _game_files(game_dir)
        return game_dir, game_dir.parent.parent
    raise AssertionError(f"unknown layout {name}")


# ── steamapps_dir_for: path arithmetic only ──────────────────────────


@pytest.mark.parametrize("game_dir, expected", [
    # The Windows layout: the same folder as game_dir.parent.parent.
    (PureWindowsPath(r"X:\SteamLibrary\steamapps\common\Crimson Desert"),
     PureWindowsPath(r"X:\SteamLibrary\steamapps")),
    # #457's game dir after game 2.04: packages next to the .app.
    (PurePosixPath("/Users/me/Library/Application Support/Steam/steamapps/"
                   "common/Crimson Desert/packages"),
     PurePosixPath("/Users/me/Library/Application Support/Steam/steamapps")),
    # #457's pointer from before 2.04: packages inside the .app.
    (PurePosixPath("/Users/me/Library/Application Support/Steam/steamapps/"
                   "common/Crimson Desert/CrimsonDesert_Steam.app/Contents/"
                   "Resources/packages"),
     PurePosixPath("/Users/me/Library/Application Support/Steam/steamapps")),
    # No steamapps above: game_dir.parent.parent, as before.
    (PureWindowsPath(r"C:\XboxGames\Crimson Desert\Content"),
     PureWindowsPath(r"C:\XboxGames")),
], ids=["windows", "macos_sibling_packages", "macos_in_bundle", "no_steamapps"])
def test_steamapps_dir_for_report_paths(game_dir, expected):
    assert steamapps_dir_for(game_dir) == expected


def test_steamapps_match_ignores_case():
    # Older Steam clients named the folder SteamApps.
    game_dir = PurePosixPath("/Steam/SteamApps/common/Crimson Desert/packages")
    assert steamapps_dir_for(game_dir) == PurePosixPath("/Steam/SteamApps")


def test_steamapps_search_stops_at_the_level_limit():
    limit = game_finder._STEAMAPPS_MAX_LEVELS
    steamapps = PurePosixPath("/lib/steamapps")
    at_limit = steamapps.joinpath(*(f"d{i}" for i in range(limit)))
    past_limit = at_limit / "deeper"
    assert steamapps_dir_for(at_limit) == steamapps
    assert steamapps_dir_for(past_limit) == past_limit.parent.parent


# ── the three manifest lookups, per layout ───────────────────────────


@pytest.mark.parametrize("layout", LAYOUTS)
def test_build_id_is_read(tmp_path, layout):
    game_dir, manifest_dir = _layout(tmp_path, layout)
    _write_manifest(manifest_dir, buildid="424242")
    assert get_steam_build_id(game_dir) == "424242"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_steam_language_is_read(tmp_path, layout):
    game_dir, manifest_dir = _layout(tmp_path, layout)
    _write_manifest(manifest_dir, language="german")
    assert detect_steam_language(game_dir) == "german"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_app_id_comes_from_the_manifest(tmp_path, layout, monkeypatch):
    # The manifest's id and the fallback are both 3321460, so swap the
    # fallback for a marker to see which of the two answered.
    monkeypatch.setattr(game_monitor, "FALLBACK_APP_ID", "fallback")
    game_dir, manifest_dir = _layout(tmp_path, layout)
    _write_manifest(manifest_dir)
    assert get_steam_app_id(game_dir) == "3321460"


# ── what that changes on macOS ───────────────────────────────────────


@pytest.mark.parametrize("layout", MACOS_LAYOUTS)
def test_macos_fingerprint_follows_the_steam_build(tmp_path, layout):
    """There is no exe on macOS, so the build id is the fingerprint's only
    input; a Steam update has to change it."""
    game_dir, manifest_dir = _layout(tmp_path, layout)
    _write_manifest(manifest_dir, buildid="424242")
    before = detect_game_version(game_dir)
    _write_manifest(manifest_dir, buildid="424243")
    after = detect_game_version(game_dir)
    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.parametrize("layout", MACOS_LAYOUTS)
def test_macos_snapshot_stamps_the_game_version(tmp_path, layout):
    """#466: rescans ended in "Snapshot Done — Game Version Unknown"."""
    game_dir, manifest_dir = _layout(tmp_path, layout)
    _write_manifest(manifest_dir)
    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    try:
        SnapshotWorker(game_dir=game_dir, db_path=db.db_path).run()
        cfg = Config(db)
        assert cfg.get("last_snapshot_version_stamped") == "1"
        stored = cfg.get("game_version_fingerprint")
        assert stored is not None
        assert stored == detect_game_version(game_dir)
    finally:
        db.close()


@pytest.mark.parametrize("layout", MACOS_LAYOUTS)
def test_macos_upgrade_adopts_the_live_version_then_catches_updates(
        tmp_path, layout):
    """A macOS database from before this fix: a snapshot, but no stored
    fingerprint, no ``version_detector_v2`` flag, and a NULL
    ``game_version_hash`` on every mod, because the detector returned None
    for all of them. ``main.py`` runs ``backfill_stored_fingerprints``
    before it compares stored against live, so the first launch stores the
    live value, there is no mismatch to report, and the Apply gate passes.
    After a Steam update the build id moves and the gate blocks until a
    rescan, as it does on Windows."""
    game_dir, manifest_dir = _layout(tmp_path, layout)
    _write_manifest(manifest_dir, buildid="424242")
    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    try:
        db.connection.execute(
            "INSERT INTO snapshots (file_path, file_hash, file_size) "
            "VALUES ('0008/0.paz', 'aa', 83)")
        db.connection.execute(
            "INSERT INTO mods (name, mod_type, game_version_hash) "
            "VALUES ('old mod', 'paz', NULL)")
        db.connection.commit()

        assert backfill_stored_fingerprints(db, game_dir) is True
        stored = Config(db).get("game_version_fingerprint")
        live = detect_game_version(game_dir)
        assert stored is not None
        assert stored == live
        assert not is_apply_blocked_by_live_game_change(
            current_fingerprint=live, snapshot_fingerprint=stored)
        # NULL means "no version recorded", which the outdated badge skips.
        assert db.connection.execute(
            "SELECT game_version_hash FROM mods").fetchone()[0] is None

        _write_manifest(manifest_dir, buildid="424243")
        assert is_apply_blocked_by_live_game_change(
            current_fingerprint=detect_game_version(game_dir),
            snapshot_fingerprint=stored)
    finally:
        db.close()
