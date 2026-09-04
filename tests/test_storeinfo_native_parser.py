"""storeinfo stock-list parser (GitHub #183 groundwork).

The trust anchor mirrors the iteminfo native parser: parse +
serialize on the live extracted storeinfo.pabgb must produce
byte-identical output, or applying a stock_data_list intent would
corrupt the file (the game crashes on store open with a corrupt
storeinfo body). The live-fixture tests skip when the extracted
vanilla file is not present; the synthetic tests always run.

Layout pinning (GitHub #351)
----------------------------
The fixed-offset tests below MUST pass the layout they were written
against. ``parse_stock_list``'s layout parameter defaults to
``DEFAULT_LAYOUT``, which is deliberately the *newest* build -- so a
test that omits it silently re-points at whatever layout ships next.
That is what #351 was: this file's ``issue_repro`` snapshot is from the
CD 1.11 era, the default had moved on to CD 1.16, and a 1.11 table was
being parsed as 1.16. CD 1.11 puts the const byte at record offset 34
and CD 1.16 puts it at 42, which was exactly the byte in the error:

    const byte at record offset 42 is 0 (expected 1)

It read as a live regression -- "the format drifted, store mods are
dead" -- and it was neither. Nothing about that snapshot changes when
the game updates.

Two things follow, and both are done here. The fixed-offset tests name
their layout explicitly. And the coverage that actually matters no
longer depends on an untracked snapshot at all: it runs against the
committed ``vanilla113`` / ``vanilla116`` fixtures through
``locate_stock_list``, which is the path production uses and which
takes no offset constant. That is audit finding C7 again (see
``tests/fixture_loaders``) -- a proof gated on a machine-local path is
a proof that runs nowhere, least of all in CI.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from cdumm.engine.storeinfo_native_parser import (
    LAYOUTS,
    ORDER_ELEM_SIZE,
    StockRecord,
    StoreinfoParseError,
    StoreListNotFound,
    _unreadable_entries,
    locate_stock_list,
    parse_stock_list,
    serialize_stock_list,
)
from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index
from tests.fixture_loaders import (
    has_vanilla113,
    has_vanilla116,
    has_vanilla1161,
    load_vanilla113,
    load_vanilla116,
    load_vanilla1161,
)

_VANILLA_DIR = Path(__file__).resolve().parents[1] / "issue_repro" / "183" / "vanilla"
_LIVE_BODY = _VANILLA_DIR / "storeinfo.pabgb"
_LIVE_HEADER = _VANILLA_DIR / "storeinfo.pabgh"

_BY_LABEL = {ly.label: ly for ly in LAYOUTS}

#: The build the ``issue_repro/183`` snapshot was extracted from. Pinned
#: rather than defaulted -- see the module docstring.
_FIXTURE_LAYOUT = _BY_LABEL["CD 1.11"]


def _have_live_fixture() -> bool:
    return _LIVE_BODY.exists() and _LIVE_HEADER.exists()


# ── Synthetic round-trip (always runs) ───────────────────────────────


def _sample_records() -> list[StockRecord]:
    return [
        StockRecord(lookup_a=3101, raw_a=1_000_000, raw_b=1_000_000,
                    raw_c=1, body=6001,
                    sub_data={"flag": 0, "lookup_a": 4294967061,
                              "lookup_b": 0, "lookup_c": 0}),
        StockRecord(lookup_a=3101, raw_a=1_000_000, raw_b=1_000_000,
                    raw_d=1, raw_e=1, flag_a=1, body=1_003_172,
                    sub_data=None),
    ]


@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda ly: ly.label)
def test_synthetic_round_trip(layout):
    """Round-trips in EVERY layout, not just whichever one is the default.

    This used to hardcode head == 110 (CD 1.11). When CD 1.13 moved the
    record shape, that hardcoding is precisely what made the test agree
    with a parser that could no longer read the game — so it is now driven
    off the layout under test.
    """
    recs = _sample_records()
    blob = serialize_stock_list(recs, layout)
    # count + rec0 (head + sub_data flag + 13 + sub_gap + effect u32)
    #       + rec1 (head + sub_data flag +  0 + sub_gap + effect u32)
    head, gap = layout.head_size, layout.sub_gap_size
    assert len(blob) == (4 + (head + 1 + 13 + gap + 4)
                         + (head + 1 + gap + 4))
    parsed, start, end = parse_stock_list(blob, 0, layout)
    assert (start, end) == (0, len(blob))
    assert serialize_stock_list(parsed, layout) == blob
    assert parsed[0].sub_data == recs[0].sub_data
    assert parsed[1].sub_data is None
    assert [r.body for r in parsed] == [6001, 1_003_172]


@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda ly: ly.label)
def test_refuses_unknown_sub_data_flag(layout):
    blob = bytearray(serialize_stock_list([_sample_records()[1]], layout))
    # Corrupt the sub_data optional flag (count u32 + the record head).
    blob[4 + layout.head_size] = 7
    with pytest.raises(StoreinfoParseError, match="optional flag is 7"):
        parse_stock_list(bytes(blob), 0, layout)


@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda ly: ly.label)
def test_non_empty_effect_list_round_trips(layout):
    """``_orderCountDataList`` used to be refused outright, which cost 15
    entries of the 1.13 table. Its element is ORDER_ELEM_SIZE bytes
    (derived by exact tiling) and is carried verbatim."""
    rec = _sample_records()[1]
    rec.effect_list = [bytes(range(ORDER_ELEM_SIZE)),
                       b"\xff" * ORDER_ELEM_SIZE]
    blob = serialize_stock_list([rec], layout)
    assert len(blob) == (4 + layout.head_size + 1 + layout.sub_gap_size + 4
                         + 2 * ORDER_ELEM_SIZE)
    parsed, _s, _e = parse_stock_list(blob, 0, layout)
    assert parsed[0].effect_list == rec.effect_list
    assert serialize_stock_list(parsed, layout) == blob


def test_refuses_a_wrongly_sized_effect_element_on_serialize():
    """The elements are opaque, so the ONE thing we can still check is
    that they are the right width — a short one would silently shift
    every following record."""
    rec = _sample_records()[1]
    rec.effect_list = [b"\x00" * (ORDER_ELEM_SIZE - 1)]
    with pytest.raises(StoreinfoParseError, match="opaque bytes"):
        serialize_stock_list([rec])


def test_refuses_an_implausible_effect_count_on_parse():
    """A huge count means we are misaligned, not that the record has
    four billion order entries."""
    blob = bytearray(serialize_stock_list([_sample_records()[1]]))
    struct.pack_into("<I", blob, len(blob) - 4, 0xFFFFFF)
    with pytest.raises(StoreinfoParseError, match="implausible"):
        parse_stock_list(bytes(blob), 0)


# ── Live-fixture round-trip (the trust anchor) ───────────────────────


def _entry_payload_offsets():
    from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index
    body = _LIVE_BODY.read_bytes()
    key_size, offsets = parse_pabgh_index(
        _LIVE_HEADER.read_bytes(), "storeinfo")
    spans = sorted(offsets.values()) + [len(body)]
    out = {}
    for key, off in offsets.items():
        _, _, payload = _parse_entry_header(body, off, key_size)
        end = spans[spans.index(off) + 1]
        out[key] = (payload, end)
    return body, out


@pytest.mark.skipif(not _have_live_fixture(),
                    reason="extracted vanilla storeinfo fixture not present")
def test_live_entry_3101_round_trips_byte_exact():
    """Entry 3101 is the #183 mod's target. On the CD 1.11 build this
    snapshot came from it has 38 records (one more than the pre-patch
    37); they must survive parse + serialize byte-identically. Also pins
    the 1.11 layout: const33==1 and is_restore_item in {0,1} for every
    record.

    ``_FIXTURE_LAYOUT`` is passed explicitly. Omitting it is #351: the
    default is the newest layout, not this snapshot's.
    """
    body, entries = _entry_payload_offsets()
    payload, _end = entries[3101]
    count_off = payload + _FIXTURE_LAYOUT.count_payload_offset
    records, start, end = parse_stock_list(body, count_off, _FIXTURE_LAYOUT)
    assert len(records) == 38
    assert serialize_stock_list(records, _FIXTURE_LAYOUT) == body[start:end]
    assert all(r.const33 == 1 for r in records)
    assert all(r.is_restore_item in (0, 1) for r in records)


@pytest.mark.skipif(not _have_live_fixture(),
                    reason="extracted vanilla storeinfo fixture not present")
def test_live_full_file_clean_entries_round_trip():
    """Every entry the parser accepts must round-trip byte-exact.
    Entries it cannot handle yet (disc-variant value payloads or
    non-empty effect lists) must raise — never mis-parse silently.
    On the CD 1.11 build this snapshot came from, 268 of 293 entries are
    clean."""
    body, entries = _entry_payload_offsets()
    ok = failed = refused = 0
    for key, (payload, end) in entries.items():
        count_off = payload + _FIXTURE_LAYOUT.count_payload_offset
        if count_off + 4 > end:
            refused += 1
            continue
        try:
            records, start, lend = parse_stock_list(
                body, count_off, _FIXTURE_LAYOUT)
        except (StoreinfoParseError, struct.error, IndexError):
            refused += 1
            continue
        if serialize_stock_list(records, _FIXTURE_LAYOUT) == body[start:lend]:
            ok += 1
        else:
            failed += 1
    assert failed == 0, f"{failed} entries mis-round-tripped"
    assert ok >= 260, f"only {ok} entries round-tripped (expected >=260)"


# ── Committed-fixture coverage: runs everywhere, including CI ─────────
#
# The two tests above are gated on an untracked snapshot, so on a fresh
# clone and on the CI runner they skip. Everything below reads fixtures
# that are in the repo, and goes through the production locate path, so
# a real storeinfo regression has something that actually fails.

_COMMITTED = (
    ("vanilla113", "CD 1.13", 293, 263, 5443, 30),
    ("vanilla116", "CD 1.16", 432, 397, 6376, 35),
    # GitHub #365. The 15 Aug 2026 patch took 4 bytes out of the opaque
    # `vgap` interior (71 -> 67) without moving anything ahead of the
    # const tripwire, so this build needs its own layout even though
    # every offset up to the const is identical to CD 1.16.
    ("vanilla1161", "CD 1.16.1", 437, 398, 6378, 39),
)


def _committed_entries(load, ver: str):
    body = load("storeinfo.pabgb")
    header = load("storeinfo.pabgh")
    key_size, offsets = parse_pabgh_index(header, "storeinfo")
    spans = sorted(offsets.values()) + [len(body)]
    out = {}
    for key, off in offsets.items():
        _, _, payload = _parse_entry_header(body, off, key_size)
        out[key] = (payload, spans[spans.index(off) + 1])
    return body, out


def _walk_committed(ver: str, layout):
    load = {"vanilla113": load_vanilla113,
            "vanilla116": load_vanilla116,
            "vanilla1161": load_vanilla1161}[ver]
    body, entries = _committed_entries(load, ver)
    located = records = empty = not_found = 0
    for key, (payload, end) in entries.items():
        try:
            recs, start, lend = locate_stock_list(
                body, payload, end, key, layout)
        except StoreListNotFound as exc:
            # locate_stock_list distinguishes "provably empty" from
            # "could not read", and that distinction is the point.
            if "too" in str(exc) or "provably" in str(exc):
                empty += 1
            else:
                not_found += 1
            continue
        located += 1
        records += len(recs)
        assert serialize_stock_list(recs, layout) == body[start:lend], (
            f"{ver} store {key} did not round-trip byte-exact")
    return located, records, empty, not_found, len(entries)


@pytest.mark.parametrize(
    "ver,label,n_entries,exp_located,exp_records,exp_empty",
    _COMMITTED, ids=[c[0] for c in _COMMITTED])
def test_committed_fixture_locates_and_round_trips(
        ver, label, n_entries, exp_located, exp_records, exp_empty):
    """The whole table is accounted for, and every record round-trips.

    ``located + provably-empty == entries`` with ``not_found == 0`` is
    the assertion that would have contradicted #351's "store mods are
    dead" reading immediately: the parser reads the current table
    completely. Counts are pinned so a real drift moves them.
    """
    _have = {"vanilla113": has_vanilla113, "vanilla116": has_vanilla116,
             "vanilla1161": has_vanilla1161}[ver]
    if not _have("storeinfo.pabgb"):
        pytest.skip(f"{ver} storeinfo fixture absent")

    located, records, empty, not_found, total = _walk_committed(
        ver, _BY_LABEL[label])

    assert not_found == 0, f"{not_found} entries could not be located"
    assert located == exp_located
    assert records == exp_records
    assert empty == exp_empty
    assert located + empty == total == n_entries


@pytest.mark.parametrize(
    "ver,label,n_entries,exp_located,exp_records,exp_empty",
    _COMMITTED, ids=[c[0] for c in _COMMITTED])
def test_older_layouts_lose_decisively_on_committed_fixture(
        ver, label, n_entries, exp_located, exp_records, exp_empty):
    """Detection is not a close call, so a wrong layout cannot win.

    Asserted on UNREADABLE entries, which is what ``detect_storeinfo_layout``
    ranks on, rather than on how many entries a layout locates.

    Located-count stopped separating layouts on CD 1.16.1 (GitHub #365):
    CD 1.13 locates 320 of that table's 437 against CD 1.16.1's 398, a
    margin of 1.24x where every earlier build gave 100x or more, so the
    old "must locate less than half" assertion fails there on a correct
    fix. Holes separate them cleanly -- 78 against 0 -- because a layout
    that abandons entries the other one reads has not understood the
    table better however many records it got through.

    If a future build ever ties on holes too, detection can no longer
    tell those layouts apart and the tie must be resolved rather than
    broken silently by ordering, which is precisely how the #352 no-op
    change looked like an improvement.
    """
    _have = {"vanilla113": has_vanilla113, "vanilla116": has_vanilla116,
             "vanilla1161": has_vanilla1161}[ver]
    if not _have("storeinfo.pabgb"):
        pytest.skip(f"{ver} storeinfo fixture absent")

    load = {"vanilla113": load_vanilla113, "vanilla116": load_vanilla116,
            "vanilla1161": load_vanilla1161}[ver]
    body = load("storeinfo.pabgb")
    _ks, offsets = parse_pabgh_index(load("storeinfo.pabgh"), "storeinfo")
    starts = sorted(offsets.values())

    right = _unreadable_entries(body, starts, _BY_LABEL[label])
    assert right == 0, (
        f"{ver}: its own layout {label!r} leaves {right} entries "
        f"unreadable; it does not fully explain the table")
    for other in LAYOUTS:
        if other.label == label:
            continue
        got = _unreadable_entries(body, starts, other)
        assert got > right, (
            f"{ver}: layout {other.label!r} leaves {got} entries "
            f"unreadable against {label!r}'s {right} — detection is no "
            f"longer decisive")


@pytest.mark.skipif(
    not (has_vanilla116("storeinfo.pabgb") and has_vanilla1161("storeinfo.pabgb")),
    reason="vanilla116/vanilla1161 storeinfo fixtures absent")
def test_vgap_shift_is_uniformly_at_37():
    """GitHub #365: the CD 1.16.1 interior shrink is one point, not two.

    Naive first-byte-divergence between the CD 1.16 (71-byte) and CD
    1.16.1 (67-byte) interior lands at index 37 for 4,749 of the 6,376
    records shared between the two builds and at 53 for the other 1,627
    -- which is what originally made this look like two different
    layouts needing two different ``raw_e``/``raw_g``/``raw_q``
    mappings (see StoreLayout.raw_e_off).

    It isn't. This pins the stronger claim directly: shifting at 37 --
    keep vgap[:37], drop exactly 4 bytes, keep the rest -- reproduces
    the CD 1.16 interior byte-exact for EVERY one of the 6,376 shared
    records, with zero exceptions. The 1,627 that merely *look*
    consistent with a shift at 53 too are not a second layout; they are
    records whose vgap[37:57] is all zero in both builds, so shifting at
    53 is also consistent for them by coincidence, never because
    shifting at 37 is wrong for them.
    """
    layout116 = _BY_LABEL["CD 1.16"]
    layout1161 = _BY_LABEL["CD 1.16.1"]
    body116, entries116 = _committed_entries(load_vanilla116, "vanilla116")
    body1161, entries1161 = _committed_entries(load_vanilla1161, "vanilla1161")

    def _records(body, entries, layout):
        by_store: dict[int, dict[int, StockRecord]] = {}
        for key, (payload, end) in entries.items():
            try:
                recs, _s, _e = locate_stock_list(body, payload, end, key, layout)
            except StoreListNotFound:
                continue
            by_store[key] = {}
            for r in recs:
                by_store[key].setdefault(r.body, []).append(r)
        return by_store

    stores116 = _records(body116, entries116, layout116)
    stores1161 = _records(body1161, entries1161, layout1161)

    common_stores = set(stores116) & set(stores1161)
    assert len(common_stores) == 397

    checked = 0
    zero_window_ambiguous = 0
    for key in common_stores:
        pool1161 = {b: list(rs) for b, rs in stores1161[key].items()}
        for body_id, recs116 in stores116[key].items():
            cands = pool1161.get(body_id)
            if not cands:
                continue
            for r116 in recs116:
                if not cands:
                    break
                r1161 = cands.pop(0)
                checked += 1
                v116, v1161 = r116.vgap, r1161.vgap
                assert v116[:37] == v1161[:37] and v116[41:] == v1161[37:], (
                    f"store {key} body {body_id}: shift-at-37 does not "
                    f"reproduce the CD 1.16 interior byte-exact -- a "
                    f"real second layout, not zero-padding coincidence")
                window = v116[37:57]
                if v116[:53] == v1161[:53] and v116[57:] == v1161[53:]:
                    assert not any(window), (
                        f"store {key} body {body_id}: shift-at-53 is "
                        f"ALSO consistent but vgap[37:57] is non-zero "
                        f"({window.hex()}) -- this would be genuine "
                        f"evidence of a second layout")
                    zero_window_ambiguous += 1

    assert checked == 6376
    assert zero_window_ambiguous == 1627


def test_parsing_an_older_shape_with_the_default_layout_refuses():
    """The #351 bug class, pinned so it cannot come back silently.

    A CD 1.11-shaped list parsed under the newest layout must RAISE, not
    return plausible-looking records. This is what makes omitting the
    layout argument a loud failure rather than a wrong answer — and it
    needs no fixture, so it runs everywhere.
    """
    old = _BY_LABEL["CD 1.11"]
    blob = serialize_stock_list(_sample_records(), old)

    # Sanity: it round-trips under its own layout.
    recs, _s, _e = parse_stock_list(blob, 0, old)
    assert len(recs) == 2

    # Under the newest layout the const tripwire is at a different record
    # offset, so the walk must be refused.
    newest = LAYOUTS[0]
    assert newest.const_off != old.const_off, (
        "this test needs two layouts whose const byte differs")
    with pytest.raises((StoreinfoParseError, struct.error, IndexError)):
        parse_stock_list(blob, 0, newest)
