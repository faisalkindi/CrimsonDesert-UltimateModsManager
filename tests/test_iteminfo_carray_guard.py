"""Regression for GitHub #247 (CD 1.13 apply hang).

When the game ships a layout shift the native iteminfo parser doesn't
model yet, a CArray length prefix decodes as garbage (millions/billions).
Before this guard, ``_Reader.carray`` ran ``range(n)`` and spun building a
giant list until it finally overran — long enough that a Format 3 iteminfo
mod (e.g. Fat Stacks, 2000+ intents) tripped the 180s apply watchdog and
killed the whole run, taking every other mod down with it.

The parser must instead fail fast so ``build_iteminfo_intent_change``'s
existing try/except turns it into a clean "game version not supported yet"
skip, letting the non-iteminfo mods apply.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.iteminfo_native_parser import _Reader


def test_carray_rejects_billions_count_fast():
    # count = ~4 billion with no element bytes following. A real array
    # can't have more elements than remaining bytes, so this must raise
    # immediately rather than attempt to build range(4_000_000_000).
    r = _Reader(struct.pack("<I", 4_000_000_000))
    with pytest.raises(ValueError):
        r.carray(_Reader.u8)


def test_carray_rejects_count_exceeding_remaining():
    # count=1000 but only 3 payload bytes -> impossible -> raise.
    r = _Reader(struct.pack("<I", 1000) + b"\x01\x02\x03")
    with pytest.raises(ValueError):
        r.carray(_Reader.u8)


def test_carray_accepts_valid_count():
    # count=3 with exactly 3 payload bytes decodes normally; the guard
    # never fires on valid data (a real count is always <= remaining).
    r = _Reader(struct.pack("<I", 3) + bytes([10, 20, 30]))
    assert r.carray(_Reader.u8) == [10, 20, 30]


def test_carray_empty_is_fine():
    r = _Reader(struct.pack("<I", 0))
    assert r.carray(_Reader.u8) == []
