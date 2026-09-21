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

from pathlib import Path

import pytest

from cdumm.semantic.pabgb_types import SUBSTRUCT_DEFS

ELEMENT = "StageInfo_ExecuteTargetStageEntry"
#: The list reader (sub_141494CF0 on b25116796), and the per-element
#: reader it calls (sub_14145C3F0 there), resolved by name at run time.
LIST_FIELD = ("StageInfo", "_executeTargetStageList")
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



@pytest.mark.slow
def test_the_binary_agrees_the_element_is_twenty_bytes(deriver, reader_of):
    deriver._memo.clear()
    assert deriver.list_element(reader_of(*LIST_FIELD)) == ("fixed", 20)


@pytest.mark.slow
def test_the_element_reader_is_the_one_this_width_came_from(deriver, reader_of):
    """Anchor the claim to the function it was read off.

    #420 and #423 were both built on an address that turned out not to be
    the reader they thought it was, so the reader is pinned here rather
    than assumed.
    """
    elem = deriver.element_reader(reader_of(*LIST_FIELD))
    assert elem is not None
    deriver._memo.clear()
    assert deriver.solve_reader(elem) == ("fixed", 20)


@pytest.mark.slow
def test_the_members_match_the_readers_sized_reads(deriver, reader_of):
    """Four 4-byte reads then four 1-byte reads, in that order."""
    elem = deriver.element_reader(reader_of(*LIST_FIELD))
    deriver._memo.clear()
    parts = deriver.reader_parts(elem)
    assert parts == [("fixed", 4)] * 4 + [("fixed", 1)] * 4
    assert [WIDTH[t] for _n, t in SUBSTRUCT_DEFS[ELEMENT]] == [
        n for _k, n in parts]
