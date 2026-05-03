"""GitHub #61 (Loe-Aner, 2026-05-02): Apply stalls 3 minutes at
0010/0.paz with P3rdpc Mod V 3.5 (97k deltas / 130MB), watchdog
kills at 180s.

Root cause: _get_vanilla_entry_content() re-parses the entire PAMT
from disk on every call. The overlay dedup phase calls this once
per unique (pamt_dir, entry_path) group — for P3rdpc that's
hundreds of calls × ~2ms per parse = O(n²) blow-up.

Fix: cache parse_pamt results keyed by PAMT path within an apply
run. O(n) → O(1) per dir after first parse.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def _make_fake_pamt(dir_path: Path, num_entries: int = 5) -> None:
    """Drop a minimal PAMT file. parse_pamt is stubbed so contents
    don't matter — only the path lookup needs to succeed."""
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "0.pamt").write_bytes(b"\x00" * 256)


def test_get_vanilla_entry_content_caches_pamt_parse(tmp_path: Path):
    """Repeated calls for the same pamt_dir must hit a cache instead
    of re-parsing the PAMT on every call. With 500+ overlay groups
    in a single dir (P3rdpc case), the un-cached path is O(n²)."""
    from cdumm.engine.apply_engine import ApplyWorker
    from cdumm.archive.paz_parse import PazEntry
    from cdumm.engine import apply_engine as ae_mod

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    _make_fake_pamt(vanilla_dir / "0010")

    worker = ApplyWorker.__new__(ApplyWorker)
    worker._vanilla_dir = vanilla_dir
    worker._game_dir = game_dir

    # Stub parse_pamt with a counter. Returns one entry whose
    # _extract_from_paz call is also stubbed to a fixed payload.
    parse_calls = {"count": 0}
    fake_entries = [PazEntry(
        path="gamedata/x.pabgb",
        paz_file=str(vanilla_dir / "0010" / "0.paz"),
        offset=0, comp_size=10, orig_size=10, flags=0, paz_index=0,
    )]

    def _fake_parse(pamt_path, paz_dir):
        parse_calls["count"] += 1
        return fake_entries

    def _fake_extract(entry, paz_path=None):
        return b"VANILLA_BODY"

    import cdumm.archive.paz_parse as pp_mod
    import cdumm.engine.json_patch_handler as jph_mod
    monkey_orig_parse = pp_mod.parse_pamt
    monkey_orig_extract = jph_mod._extract_from_paz
    pp_mod.parse_pamt = _fake_parse
    jph_mod._extract_from_paz = _fake_extract

    try:
        # Simulate 500 overlay-dedup lookups for the same pamt_dir
        # (the P3rdpc pattern: many entries inside 0010/0.paz).
        for i in range(500):
            body = worker._get_vanilla_entry_content(
                "0010/0.paz", "gamedata/x.pabgb")
            assert body == b"VANILLA_BODY"

        assert parse_calls["count"] == 1, (
            f"parse_pamt was called {parse_calls['count']} times for the "
            f"same PAMT — must be cached. P3rdpc's 500+ overlay dedup "
            f"calls trigger 500 disk re-parses without caching, blowing "
            f"past the 180s watchdog."
        )
    finally:
        pp_mod.parse_pamt = monkey_orig_parse
        jph_mod._extract_from_paz = monkey_orig_extract


def test_pamt_cache_separate_per_pamt_path(tmp_path: Path):
    """Different PAMT files (different pamt_dir) must each parse once,
    not share a single cache slot."""
    from cdumm.engine.apply_engine import ApplyWorker
    from cdumm.archive.paz_parse import PazEntry

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    _make_fake_pamt(vanilla_dir / "0010")
    _make_fake_pamt(vanilla_dir / "0020")

    worker = ApplyWorker.__new__(ApplyWorker)
    worker._vanilla_dir = vanilla_dir
    worker._game_dir = game_dir

    parse_calls = {"count": 0, "paths": []}
    fake_entries = [PazEntry(
        path="gamedata/x.pabgb",
        paz_file="dummy",
        offset=0, comp_size=10, orig_size=10, flags=0, paz_index=0,
    )]

    def _fake_parse(pamt_path, paz_dir):
        parse_calls["count"] += 1
        parse_calls["paths"].append(str(pamt_path))
        return fake_entries

    def _fake_extract(entry, paz_path=None):
        return b"VANILLA_BODY"

    import cdumm.archive.paz_parse as pp_mod
    import cdumm.engine.json_patch_handler as jph_mod
    pp_mod_orig = pp_mod.parse_pamt
    jph_mod_orig = jph_mod._extract_from_paz
    pp_mod.parse_pamt = _fake_parse
    jph_mod._extract_from_paz = _fake_extract

    try:
        for _ in range(50):
            worker._get_vanilla_entry_content("0010/0.paz", "gamedata/x.pabgb")
            worker._get_vanilla_entry_content("0020/0.paz", "gamedata/x.pabgb")
        assert parse_calls["count"] == 2, (
            f"Expected exactly 2 parses (one per pamt_dir), got "
            f"{parse_calls['count']}. Paths: {parse_calls['paths'][:5]}"
        )
    finally:
        pp_mod.parse_pamt = pp_mod_orig
        jph_mod._extract_from_paz = jph_mod_orig
