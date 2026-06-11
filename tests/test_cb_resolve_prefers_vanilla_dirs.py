"""CB file-to-directory resolution must not anchor on CDUMM's own
overlay dirs or prefer mod dirs over vanilla (audit finding 4).

_resolve_files_to_directories used to pick the highest-numbered dir
with a 0.pamt. That could be a transient CDUMM apply overlay (carries
_cdumm_overlay.marker, rebuilt or deleted every apply) or a standalone
mod's dir, baking mod slot sizes into a fresh import. Rules now:

  * dirs carrying _cdumm_overlay.marker are skipped outright,
  * when a file resolves in both a vanilla dir (0000-0035) and a mod
    dir (0036+), the vanilla dir wins,
  * a 0036+ dir is chosen only when nothing vanilla has the file.
"""
from __future__ import annotations

from pathlib import Path

from tests.pamt_synth import build_pamt

from cdumm.engine.crimson_browser_handler import (
    _resolve_files_to_directories,
)


def _make_game_dir_entry(game_dir: Path, dir_name: str, file_name: str,
                         marker: bool = False) -> None:
    d = game_dir / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "0.pamt").write_bytes(build_pamt([{
        "name": file_name, "offset": 0, "comp_size": 4,
        "orig_size": 4, "flags": 0,
    }]))
    if marker:
        (d / "_cdumm_overlay.marker").write_text("cdumm", encoding="utf-8")


def test_overlay_marker_dir_is_skipped(tmp_path: Path):
    game_dir = tmp_path / "game"
    src = tmp_path / "hair.xml"
    src.write_text("<hair/>", encoding="utf-8")
    _make_game_dir_entry(game_dir, "0009", "hair.xml")
    _make_game_dir_entry(game_dir, "0036", "hair.xml", marker=True)

    resolved, unresolved = _resolve_files_to_directories(
        [("character/hair.xml", src)], game_dir)

    assert "0036" not in resolved, (
        "transient CDUMM overlay dir must never anchor CB resolution")
    assert "0009" in resolved
    assert unresolved == []


def test_vanilla_dir_preferred_over_mod_dir(tmp_path: Path):
    game_dir = tmp_path / "game"
    src = tmp_path / "hair.xml"
    src.write_text("<hair/>", encoding="utf-8")
    _make_game_dir_entry(game_dir, "0009", "hair.xml")
    # Standalone-mod dir, no marker, but above the vanilla 0035 ceiling.
    _make_game_dir_entry(game_dir, "0040", "hair.xml")

    resolved, unresolved = _resolve_files_to_directories(
        [("character/hair.xml", src)], game_dir)

    assert "0040" not in resolved, (
        "a 0036+ mod dir must not win when a vanilla dir has the file")
    assert "0009" in resolved
    assert unresolved == []


def test_highest_vanilla_dir_still_wins_within_vanilla_range(
        tmp_path: Path):
    game_dir = tmp_path / "game"
    src = tmp_path / "hair.xml"
    src.write_text("<hair/>", encoding="utf-8")
    _make_game_dir_entry(game_dir, "0009", "hair.xml")
    _make_game_dir_entry(game_dir, "0030", "hair.xml")

    resolved, _ = _resolve_files_to_directories(
        [("character/hair.xml", src)], game_dir)

    assert "0030" in resolved
    assert "0009" not in resolved


def test_mod_dir_used_when_no_vanilla_dir_has_file(tmp_path: Path,
                                                   caplog):
    game_dir = tmp_path / "game"
    src = tmp_path / "newthing.xml"
    src.write_text("<x/>", encoding="utf-8")
    _make_game_dir_entry(game_dir, "0040", "newthing.xml")

    import logging
    with caplog.at_level(logging.WARNING,
                         logger="cdumm.engine.crimson_browser_handler"):
        resolved, unresolved = _resolve_files_to_directories(
            [("character/newthing.xml", src)], game_dir)

    assert "0040" in resolved
    assert unresolved == []
    assert any("mod-installed dir 0040" in r.message
               for r in caplog.records), (
        "choosing a 0036+ dir must be logged for diagnosability")
