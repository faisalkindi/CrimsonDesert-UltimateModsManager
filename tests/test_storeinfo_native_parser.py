"""storeinfo stock-list parser (GitHub #183 groundwork).

The trust anchor mirrors the iteminfo native parser: parse +
serialize on the live extracted storeinfo.pabgb must produce
byte-identical output, or applying a stock_data_list intent would
corrupt the file (the game crashes on store open with a corrupt
storeinfo body). The live-fixture tests skip when the extracted
vanilla file is not present; the synthetic tests always run.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from cdumm.engine.storeinfo_native_parser import (
    LIST_COUNT_PAYLOAD_OFFSET,
    StockRecord,
    StoreinfoParseError,
    parse_stock_list,
    serialize_stock_list,
)

_VANILLA_DIR = Path(__file__).resolve().parents[1] / "issue_repro" / "183" / "vanilla"
_LIVE_BODY = _VANILLA_DIR / "storeinfo.pabgb"
_LIVE_HEADER = _VANILLA_DIR / "storeinfo.pabgh"


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


def test_synthetic_round_trip():
    recs = _sample_records()
    blob = serialize_stock_list(recs)
    # 4 (count) + rec0 with sub_data (110+1+13+4) + rec1 without (110+1+4).
    # Head is 110 since CD 1.11 added the is_restore_item u8 (#183).
    assert len(blob) == 4 + 128 + 115
    parsed, start, end = parse_stock_list(blob, 0)
    assert (start, end) == (0, len(blob))
    assert serialize_stock_list(parsed) == blob
    assert parsed[0].sub_data == recs[0].sub_data
    assert parsed[1].sub_data is None
    assert [r.body for r in parsed] == [6001, 1_003_172]


def test_refuses_unknown_sub_data_flag():
    blob = bytearray(serialize_stock_list([_sample_records()[1]]))
    # Corrupt the sub_data optional flag (count 4B + head 110B).
    blob[4 + 110] = 7
    with pytest.raises(StoreinfoParseError, match="optional flag is 7"):
        parse_stock_list(bytes(blob), 0)


def test_refuses_non_empty_effect_list_on_parse():
    blob = bytearray(serialize_stock_list([_sample_records()[1]]))
    # effect_list count is the trailing u32 of the record.
    struct.pack_into("<I", blob, len(blob) - 4, 3)
    with pytest.raises(StoreinfoParseError, match="effect_list has 3"):
        parse_stock_list(bytes(blob), 0)


def test_refuses_non_empty_effect_list_on_serialize():
    rec = _sample_records()[1]
    rec.effect_list = [object()]
    with pytest.raises(StoreinfoParseError, match="non-empty effect_list"):
        serialize_stock_list([rec])


# ── Live-fixture round-trip (the trust anchor) ───────────────────────


def _entry_payload_offsets():
    from cdumm.semantic.parser import parse_pabgh_index, _parse_entry_header
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
    """Entry 3101 is the #183 mod's target. On the current CD 1.11
    build it has 38 records (one more than the pre-patch 37); they must
    survive parse + serialize byte-identically. Also pins the 1.11
    layout: const33==1 and is_restore_item in {0,1} for every record."""
    body, entries = _entry_payload_offsets()
    payload, _end = entries[3101]
    count_off = payload + LIST_COUNT_PAYLOAD_OFFSET
    records, start, end = parse_stock_list(body, count_off)
    assert len(records) == 38
    assert serialize_stock_list(records) == body[start:end]
    assert all(r.const33 == 1 for r in records)
    assert all(r.is_restore_item in (0, 1) for r in records)


@pytest.mark.skipif(not _have_live_fixture(),
                    reason="extracted vanilla storeinfo fixture not present")
def test_live_full_file_clean_entries_round_trip():
    """Every entry the parser accepts must round-trip byte-exact.
    Entries it cannot handle yet (disc-variant value payloads or
    non-empty effect lists) must raise — never mis-parse silently.
    On the current CD 1.11 build, 268 of 293 entries are clean."""
    body, entries = _entry_payload_offsets()
    ok = failed = refused = 0
    for key, (payload, end) in entries.items():
        count_off = payload + LIST_COUNT_PAYLOAD_OFFSET
        if count_off + 4 > end:
            refused += 1
            continue
        try:
            records, start, lend = parse_stock_list(body, count_off)
        except (StoreinfoParseError, struct.error, IndexError):
            refused += 1
            continue
        if serialize_stock_list(records) == body[start:lend]:
            ok += 1
        else:
            failed += 1
    assert failed == 0, f"{failed} entries mis-round-tripped"
    assert ok >= 260, f"only {ok} entries round-tripped (expected >=260)"
