"""Variable-element lists are pinned from the loop body, not searched.

``list_element`` already works out that a reader's element is "16 + n",
meaning 12 bytes then a CString. Stage 1b used to report that and stop,
so every such reader stayed unresolved and any table using one could
never tile. It now pins ``('slist', n)`` with that base.

The base comes from the loop body, exactly like the fixed-element base
pinned one line above it in the same function. It is NOT searched against
table data: searching is what produced the wrong constants shipped for
HouseInfo (``CArray<[u8;46]>``) and FailMessageInfo (``CArray<[u8;33]>``),
where every element string in the measured build happened to be one
length.

Measured on stageinfo (GitHub #409) when this landed: readers pinned from
the loop body went from 3 to 9, and the record walk reached field 23 of 91
instead of field 13. That is the same result three passes of proving those
readers by hand produced one at a time.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
pytest.importorskip("capstone", reason="analysis-only dependency")

from derive_table_layout import Deriver


def _apply(blob: bytes, spec):
    """``_apply`` touches no instance state, so drive it unbound."""
    return Deriver._apply(None, blob, 0, len(blob), spec)


def _slist(base: int, strings: list[bytes]) -> bytes:
    out = struct.pack("<I", len(strings))
    for s in strings:
        out += b"\xAB" * base + struct.pack("<I", len(s)) + s
    return out


def test_slist_zero_base_is_a_carray_of_cstrings():
    blob = _slist(0, [b"abc", b"", b"defgh"])
    assert _apply(blob, ("slist", 0)) == len(blob)


def test_slist_honours_the_element_prefix():
    """The whole point: n > 0 must consume n bytes per element."""
    blob = _slist(12, [b"abc", b"defgh"])
    assert _apply(blob, ("slist", 12)) == len(blob)


def test_the_wrong_base_does_not_land_on_the_end():
    """A base that is off must fail rather than quietly tile."""
    blob = _slist(12, [b"abc", b"defgh"])
    assert _apply(blob, ("slist", 0)) != len(blob)


def test_an_empty_list_consumes_only_its_count():
    assert _apply(struct.pack("<I", 0) + b"junk", ("slist", 12)) == 4


def test_a_truncated_element_is_refused_not_guessed():
    blob = struct.pack("<I", 1) + b"\xAB" * 12 + struct.pack("<I", 99)
    assert _apply(blob, ("slist", 12)) is None
