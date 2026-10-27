"""GitHub #451 (7uk0), end to end on the installed game.

Character Creator Enhanced 9.1.15 ships its game files one folder too
deep, ``Character Creator Enhanced/{0009,0010,0012}/...``, and most of
them are NEW files with no vanilla PAMT entry. Before #452 the wrapper
sent every file to basename lookup, every new non-DDS file was
dropped, and the new DDSes were packed under the wrapper path, so 14
of 1,145 files reached the game and it never finished loading.

This builds a mod with the same shape and runs the real
``convert_to_paz_mod`` against the real 0009 / 0012 indexes. It needs
the game because the question is what the vanilla PAMTs contain;
there is no fixture for a 654,070-entry index.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.archive.paz_parse import parse_pamt
from cdumm.engine.crimson_browser_handler import (
    _pamt_full_paths,
    convert_to_paz_mod,
)
from cdumm.engine.json_patch_handler import decompress_entry

from .conftest import _crimson_desert_dir

#: A vanilla folder Character Creator adds new head prefabs to.
HEAD_FOLDER = "character/bin__/prefab/1_pc/01_phm/head/head"
NEW_PREFAB = f"{HEAD_FOLDER}/zz_cdumm_test_head_0001.prefab"
NEW_DDS = "ui/texture/image/customizeimage/zz_cdumm_test_icon_0001.dds"


@pytest.fixture(scope="module")
def game() -> Path:
    g = _crimson_desert_dir()
    if g is None or not (g / "0009" / "0.pamt").is_file():
        pytest.skip("no Crimson Desert install found (set CDUMM_GAME_DIR)")
    return g


@pytest.fixture(scope="module")
def vanilla_0009(game):
    pamt = game / "0009" / "0.pamt"
    entries = parse_pamt(str(pamt), paz_dir=str(game / "0009"))
    return entries, _pamt_full_paths(pamt)


def _unique_basename_elsewhere(entries, fulls) -> str:
    """A basename that occurs exactly once in 0009, outside HEAD_FOLDER.

    A new file with that name in HEAD_FOLDER used to be matched to it by
    the basename fallback and overwrite an unrelated vanilla file.
    """
    count: dict[str, int] = {}
    where: dict[str, str] = {}
    for fp in fulls:
        b = fp.rsplit("/", 1)[-1].lower()
        count[b] = count.get(b, 0) + 1
        where[b] = fp
    for b, n in count.items():
        if (n == 1 and b.endswith(".prefab")
                and not where[b].lower().startswith(HEAD_FOLDER)):
            return b
    pytest.skip("no uniquely named .prefab outside the head folder")


@pytest.mark.slow
def test_wrapped_mod_new_files_land_at_their_game_paths(
        game, vanilla_0009, tmp_path):
    entries, fulls = vanilla_0009
    assert fulls is not None and len(fulls) == len(entries)
    vanilla_paths = {fp.lower() for fp in fulls}
    assert HEAD_FOLDER in {fp.rsplit("/", 1)[0].lower() for fp in fulls}, (
        "premise: the head folder exists in vanilla 0009")
    assert NEW_PREFAB.lower() not in vanilla_paths, "premise: a NEW file"

    clash = _unique_basename_elsewhere(entries, fulls)
    clash_path = f"{HEAD_FOLDER}/{clash}"
    assert clash_path.lower() not in vanilla_paths

    mod = tmp_path / "Character Creator"
    wrap = mod / "Character Creator Enhanced"
    prefab = b"PREFAB" + bytes(range(256)) * 4
    clash_body = b"CLASH-" + bytes(64)
    dds = b"DDS " + b"\x7c" + bytes(123) + b"PIXELS" * 32
    for rel, body in ((f"0009/{NEW_PREFAB}", prefab),
                      (f"0009/{clash_path}", clash_body),
                      (f"0012/{NEW_DDS}", dds)):
        p = wrap / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    (mod / "README.txt").write_text("not a game file")

    work = tmp_path / "work"
    work.mkdir()
    out = convert_to_paz_mod(
        {"_base_dir": mod, "files_dir": ".", "id": "cc-451"}, game, work)
    assert out == work

    # Nothing replaced a vanilla file: the name clash is a NEW file in
    # its own folder, not the vanilla file that shares its basename. A
    # replacement would have copied that dir's PAZ into the work dir.
    for d in ("0009", "0012"):
        assert not list((work / d).glob("*.paz")), d

    groups = [d for d in work.iterdir()
              if d.is_dir() and d.name.isdigit() and (d / "0.paz").is_file()]
    assert len(groups) == 1, "all new files go into ONE overlay group"
    g = groups[0]
    got_entries = parse_pamt(str(g / "0.pamt"), paz_dir=str(g))
    got = dict(zip((p.lower() for p in _pamt_full_paths(g / "0.pamt")),
                   got_entries))

    assert set(got) >= {NEW_PREFAB.lower(), clash_path.lower(),
                        NEW_DDS.lower()}
    assert not any("character creator" in p for p in got), (
        "the wrapper folder must not reach a game path")

    for path, body in ((NEW_PREFAB, prefab), (clash_path, clash_body)):
        e = got[path.lower()]
        with open(e.paz_file, "rb") as fh:
            fh.seek(e.offset)
            raw = fh.read(e.comp_size)
        assert decompress_entry(raw, e) == body, path
