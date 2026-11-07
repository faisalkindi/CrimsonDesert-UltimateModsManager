"""Game 2.04 writes a record's name before its key (#458).

Up to 2.03 every .pabgb record opens ``[key][u32 name_len][name]``; 2.04
writes ``[u32 name_len][name][key]`` and changes nothing after them.

Each test builds a 2.04 table from a committed pre-2.04 fixture by
swapping the two fields in every record -- which is exactly, and only,
what 2.04 changed. The oracle is the original: a reader handed the
name-first copy and told so must read the same records, at the same
payload offsets, as it reads from the fixture. The copy is the same size
and uses the fixture's own .pabgh unchanged. The real 2.04.08 tables in
fixtures/vanilla20408 are name first already, so they're left out here
(test_build_layouts.py reads them).
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from cdumm.engine.format3_apply import _payload_offset
from cdumm.engine.schema_verify import decode_score, verified_order
from cdumm.engine.statusgroupinfo_writer import parse_record_full
from cdumm.engine.storeinfo_native_parser import (
    _entry_payload,
    detect_storeinfo_layout,
    locate_stock_list,
)
from cdumm.semantic.parser import (
    _parse_entry_header,
    parse_pabgh_index,
    parse_records,
)
from cdumm.semantic.record_header import (
    KEY_FIRST,
    NAME_FIRST,
    read_header,
    table_header_order,
)
from tests.fixture_loaders import to_name_first

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(build: str, table: str) -> tuple[bytes, bytes]:
    d = _FIXTURES / build
    return (zlib.decompress((d / f"{table}.pabgb.zlib").read_bytes()),
            zlib.decompress((d / f"{table}.pabgh.zlib").read_bytes()))


#: Fixture folders of game 2.04 itself: name first, not copies to make.
_NAME_FIRST_BUILDS = frozenset({"vanilla20408"})


def _fixture_tables() -> list[tuple[str, str]]:
    out = []
    for d in sorted(p for p in _FIXTURES.iterdir() if p.is_dir()
                    and p.name not in _NAME_FIRST_BUILDS):
        for b in sorted(d.glob("*.pabgb.zlib")):
            table = b.name.split(".")[0]
            if (d / f"{table}.pabgh.zlib").exists():
                out.append((d.name, table))
    return out


_ALL = _fixture_tables()


def test_the_fixture_set_is_not_empty():
    assert len(_ALL) >= 25, _ALL


@pytest.mark.parametrize("build,table", _ALL)
def test_committed_fixtures_are_key_first_and_their_copies_name_first(build, table):
    body, header = _load(build, table)
    ks, offs = parse_pabgh_index(header, table)
    assert table_header_order(body, offs, ks) == KEY_FIRST
    nf = to_name_first(body, header, table)
    assert table_header_order(nf, offs, ks) == NAME_FIRST


@pytest.mark.parametrize("build,table", _ALL)
def test_entry_header_reads_the_same_record_either_way(build, table):
    body, header = _load(build, table)
    ks, offs = parse_pabgh_index(header, table)
    nf = to_name_first(body, header, table)
    for key, off in offs.items():
        want = _parse_entry_header(body, off, ks)
        assert _parse_entry_header(nf, off, ks, NAME_FIRST) == want, (table, key)
        for no_null_skip in (False, True):
            assert (_payload_offset(nf, off, ks, no_null_skip=no_null_skip,
                                    header_order=NAME_FIRST)
                    == _payload_offset(body, off, ks,
                                       no_null_skip=no_null_skip)), (table, key)


@pytest.mark.parametrize("build,table", _ALL)
def test_parse_records_is_identical_on_the_name_first_copy(build, table):
    body, header = _load(build, table)
    nf = to_name_first(body, header, table)
    assert parse_records(table, nf, header) == parse_records(table, body, header)


@pytest.mark.parametrize("build", ["vanilla110", "vanilla113", "vanilla116",
                                   "vanilla_b25116796"])
def test_iteminfo_decode_score_is_identical_either_way(build):
    body, header = _load(build, "iteminfo")
    nf = to_name_first(body, header, "iteminfo")
    order = verified_order("ItemInfo")
    assert order
    assert (decode_score("ItemInfo", order, nf, header)
            == decode_score("ItemInfo", order, body, header))


@pytest.mark.parametrize("build", ["vanilla113", "vanilla116", "vanilla1161",
                                   "vanilla_b24994088", "vanilla_b25116796"])
def test_storeinfo_locates_the_same_stock_lists_either_way(build):
    body, header = _load(build, "storeinfo")
    _ks, offs = parse_pabgh_index(header, "storeinfo")
    nf = to_name_first(body, header, "storeinfo")
    starts = sorted(offs.values())
    layout = detect_storeinfo_layout(body, starts)
    assert detect_storeinfo_layout(nf, starts, NAME_FIRST) == layout
    spans = starts + [len(body)]
    located = 0
    for key, off in offs.items():
        end = spans[spans.index(off) + 1]
        try:
            want = locate_stock_list(body, _entry_payload(body, off), end,
                                     key, layout)
        except Exception as exc:                      # noqa: BLE001
            with pytest.raises(type(exc)):
                locate_stock_list(nf, _entry_payload(nf, off, NAME_FIRST),
                                  end, key, layout)
            continue
        assert locate_stock_list(nf, _entry_payload(nf, off, NAME_FIRST),
                                 end, key, layout) == want
        located += 1
    assert located > 0


def test_statusgroupinfo_parses_the_same_records_either_way():
    body, header = _load("vanilla115", "statusgroupinfo")
    _ks, offs = parse_pabgh_index(header, "statusgroupinfo")
    nf = to_name_first(body, header, "statusgroupinfo")
    starts = sorted(offs.values())
    parsed = 0
    for o in starts:
        i = starts.index(o)
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        want = parse_record_full(body, o, end)
        assert parse_record_full(nf, o, end, NAME_FIRST) == want
        parsed += want is not None
    assert parsed == len(starts)


# ── the order is the table's, not the record's ────────────────────────────

def _name_first_table(records: list[tuple[int, bytes, bytes]],
                      ks: int = 4) -> tuple[bytes, dict[int, int]]:
    body = bytearray()
    offs = {}
    for key, name, payload in records:
        offs[key] = len(body)
        body += (struct.pack("<I", len(name)) + name
                 + key.to_bytes(ks, "little") + payload)
    return bytes(body), offs


def test_a_record_that_fits_both_orders_follows_its_table():
    # Game 2.04's formationinfo has this record: named "1", key 1. Read key
    # first it is key 1 and a 305-byte name, which also fits.
    body, offs = _name_first_table([
        (1, b"1", bytes(320)),
        (1001, b"Formation_A", b"\x00" * 8),
        (1002, b"Formation_B", b"\x00" * 8),
    ])
    assert read_header(body, offs[1], 4, KEY_FIRST)[0] == 1     # fits, wrongly
    assert table_header_order(body, offs, 4) == NAME_FIRST
    assert read_header(body, offs[1], 4, NAME_FIRST) == (1, b"1", 9)
    assert _parse_entry_header(body, offs[1], 4, NAME_FIRST)[:2] == (1, "1")


def test_a_table_that_fits_neither_order_has_none():
    good, offs = _name_first_table([(7, b"Seven", b"\x00")])
    legacy = (8).to_bytes(4, "little") + struct.pack("<I", 5) + b"Eight\x00"
    body = good + legacy
    offs = {**offs, 8: len(good)}
    assert table_header_order(body, offs, 4) is None


def test_without_an_order_the_header_is_read_key_first_as_before():
    body, header = _load("vanilla116", "skill")
    ks, offs = parse_pabgh_index(header, "skill")
    for key, off in list(offs.items())[:50]:
        assert (_parse_entry_header(body, off, ks)
                == _parse_entry_header(body, off, ks, None)
                == _parse_entry_header(body, off, ks, KEY_FIRST))
