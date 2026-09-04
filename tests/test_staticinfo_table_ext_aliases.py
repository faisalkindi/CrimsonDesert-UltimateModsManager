"""The 2026-09-04 client update renamed every game-data table.

``<table>.pabgh`` / ``<table>.pabgb`` became
``<table>.staticinfoheader`` / ``<table>.staticinfobody``. The
containers are byte-identical; only the extensions changed. Mods (and
every already-imported CDMods entry) still declare the ``.pabgb``
names, so every ``_find_pamt_entry`` lookup missed, every Format 3
vanilla extraction returned None, and Apply finished with
``APPLY_SILENT_FAILURE: N JSON mod(s) were enabled but produced no
game changes`` while telling users their mods were outdated.

These tests pin the alias layer that resolves both namings.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.archive.paz_parse import PazEntry
from cdumm.archive.table_ext import (
    BODY_EXTS,
    HEADER_EXTS,
    alias_paths,
    body_path_for,
    header_path_for,
    is_body_path,
    is_header_path,
    is_table_path,
    split_table_ext,
    strip_table_ext,
)

# --------------------------------------------------------------- helpers

def _FakeEntry(path: str, paz_file: str) -> PazEntry:
    """A real PazEntry — _find_pamt_entry isinstance-checks its hits, so
    a stand-in dataclass would pass the lookup and fail the type gate."""
    return PazEntry(path=path, paz_file=paz_file, offset=0, comp_size=4,
                    orig_size=4, flags=0, paz_index=0)


# ------------------------------------------------------------- table_ext

@pytest.mark.parametrize("path,expected", [
    ("gamedata/iteminfo.pabgb",
     ["gamedata/iteminfo.pabgb", "gamedata/iteminfo.staticinfobody"]),
    ("gamedata/iteminfo.staticinfobody",
     ["gamedata/iteminfo.staticinfobody", "gamedata/iteminfo.pabgb"]),
    ("iteminfo.pabgh",
     ["iteminfo.pabgh", "iteminfo.staticinfoheader"]),
    # Non-table paths must stay single-element: alias_paths runs once
    # per PAMT entry across millions of entries on an index build.
    ("ui/texture/foo.dds", ["ui/texture/foo.dds"]),
    ("noextension", ["noextension"]),
])
def test_alias_paths(path, expected):
    assert alias_paths(path) == expected


def test_alias_paths_puts_the_real_name_first():
    """_get_pamt_index relies on this: the first alias keeps the
    pre-update 'real name wins' / 'first seen wins' index semantics."""
    for p in ("a/b.staticinfobody", "a/b.pabgb", "a/b.dds"):
        assert alias_paths(p)[0] == p


def test_header_and_body_derivation_stays_in_one_naming_family():
    assert header_path_for("gamedata/iteminfo.pabgb") == \
        "gamedata/iteminfo.pabgh"
    assert header_path_for("gamedata/iteminfo.staticinfobody") == \
        "gamedata/iteminfo.staticinfoheader"
    assert body_path_for("gamedata/iteminfo.staticinfoheader") == \
        "gamedata/iteminfo.staticinfobody"
    # Not a table: handed back untouched so callers need no pre-check.
    assert header_path_for("ui/foo.dds") == "ui/foo.dds"


def test_predicates_and_stripping():
    assert is_body_path("x.pabgb") and is_body_path("x.STATICINFOBODY")
    assert is_header_path("x.pabgh") and is_header_path("x.staticinfoheader")
    assert not is_body_path("x.pabgh")
    assert not is_header_path("x.staticinfobody")
    assert is_table_path("x.pabgh") and not is_table_path("x.dds")
    assert split_table_ext("a/b.dds") == ("a/b.dds", None)
    for name in ("gamedata/ItemInfo.PABGB", "iteminfo.staticinfobody",
                 "iteminfo"):
        assert strip_table_ext(name) == "iteminfo"


def test_ext_tuples_cover_both_namings():
    assert ".pabgb" in BODY_EXTS and ".staticinfobody" in BODY_EXTS
    assert ".pabgh" in HEADER_EXTS and ".staticinfoheader" in HEADER_EXTS


# ------------------------------------------------- PAMT index resolution

def _index_over(tmp_path, monkeypatch, entry_names):
    """Build a _get_pamt_index over a fake 0008 holding ``entry_names``."""
    from cdumm.engine import json_patch_handler as jph

    cdmods_root = tmp_path / "CDMods"
    game_dir = tmp_path / "game"
    (game_dir / "0008").mkdir(parents=True)
    (game_dir / "0008" / "0.pamt").write_bytes(b"\x00")
    (game_dir / "0008" / "0.paz").write_bytes(b"\x00")
    cdmods_root.mkdir(parents=True)

    monkeypatch.setattr(jph, "parse_pamt", lambda pamt_path, paz_dir: [
        _FakeEntry(path=n, paz_file=str(Path(paz_dir) / "0.paz"))
        for n in entry_names])
    monkeypatch.setattr(jph, "get_cdmods_root",
                        lambda config, gdir: cdmods_root)
    jph._pamt_index_cache.clear()
    return jph, game_dir


POST_UPDATE = [
    "gamedata/iteminfo.staticinfobody",
    "gamedata/iteminfo.staticinfoheader",
    "gamedata/dropsetinfo.staticinfobody",
]


@pytest.mark.parametrize("lookup,expected", [
    # What every mod on Nexus declares.
    ("gamedata/iteminfo.pabgb", "gamedata/iteminfo.staticinfobody"),
    ("gamedata/iteminfo.pabgh", "gamedata/iteminfo.staticinfoheader"),
    ("gamedata/dropsetinfo.pabgb", "gamedata/dropsetinfo.staticinfobody"),
    # Format 3 mods target by bare basename.
    ("iteminfo.pabgb", "gamedata/iteminfo.staticinfobody"),
    # The real post-update name still resolves to itself.
    ("gamedata/iteminfo.staticinfobody", "gamedata/iteminfo.staticinfobody"),
])
def test_find_pamt_entry_resolves_legacy_names_after_the_rename(
        tmp_path, monkeypatch, lookup, expected):
    jph, game_dir = _index_over(tmp_path, monkeypatch, POST_UPDATE)
    entry = jph._find_pamt_entry(lookup, game_dir)
    assert entry is not None, (
        f"{lookup!r} did not resolve - this is the 2026-09-04 "
        f"APPLY_SILENT_FAILURE regression")
    assert entry.path == expected


def test_find_pamt_entries_lists_the_aliased_entry(tmp_path, monkeypatch):
    """The twin-retry pool (#167) must see the entry under either name."""
    jph, game_dir = _index_over(tmp_path, monkeypatch, POST_UPDATE)
    got = jph._find_pamt_entries("gamedata/iteminfo.pabgb", game_dir)
    assert [e.path for e in got][:1] == ["gamedata/iteminfo.staticinfobody"]


def test_basename_collision_still_prefers_the_lowest_numbered_dir(
        tmp_path, monkeypatch):
    """GitHub #99 guard: the game ships iteminfo twice (gamedata/ in
    0008, ui/ in 0072). Aliasing must not disturb first-seen-wins."""
    from cdumm.engine import json_patch_handler as jph

    cdmods_root = tmp_path / "CDMods"
    game_dir = tmp_path / "game"
    for d in ("0008", "0072"):
        (game_dir / d).mkdir(parents=True)
        (game_dir / d / "0.pamt").write_bytes(b"\x00")
        (game_dir / d / "0.paz").write_bytes(b"\x00")
    cdmods_root.mkdir(parents=True)

    def fake_parse(pamt_path, paz_dir):
        name = ("gamedata/iteminfo.staticinfobody"
                if Path(paz_dir).name == "0008"
                else "ui/iteminfo.staticinfobody")
        return [_FakeEntry(path=name,
                           paz_file=str(Path(paz_dir) / "0.paz"))]

    monkeypatch.setattr(jph, "parse_pamt", fake_parse)
    monkeypatch.setattr(jph, "get_cdmods_root",
                        lambda config, gdir: cdmods_root)
    jph._pamt_index_cache.clear()

    e = jph._find_pamt_entry("iteminfo.pabgb", game_dir)
    assert e is not None
    assert e.path == "gamedata/iteminfo.staticinfobody"
    assert Path(e.paz_file).parent.name == "0008"


def test_superseded_index_caches_are_removed(tmp_path, monkeypatch):
    """The v4 bump strands ~1 GB of v3 pickles per CDMods root."""
    jph, game_dir = _index_over(tmp_path, monkeypatch, POST_UPDATE)
    cdmods = tmp_path / "CDMods"
    stale = cdmods / ".pamt_index_v3_vanilla.cache"
    stale.write_bytes(b"junk")
    (cdmods / ".pamt_index.cache").write_bytes(b"junk")
    unrelated = cdmods / "cdumm.db"
    unrelated.write_bytes(b"keep me")

    jph._get_pamt_index(game_dir)

    assert not stale.exists()
    assert not (cdmods / ".pamt_index.cache").exists()
    assert unrelated.read_bytes() == b"keep me"
    assert list(cdmods.glob(".pamt_index*.cache"))


def test_sweep_keeps_the_sibling_current_version_caches(
        tmp_path, monkeypatch):
    """One CDMods root holds a vanilla cache AND one per game install.

    Sweeping everything but the file being written would evict the
    other dir's still-valid cache on every call, so vanilla and game
    would take turns rebuilding a multi-million-key index (~9s each)
    on every Apply.
    """
    jph, game_dir = _index_over(tmp_path, monkeypatch, POST_UPDATE)
    cdmods = tmp_path / "CDMods"
    ver = jph._PAMT_INDEX_CACHE_VERSION
    sibling = cdmods / f".pamt_index_{ver}_vanilla.cache"
    sibling.write_bytes(b"a valid current-version cache")

    jph._get_pamt_index(game_dir)

    assert sibling.exists(), (
        "the sweep evicted a current-version cache for another directory")
    assert sibling.read_bytes() == b"a valid current-version cache"


# ------------------------------------------------- downstream name users

def test_identify_table_from_path_accepts_the_new_name():
    from cdumm.semantic.parser import identify_table_from_path
    assert identify_table_from_path("gamedata/iteminfo.staticinfobody") == \
        identify_table_from_path("gamedata/iteminfo.pabgb")
    # A header is not a body and must not be mistaken for the table.
    assert identify_table_from_path(
        "gamedata/iteminfo.staticinfoheader") is None


def test_byte_merge_is_still_allowed_on_renamed_tables():
    """Two mods editing one table must byte-merge, not fall to last-wins."""
    from cdumm.engine.apply_engine import _entry_supports_byte_merge
    assert _entry_supports_byte_merge("gamedata/iteminfo.staticinfobody")
    assert _entry_supports_byte_merge("gamedata/iteminfo.staticinfoheader")
    assert _entry_supports_byte_merge("gamedata/iteminfo.pabgb")
    assert not _entry_supports_byte_merge("ui/texture/foo.dds")


def test_conflict_detector_matches_a_declared_target_to_a_renamed_entry():
    from cdumm.engine.conflict_detector import ConflictDetector as CD
    assert CD._entry_matches("iteminfo.pabgb",
                             "gamedata/iteminfo.staticinfobody")
    assert not CD._entry_matches("storeinfo.pabgb",
                                 "gamedata/iteminfo.staticinfobody")
