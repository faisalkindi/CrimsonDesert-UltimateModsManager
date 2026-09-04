"""The 4 September 2026 patch (buildid 25116796) broke store mods again.

Same failure mode as CD 1.16 and CD 1.16.1: the record shape moved and
nothing ahead of the const tripwire moved with it, so the tripwire never
fired. What the user saw was 16 of the 17 stores in donr484's "Shop
Smart. Shop H-Mart" dropped, each with a message naming the *mod*::

    store 3101: no span in this entry is a byte-exact CD 1.16 stock
    list anchored on the store key

CD 1.16 "won" detection with 17 of 436 entries read and 380 left
unexplained -- the least bad of five wrong answers. So this file pins
two things: the new layout, and that a winner that thin now refuses
outright instead of being handed to the writer.

THE CHANGE

Eight zero bytes appeared between ``sub_data`` and the ``effect_list``
count. It was derived by shift against the previous build's committed
table (``vanilla_b24994088``, buildid 24994088), which carries the same
436 entries: every entry grew by exactly ``1 + 8 * record_count``, and
for all 6,376 shared records the record either side of the eight bytes
is reproduced byte-exact. The ``+1`` is a byte in the entry's own fixed
part, which this module never reads -- the list is located, not
computed. Both halves of that derivation are asserted below, so the
next build can be diffed the same way.

Every number here was measured on the committed fixtures.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.storeinfo_native_parser import (
    LAYOUTS,
    ORDER_ELEM_SIZE,
    StoreinfoParseError,
    StoreListNotFound,
    _Reader,
    _score_layout,
    _unreadable_entries,
    detect_storeinfo_layout,
    locate_stock_list,
    read_stock_record,
    serialize_stock_list,
)
from cdumm.semantic.parser import parse_pabgh_index
from tests.fixture_loaders import (
    has_vanilla_b25116796,
    load_vanilla_b24994088,
    load_vanilla_b25116796,
)

pytestmark = pytest.mark.skipif(
    not has_vanilla_b25116796("storeinfo.pabgb"),
    reason="buildid 25116796 fixtures absent")

# Measured on tests/fixtures/vanilla_b25116796/storeinfo.
TOTAL_ENTRIES = 436
LOCATED_ENTRIES = 397
LOCATED_RECORDS = 6_376
PROVABLY_EMPTY_ENTRIES = 39
ORDER_ELEMENTS = 1_063
SUB_GAP = 8


def _payload(body: bytes, off: int) -> int:
    return off + 6 + struct.unpack_from("<I", body, off + 2)[0] + 1


@pytest.fixture(scope="module")
def table():
    body = load_vanilla_b25116796("storeinfo.pabgb")
    header = load_vanilla_b25116796("storeinfo.pabgh")
    _key_size, offsets = parse_pabgh_index(header, "storeinfo")
    return body, offsets


@pytest.fixture(scope="module")
def layout(table):
    body, offsets = table
    return detect_storeinfo_layout(body, sorted(offsets.values()))


def _locate_all(body, offsets, layout):
    spans = sorted(offsets.values()) + [len(body)]
    found, empty = {}, []
    for key, off in offsets.items():
        end = spans[spans.index(off) + 1]
        try:
            found[key] = locate_stock_list(
                body, _payload(body, off), end, key, layout)
        except StoreinfoParseError:
            empty.append(key)
    return found, empty


# -- the break, stated as numbers ---------------------------------------

def test_every_previous_layout_leaves_most_of_the_table_unexplained(table):
    body, offsets = table
    offs = sorted(offsets.values())
    for label, score in (("CD 1.16.1", (5, 5)), ("CD 1.16", (17, 30)),
                         ("CD 1.13", (11, 18)), ("CD 1.11", (0, 0)),
                         ("CD 1.10", (0, 0))):
        cand = next(c for c in LAYOUTS if c.label == label)
        assert _score_layout(body, offs, cand) == score, label
        assert _unreadable_entries(body, offs, cand) > LOCATED_ENTRIES / 2


def test_the_live_table_is_detected_as_the_new_build(layout):
    assert layout.label == "CD b25116796"
    assert layout.sub_gap_size == SUB_GAP
    # nothing ahead of the insertion moved: same head, same interior,
    # same const offset as CD 1.16.1 -- which is why the tripwire missed
    prev = next(c for c in LAYOUTS if c.label == "CD 1.16.1")
    assert layout.head_size == prev.head_size == 114
    assert layout.const_off == prev.const_off == 42
    assert (layout.raw_e_off, layout.raw_g_off,
            layout.raw_q_off) == (37, 53, 55)


def test_detection_beats_every_other_candidate(table, layout):
    body, offsets = table
    offs = sorted(offsets.values())
    best = (LOCATED_ENTRIES, LOCATED_RECORDS)
    assert _score_layout(body, offs, layout) == best
    assert _unreadable_entries(body, offs, layout) == 0
    for cand in LAYOUTS:
        if cand is not layout:
            assert _score_layout(body, offs, cand) < best


# -- every entry is accounted for ---------------------------------------

def test_every_located_entry_round_trips_byte_exact(table, layout):
    body, offsets = table
    found, empty = _locate_all(body, offsets, layout)
    for recs, start, end in found.values():
        assert serialize_stock_list(recs, layout) == body[start:end]
    assert len(found) == LOCATED_ENTRIES
    assert sum(len(r) for r, _s, _e in found.values()) == LOCATED_RECORDS
    assert len(empty) == PROVABLY_EMPTY_ENTRIES


def test_no_entry_is_left_unexplained(table, layout):
    body, offsets = table
    found, empty = _locate_all(body, offsets, layout)
    assert len(found) + len(empty) == TOTAL_ENTRIES == len(offsets)
    spans = sorted(offsets.values()) + [len(body)]
    for key in empty:
        off = offsets[key]
        with pytest.raises(StoreListNotFound) as ei:
            locate_stock_list(body, _payload(body, off),
                              spans[spans.index(off) + 1], key, layout)
        assert ei.value.provably_empty


def test_the_anchor_and_the_order_lists_still_hold(table, layout):
    body, offsets = table
    found, _empty = _locate_all(body, offsets, layout)
    for key, (recs, _s, _e) in found.items():
        assert recs[0].lookup_a == key
    seen = [el for recs, _s, _e in found.values()
            for rec in recs for el in rec.effect_list]
    assert len(seen) == ORDER_ELEMENTS
    assert all(len(el) == ORDER_ELEM_SIZE for el in seen)


def test_the_new_block_is_zero_in_every_shipped_record(table, layout):
    """Why it is carried verbatim instead of decoded: the game ships no
    non-zero value to name a field from."""
    body, offsets = table
    found, _empty = _locate_all(body, offsets, layout)
    for recs, _s, _e in found.values():
        for rec in recs:
            assert rec.sub_gap == bytes(SUB_GAP)


# -- the derivation, held to both builds --------------------------------

def _both_builds():
    return (load_vanilla_b25116796("storeinfo.pabgb"),
            load_vanilla_b25116796("storeinfo.pabgh"),
            load_vanilla_b24994088("storeinfo.pabgb"),
            load_vanilla_b24994088("storeinfo.pabgh"))


def test_growth_against_the_previous_build_is_one_plus_eight_per_record():
    """The measurement the layout came from. Same 436 entries on both
    builds; each grew by exactly 1 + 8 * record_count."""
    new_b, new_h, old_b, old_h = _both_builds()
    _ks, new_offs = parse_pabgh_index(new_h, "storeinfo")
    _ks, old_offs = parse_pabgh_index(old_h, "storeinfo")
    assert set(new_offs) == set(old_offs)

    prev = next(c for c in LAYOUTS if c.label == "CD 1.16.1")
    new_spans = sorted(new_offs.values()) + [len(new_b)]
    old_spans = sorted(old_offs.values()) + [len(old_b)]
    checked = 0
    for key in old_offs:
        no, oo = new_offs[key], old_offs[key]
        n_end = new_spans[new_spans.index(no) + 1]
        o_end = old_spans[old_spans.index(oo) + 1]
        try:
            recs, _s, _e = locate_stock_list(
                old_b, _payload(old_b, oo), o_end, key, prev)
        except StoreListNotFound as exc:
            assert exc.provably_empty
            assert (n_end - no) - (o_end - oo) == 1
            continue
        assert (n_end - no) - (o_end - oo) == 1 + SUB_GAP * len(recs)
        checked += 1
    assert checked == LOCATED_ENTRIES


def test_each_record_is_the_old_one_with_eight_bytes_at_the_effect_count():
    """Where the eight bytes went, by shift rather than by first
    difference: keep the record up to the effect_list count, insert
    eight, keep the rest. 62 records also changed raw_a and 37 changed
    sub_data -- shipped values, not shape -- so the assertion is on the
    span from the effect count onward, which no data change touches.
    """
    new_b, new_h, old_b, old_h = _both_builds()
    _ks, new_offs = parse_pabgh_index(new_h, "storeinfo")
    _ks, old_offs = parse_pabgh_index(old_h, "storeinfo")
    prev = next(c for c in LAYOUTS if c.label == "CD 1.16.1")
    old_spans = sorted(old_offs.values()) + [len(old_b)]

    compared = 0
    for key in sorted(old_offs):
        oo, no = old_offs[key], new_offs[key]
        o_end = old_spans[old_spans.index(oo) + 1]
        try:
            recs, o_start, _e = locate_stock_list(
                old_b, _payload(old_b, oo), o_end, key, prev)
        except StoreListNotFound:
            continue
        # the list sits at the same place, one byte further in
        n_start = no + (o_start - oo) + 1
        assert struct.unpack_from("<I", new_b, n_start)[0] == len(recs)

        r = _Reader(old_b, o_start + 4)
        pos = n_start + 4
        for rec in recs:
            start = r.pos
            read_stock_record(r, prev)
            old_rec = old_b[start:r.pos]
            new_rec = new_b[pos:pos + len(old_rec) + SUB_GAP]
            pos += len(old_rec) + SUB_GAP
            # offset of the effect_list count within the old record
            cut = len(old_rec) - 4 - len(rec.effect_list) * ORDER_ELEM_SIZE
            assert new_rec[cut:cut + SUB_GAP] == bytes(SUB_GAP)
            assert new_rec[cut + SUB_GAP:] == old_rec[cut:]
            compared += 1
    assert compared == LOCATED_RECORDS


# -- the guard that would have named the real cause ---------------------

def test_a_layout_that_explains_a_minority_of_the_table_is_refused(table):
    """What the user should have seen instead of 16 per-store refusals.

    Detection is comparative, so on an unmodelled build it always has a
    "winner". Simulated by hiding the real layout: CD 1.16 then wins with
    17 entries read and 380 unexplained, which is the state that shipped.
    """
    import cdumm.engine.storeinfo_native_parser as mod

    body, offsets = table
    offs = sorted(offsets.values())
    real = next(c for c in LAYOUTS if c.label == "CD b25116796")
    saved = mod.LAYOUTS
    mod.LAYOUTS = tuple(c for c in saved if c is not real)
    try:
        with pytest.raises(StoreinfoParseError) as ei:
            detect_storeinfo_layout(body, offs)
    finally:
        mod.LAYOUTS = saved
    msg = str(ei.value)
    assert "shape has changed again" in msg
    assert "17 of 436" in msg and "380" in msg


# -- the write path, on the build that broke it -------------------------

def test_the_mod_that_broke_applies_to_every_store_it_targets():
    """donr484's intent shape, against the live table: 16 of these 17
    stores were dropped before the layout existed."""
    from types import SimpleNamespace

    from cdumm.engine.storeinfo_writer import build_storeinfo_changes

    body = load_vanilla_b25116796("storeinfo.pabgb")
    header = load_vanilla_b25116796("storeinfo.pabgh")
    _ks, offsets = parse_pabgh_index(header, "storeinfo")
    layout = detect_storeinfo_layout(body, sorted(offsets.values()))
    spans = sorted(offsets.values()) + [len(body)]

    stores = [k for k in sorted(offsets)
              if k in (201, 301, 401, 601, 701, 1122, 1302, 1611, 2004,
                       3101, 3112, 3113, 3803, 4002)]
    before = {}
    for key in stores:
        off = offsets[key]
        recs, _s, _e = locate_stock_list(
            body, _payload(body, off), spans[spans.index(off) + 1],
            key, layout)
        before[key] = recs

    # one appended record per store, in the shape Format 3 store mods ship
    intents = [
        SimpleNamespace(
            key=key, entry="", op="array_append", field="stock_data_list",
            match=None, clone=None, old=None,
            new={"effect_list": [], "flag_a": 1, "flag_b": 0, "flag_c": 1,
                 "is_restore_item": 0, "lookup_a": key, "lookup_b": 0,
                 "lookup_c": 0, "low_price_threshold_count_116": 4294967295,
                 "order_index_113": 4294967295, "raw_a": 1000000,
                 "raw_b": 1000000, "raw_c": 99, "raw_d": 0, "raw_e": 0,
                 "sub_data": None,
                 "value": {"disc": 0, "raw_e": 1, "raw_g": 65535,
                           "raw_q": 1003828,
                           "payload": {"body": 1003828, "type": "Disc0"}}})
        for key in stores]

    changes, pabgh = build_storeinfo_changes(body, header, intents)
    assert len(changes) == len(stores) and pabgh is not None

    buf = bytearray(body)
    for c in sorted(changes, key=lambda c: -c["offset"]):
        off, orig = c["offset"], bytes.fromhex(c["original"])
        assert buf[off:off + len(orig)] == orig
        buf[off:off + len(orig)] = bytes.fromhex(c["patched"])
    new_body = bytes(buf)
    new_header = bytes.fromhex(pabgh["patched"])

    _ks, new_offsets = parse_pabgh_index(new_header, "storeinfo")
    assert detect_storeinfo_layout(
        new_body, sorted(new_offsets.values())).label == layout.label
    new_spans = sorted(new_offsets.values()) + [len(new_body)]
    for key in stores:
        off = new_offsets[key]
        recs, start, end = locate_stock_list(
            new_body, _payload(new_body, off),
            new_spans[new_spans.index(off) + 1], key, layout)
        assert len(recs) == len(before[key]) + 1
        assert recs[:-1] == before[key]          # untouched slots verbatim
        assert recs[-1].body == 1003828
        assert recs[-1].sub_gap == bytes(SUB_GAP)
        assert serialize_stock_list(recs, layout) == new_body[start:end]
