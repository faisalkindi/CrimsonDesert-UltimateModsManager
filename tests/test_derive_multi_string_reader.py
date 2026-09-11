"""A reader with several strings is kept as a sequence, not summarised.

``solve_reader`` describes a reader as "fixed bytes then ONE string",
the ``strplus`` shape. ``reader_parts`` already holds the real member
sequence, and when that sequence has more than one string the summary is
right about the fixed total and wrong about everything after the first
string: it silently drops each remaining length prefix and its bytes.

stageinfo's ``_sequencerDesc`` (``sub_14228E9D0``) is the worked example,
GitHub #409. ``solve_reader`` calls it ``strplus(37)``. ``reader_parts``
shows 37 fixed bytes and SIX strings:

    str, fixed 4, str, str, fixed 12, fixed 4, fixed 1 x9,
    str, fixed 4, str, str, fixed 4

so the summary under-consumes by five length-prefixed strings on any
record where they are not all empty. Over a 260 record probe of
stageinfo, failures at that field went from 26 to 2 when the sequence was
kept instead.

Order matters and cannot be flattened to a count plus a total: each
string's length prefix has to be read at its own position, so the walk
needs the members in program order.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
pytest.importorskip("capstone", reason="analysis-only dependency")

from derive_table_layout import Deriver


class _Walker:
    """``_apply`` reads no instance state, but the ('parts', ...) branch
    recurses through ``self``, so it needs a bound object rather than a
    bare unbound call."""

    _apply = Deriver._apply


def _apply(blob: bytes, spec):
    return _Walker()._apply(blob, 0, len(blob), spec)


def _s(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def test_a_two_string_reader_consumes_both():
    parts = (("str", 0), ("fixed", 4), ("str", 0))
    blob = _s(b"alpha") + b"\x01\x02\x03\x04" + _s(b"beta")
    assert _apply(blob, ("parts", parts)) == len(blob)


def test_the_strplus_summary_of_the_same_reader_falls_short():
    """Why the sequence is kept: the summary stops after string one."""
    blob = _s(b"alpha") + b"\x01\x02\x03\x04" + _s(b"beta")
    # "4 fixed bytes then one string" is what solve_reader would say.
    assert _apply(blob, ("str", 4)) != len(blob)


def test_empty_strings_still_cost_their_length_prefix():
    """The case that lets a wrong summary look right on most records."""
    parts = (("str", 0), ("str", 0), ("str", 0))
    blob = _s(b"") + _s(b"") + _s(b"")
    assert _apply(blob, ("parts", parts)) == 12


def test_a_truncated_member_is_refused_not_guessed():
    parts = (("str", 0), ("str", 0))
    blob = _s(b"alpha") + struct.pack("<I", 99)
    assert _apply(blob, ("parts", parts)) is None


def test_parts_nest():
    """A member may itself be a sequence, so the walk has to recurse."""
    inner = ("parts", (("str", 0), ("fixed", 2)))
    parts = (("fixed", 1), inner, ("str", 0))
    blob = b"\x00" + _s(b"xy") + b"\xAA\xBB" + _s(b"z")
    assert _apply(blob, ("parts", parts)) == len(blob)


def test_a_list_of_multi_string_elements_walks_every_element():
    """``plist``: u32 count, then N elements each an ordered sequence.

    stageinfo's ``_executeTargetStageList`` (``sub_141493A00``) is the
    worked example, GitHub #409. Its element reader ``sub_14145B100``
    reads TWO strings plus 12 fixed bytes, and ``solve_reader`` collapses
    that to ``strplus(12)``. Pinning the sequence instead carried the
    record walk from field 23 to field 24.
    """
    import struct as _s
    element = (("str", 0), ("str", 0), ("fixed", 4))
    blob = _s.pack("<I", 2)
    for a, b in ((b"one", b"two"), (b"", b"xyz")):
        blob += (_s.pack("<I", len(a)) + a
                                       + _s.pack("<I", len(b)) + b
                                       + b"\xAA\xBB\xCC\xDD")
    assert _apply(blob, ("plist", element)) == len(blob)


def test_an_empty_plist_consumes_only_its_count():
    import struct as _s
    assert _apply(_s.pack("<I", 0) + b"junk",
                  ("plist", (("str", 0),))) == 4


def test_collapsing_that_element_to_one_string_falls_short():
    """Why plist exists: slist drops the second string of each element."""
    import struct as _s
    blob = _s.pack("<I", 1) + _s.pack("<I", 3) + b"one" + \
        _s.pack("<I", 3) + b"two" + b"\xAA\xBB\xCC\xDD"
    assert _apply(blob, ("slist", 12)) != len(blob)
