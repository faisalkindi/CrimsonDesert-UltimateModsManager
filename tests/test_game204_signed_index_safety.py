"""Game 2.04 (#458): CDUMM must leave a 2.04 install untouched.

2.04 signs ``meta/0.papgt`` and refuses to start once it has been
rewritten. v3.17.13 could not read the new indexes, rebuilt the PAPGT
with 2 entries on Apply, and read zero entries back, which switched off
the voice-pack guard so Rescan and Fix Everything deleted 0036-0040.
AgentKush measured the deletions on a scratch copy of a real 2.04 install.

The last tests read the installed game and skip without one.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

import cdumm.worker_process as wp
from cdumm.archive.papgt_manager import (
    SIGNED_INDEX_MESSAGE,
    game_uses_signed_index,
    language_pack_dirs,
    read_papgt_entries,
    strip_dangling_entries,
)

from .conftest import _crimson_desert_dir
from .test_game204_archive_indexes import _compact_pamt


def _compact_papgt(names=("0000", "0036")) -> bytes:
    table = b"".join(n.encode() + b"\0" for n in names)
    entries = b"".join(
        struct.pack("<3I", 0x2001 if int(n) >= 36 else 0x7FFF00, i * 5, 0x1234 + i)
        for i, n in enumerate(names))
    return (struct.pack("<I", 0x02271681) + entries
            + struct.pack("<I", len(table)) + table + bytes(64))


def _legacy_papgt() -> bytes:
    table = b"0000\0"
    return (struct.pack("<3I", 0, 0, 0) + struct.pack("<3I", 0x3FFF00, 0, 0x1234)
            + struct.pack("<I", len(table)) + table)


def _game(tmp_path: Path, papgt: bytes, *, compact_pamt: bool = True) -> Path:
    game = tmp_path / "game"
    (game / "meta").mkdir(parents=True)
    (game / "meta" / "0.papgt").write_bytes(papgt)
    (game / "0000").mkdir()
    if compact_pamt:
        (game / "0000" / "0.pamt").write_bytes(_compact_pamt())
    return game


def test_compact_papgt_is_detected(tmp_path):
    assert game_uses_signed_index(_game(tmp_path, _compact_papgt()))


def test_a_legacy_papgt_over_2_04_archives_is_still_2_04(tmp_path):
    """v3.17.13 replaced a 2.04 PAPGT with a 50-byte legacy one (#458).
    The base PAMT still says 2.04, so the game must still count as 2.04."""
    assert game_uses_signed_index(_game(tmp_path, _legacy_papgt()))


def test_a_pre_2_04_game_is_not_flagged(tmp_path):
    game = _game(tmp_path, _legacy_papgt(), compact_pamt=False)
    (game / "0000" / "0.pamt").write_bytes(
        struct.pack("<4I", 0, 0, 0, 0) + struct.pack("<I", 6)
        + struct.pack("<IB", 0xFFFFFFFF, 1) + b"x")
    assert not game_uses_signed_index(game)


@pytest.mark.parametrize("worker", ["apply", "revert"])
def test_apply_and_revert_refuse_without_touching_the_game(tmp_path, worker):
    from cdumm.engine.apply_engine import ApplyWorker, RevertWorker

    papgt = _compact_papgt()
    game = _game(tmp_path, papgt)
    cls = ApplyWorker if worker == "apply" else RevertWorker
    w = cls(game, tmp_path / "vanilla", tmp_path / "cdumm.db")
    errors: list[str] = []
    w.error_occurred.connect(errors.append)
    w.run()
    assert errors == [SIGNED_INDEX_MESSAGE]
    assert (game / "meta" / "0.papgt").read_bytes() == papgt
    assert not (tmp_path / "cdumm.db").exists()


def test_every_listed_dir_counts_as_the_games_own(tmp_path):
    game = _game(tmp_path, _compact_papgt(("0000", "0035", "0036")))
    for n in ("0035", "0036", "0041"):
        (game / n).mkdir()
    assert language_pack_dirs(game) >= {"0036"}
    assert "0041" not in language_pack_dirs(game)


def test_strip_dangling_entries_never_writes_a_2_04_papgt(tmp_path):
    papgt = _compact_papgt(("0000", "0050"))
    game = _game(tmp_path, papgt)
    assert strip_dangling_entries(game) == 0
    assert (game / "meta" / "0.papgt").read_bytes() == papgt


def test_fix_everything_keeps_a_voice_pack_with_no_snapshot_rows(
        tmp_path, monkeypatch):
    """AgentKush's second case: a pack Steam re-downloaded or added
    after the last snapshot has no snapshot rows."""
    monkeypatch.setattr(wp, "_emit", lambda obj: None)
    game = _game(tmp_path, _compact_papgt(("0000", "0036")))
    (game / "0036").mkdir()
    (game / "0036" / "0.pamt").write_bytes(b"pack")
    (game / "0041").mkdir()
    (game / "0041" / "_cdumm_overlay.marker").write_text("")
    vanilla = tmp_path / "CDMods" / "vanilla"
    vanilla.mkdir(parents=True)
    wp._run_fix(str(game), str(vanilla), str(tmp_path / "cdumm.db"),
                steam_verified="1")
    assert (game / "0036" / "0.pamt").read_bytes() == b"pack"
    assert not (game / "0041").exists(), "a CDUMM overlay is still CDUMM's to clean"


def test_a_failed_base_index_read_is_not_cached(tmp_path):
    from cdumm.engine import json_patch_handler as jph

    game = tmp_path / "CDMods" / "vanilla"
    (game / "0000").mkdir(parents=True)
    (game / "0000" / "0.pamt").write_bytes(b"\xff" * 40)
    assert jph._get_pamt_index(game) == {}
    assert not list(game.parent.glob(".pamt_index_*_vanilla.cache"))
    assert jph._PAMT_INDEX_CACHE_VERSION != "v4", (
        "v3.17.13 cached empty v4 indexes on 2.04; they must not load")


# ── Installed game ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def game204() -> Path:
    g = _crimson_desert_dir()
    if g is None or not game_uses_signed_index(g):
        pytest.skip("no Crimson Desert 2.04 install found (set CDUMM_GAME_DIR)")
    return g


@pytest.mark.slow
def test_installed_2_04_papgt_reads_every_entry(game204):
    entries = read_papgt_entries(game204 / "meta" / "0.papgt")
    names = {n for n, _f, _h in entries}
    assert {"0000", "0008", "0035"} <= names
    on_disk = {d.name for d in game204.iterdir()
               if d.is_dir() and d.name.isdigit() and len(d.name) == 4
               and int(d.name) < 36}
    assert on_disk <= names


@pytest.mark.slow
def test_installed_2_04_iteminfo_resolves_and_extracts(game204):
    """Offsets, sizes and flags from the new reader, checked by
    extracting the table #461 and #466 could not find."""
    from cdumm.archive.paz_parse import parse_pamt
    from cdumm.engine.json_patch_handler import decompress_entry

    hits = [e for e in parse_pamt(str(game204 / "0008" / "0.pamt"))
            if e.path.lower().endswith("/iteminfo.staticinfobody")]
    assert hits, "iteminfo table missing from 0008"
    e = hits[0]
    with open(e.paz_file, "rb") as f:
        f.seek(e.offset)
        raw = f.read(e.comp_size)
    body = decompress_entry(raw, e)
    assert len(body) == e.orig_size
