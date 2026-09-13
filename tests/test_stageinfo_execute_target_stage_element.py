"""``_executeTargetStageList`` has a 20-byte element, not a u32.

The shipped order typed it ``CArray<u32>``. The reader says otherwise.
On b25116796 the list reader is ``sub_141494CF0`` and one element is read
by ``sub_14145C3F0`` in eight sized stream reads, 4+4+4+4+1+1+1+1 = 20
bytes, with the store side using a stride of 20 to match.

Why the wrong width survived: the list is EMPTY on 47424 of the 51861
records, and a zero count consumes its four count bytes whatever the
element is. So the decode score cannot tell the two apart, and indeed
does not move at all when the type is corrected. That is the reason this
is checked against the binary here rather than by decoding records, and
the reason the correction is safe: it cannot regress a walk that never
exercised the width.

This is the same trap as the rest of GitHub #409. A plausible width that
nothing contradicts is not evidence, and the decode score is only a
referee when the data actually varies the thing under test.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from cdumm.semantic.pabgb_types import SUBSTRUCT_DEFS  # noqa: E402

ELEMENT = "StageInfo_ExecuteTargetStageEntry"
#: The list reader, and the per-element reader it calls.
LIST_READER = 0x141494CF0
ELEM_READER = 0x14145C3F0
WIDTH = {"u8": 1, "u16": 2, "u32": 4, "u64": 8}


def test_the_element_type_exists_and_is_twenty_bytes():
    """Stands on its own with no game install, so CI covers the arithmetic."""
    members = SUBSTRUCT_DEFS[ELEMENT]
    assert sum(WIDTH[t] for _n, t in members) == 20


def test_the_field_points_at_that_element_type():
    import json
    over = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas"
         / "pabgb_type_overrides.json").read_text(encoding="utf-8-sig"))
    got = over["StageInfo"]["_executeTargetStageList"]["type"]
    assert got == f"CArray<{ELEMENT}>"


def _game_dir() -> Path | None:
    env = os.environ.get("CDUMM_GAME_DIR")
    if env and (Path(env) / "bin64").is_dir():
        return Path(env)
    for root in ("C:", "D:", "E:", "F:"):
        for lib in ("SteamLibrary", "Steam"):
            p = Path(f"{root}/{lib}/steamapps/common/Crimson Desert")
            if (p / "bin64").is_dir():
                return p
    return None


@pytest.fixture(scope="module")
def deriver():
    pytest.importorskip("capstone", reason="analysis-only dependency")
    pytest.importorskip("pefile", reason="analysis-only dependency")
    game = _game_dir()
    if game is None:
        pytest.skip("no Crimson Desert install found (set CDUMM_GAME_DIR)")
    from derive_table_layout import Deriver
    return Deriver(game)


@pytest.mark.slow
def test_the_binary_agrees_the_element_is_twenty_bytes(deriver):
    deriver._memo.clear()
    assert deriver.list_element(LIST_READER) == ("fixed", 20)


@pytest.mark.slow
def test_the_element_reader_is_the_one_this_width_came_from(deriver):
    """Anchor the claim to the function it was read off.

    #420 and #423 were both built on an address that turned out not to be
    the reader they thought it was, so the reader is pinned here rather
    than assumed.
    """
    assert deriver.element_reader(LIST_READER) == ELEM_READER
    deriver._memo.clear()
    assert deriver.solve_reader(ELEM_READER) == ("fixed", 20)


@pytest.mark.slow
def test_the_members_match_the_readers_sized_reads(deriver):
    """Four 4-byte reads then four 1-byte reads, in that order."""
    deriver._memo.clear()
    parts = deriver.reader_parts(ELEM_READER)
    assert parts == [("fixed", 4)] * 4 + [("fixed", 1)] * 4
    assert [WIDTH[t] for _n, t in SUBSTRUCT_DEFS[ELEMENT]] == [
        n for _k, n in parts]
