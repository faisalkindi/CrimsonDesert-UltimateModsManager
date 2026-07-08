"""Locate + byte-exact edit of gear stats in opaque iteminfo records.

Synthetic fixtures only (CI-runnable, no game data). The whole-table live
verification (adaptive whitelist -> locate -> edit -> byte-exact on the 3,341
opaque 1.13 gear records) is recorded in the PR; these pin the engine's
locate/precision/edit invariants.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.gear_stats import (
    GearStat,
    apply_stat_edit,
    build_stat_whitelist,
    edit_record_stats,
    is_gear_stat_field,
    locate_gear_stats,
    resolve_gear_stat_index,
)


def _list_i64(entries):
    b = struct.pack("<I", len(entries))
    for s, v in entries:
        b += struct.pack("<Iq", s, v)
    return b


def _list_i8(entries):
    b = struct.pack("<I", len(entries))
    for s, v in entries:
        b += struct.pack("<Ib", s, v)
    return b


def _esd(maxl, regen, static, lvl):
    # EnchantStatData = 3 i64 carrays + 1 i8 carray
    return _list_i64(maxl) + _list_i64(regen) + _list_i64(static) + _list_i8(lvl)


WL = frozenset({1000002, 1000003, 1000005})


# --- locate ---------------------------------------------------------------

def test_locate_finds_whitelisted_static_stats():
    block = _esd([], [], [(1000002, 1000), (1000003, 0)], [(1000005, 3)])
    rec = b"\xAA" * 40 + block + b"\xBB" * 40
    stats = locate_gear_stats(rec, WL)
    # only the three i64 lists are editable; the i8 level list is not exposed
    assert [(g.stat, g.value) for g in stats] == [(1000002, 1000), (1000003, 0)]
    # value_offset points exactly at the i64 value
    assert struct.unpack_from("<q", rec, stats[0].value_offset)[0] == 1000
    assert struct.unpack_from("<q", rec, stats[1].value_offset)[0] == 0


def test_block_with_unwhitelisted_stat_is_ignored():
    # 424242 is not in the whitelist -> the whole block is refused (no guessing)
    block = _esd([], [], [(1000002, 1000), (424242, 7)], [])
    rec = b"\x00" * 20 + block + b"\x00" * 20
    assert locate_gear_stats(rec, WL) == []


def test_no_stats_in_plain_bytes():
    # random-ish non-stat bytes -> nothing located
    rec = bytes((i * 37) % 256 for i in range(300))
    assert locate_gear_stats(rec, WL) == []


def test_multiple_blocks_located_in_order():
    b1 = _esd([], [], [(1000002, 111)], [])
    b2 = _esd([], [], [(1000005, 222)], [])
    rec = b"\x00" * 8 + b1 + b"\x00" * 8 + b2 + b"\x00" * 8
    stats = locate_gear_stats(rec, WL)
    assert [(g.stat, g.value) for g in stats] == [(1000002, 111), (1000005, 222)]


# --- adaptive whitelist ---------------------------------------------------

def test_whitelist_keeps_recurring_keys_drops_rare_ones():
    # stat 1000002 appears 12x, 1000003 10x -> kept; 555000 once -> dropped
    recs = [_esd([], [], [(1000002, 1), (1000003, 1)], []) for _ in range(10)]
    recs += [_esd([], [], [(1000002, 1)], []) for _ in range(2)]
    recs.append(_esd([], [], [(555000, 9)], []))
    wl = build_stat_whitelist(recs, min_freq=10)
    assert 1000002 in wl and 1000003 in wl
    assert 555000 not in wl


# --- edit is byte-exact ---------------------------------------------------

def test_apply_stat_edit_is_same_width_byte_exact():
    block = _esd([], [], [(1000002, 1000)], [])
    rec = b"\xAA" * 32 + block + b"\xBB" * 32
    g = locate_gear_stats(rec, WL)[0]
    out = apply_stat_edit(rec, g.value_offset, 999_999)
    assert len(out) == len(rec)                       # never shifts
    diff = [i for i in range(len(rec)) if rec[i] != out[i]]
    assert all(g.value_offset <= i < g.value_offset + 8 for i in diff)
    assert struct.unpack_from("<q", out, g.value_offset)[0] == 999_999
    # everything outside the 8-byte value is identical
    assert out[:g.value_offset] == rec[:g.value_offset]
    assert out[g.value_offset + 8:] == rec[g.value_offset + 8:]


def test_apply_then_relocate_sees_new_value():
    block = _esd([], [], [(1000002, 5)], [])
    rec = b"\x00" * 16 + block + b"\x00" * 16
    g = locate_gear_stats(rec, WL)[0]
    out = apply_stat_edit(rec, g.value_offset, -4200)
    g2 = locate_gear_stats(out, WL)[0]
    assert g2.stat == 1000002 and g2.value == -4200


def test_apply_rejects_out_of_range_offset():
    rec = b"\x00" * 20
    with pytest.raises(ValueError):
        apply_stat_edit(rec, 15, 1)          # 15+8 > 20


def test_apply_rejects_oversized_value():
    block = _esd([], [], [(1000002, 5)], [])
    rec = b"\x00" * 8 + block
    g = locate_gear_stats(rec, WL)[0]
    with pytest.raises(ValueError):
        apply_stat_edit(rec, g.value_offset, 2 ** 63)   # doesn't fit i64


# --- multi-edit convenience -----------------------------------------------

def test_edit_record_stats_composes_multiple_edits():
    block = _esd([], [], [(1000002, 100), (1000003, 200)], [])
    rec = b"\xAA" * 24 + block + b"\xBB" * 24
    out = edit_record_stats(rec, {0: 5000, 1: -1}, WL)
    assert len(out) == len(rec)
    stats = locate_gear_stats(out, WL)
    assert [(g.stat, g.value) for g in stats] == [(1000002, 5000), (1000003, -1)]


def test_edit_record_stats_bad_index_raises():
    block = _esd([], [], [(1000002, 1)], [])
    rec = b"\x00" * 12 + block
    with pytest.raises(KeyError):
        edit_record_stats(rec, {5: 1}, WL)


# --- Format 3 field addressing --------------------------------------------

def test_is_gear_stat_field():
    assert is_gear_stat_field("gear_stat[1000002]")
    assert is_gear_stat_field("gear_stat[0]")
    assert not is_gear_stat_field("price_list[0].price.price")
    assert not is_gear_stat_field("gear_stat")
    assert not is_gear_stat_field("")


def test_resolve_by_stat_key_and_by_index():
    located = [GearStat(1000002, 10, 100), GearStat(1000003, 20, 200)]
    # big number -> stat key (first match)
    assert resolve_gear_stat_index("gear_stat[1000003]", located) == 1
    # small number -> positional index
    assert resolve_gear_stat_index("gear_stat[0]", located) == 0
    # unknown stat key / out-of-range index -> None
    assert resolve_gear_stat_index("gear_stat[1099999]", located) is None
    assert resolve_gear_stat_index("gear_stat[9]", located) is None
