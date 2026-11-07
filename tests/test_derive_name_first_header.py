"""``derive_table_layout`` walks game 2.04's name-first records (#494).

Every record opens with its key and its name. Every build before 2.04
writes them key first, ``[key][u32 name_len][name]``, and 2.04 writes them
name first, ``[u32 name_len][name][key]`` (#489). ``tiles()`` read the name
length ``key_size`` bytes in, which on 2.04 is the wrong four bytes, so the
walk started in the wrong place and no 2.04 table tiled.

``main()`` now decides each table's order once, with
``record_header.table_header_order`` against the table's own .pabgh, and
hands it to ``tiles()`` and ``search()``. Left out, the order is key first,
so a caller that doesn't pass it reads what it always read, with one
difference: ``record_header.header_end`` refuses a name longer than
``MAX_NAME_LEN`` (500 bytes), as CDUMM's parser does, so such a record no
longer tiles. No table has one; the longest name on 2.04.08 is 260 bytes.

None of this needs the exe or capstone. ``tiles()`` and ``search()`` walk
bytes with models they are given, and ``main()`` runs here with its exe
analysis answered from the models below.
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import derive_table_layout as dtl

from cdumm.semantic.parser import parse_pabgh_index
from cdumm.semantic.record_header import KEY_FIRST, NAME_FIRST, table_header_order
from tests.fixture_loaders import load_vanilla115, load_vanilla20408

_SRC = Path(__file__).resolve().parents[1] / "tools" / "derive_table_layout.py"


def _deriver(model=None) -> dtl.Deriver:
    """A Deriver with no exe behind it: tiles() and search() need only
    the models, and the walk reads nothing else off the instance."""
    d = object.__new__(dtl.Deriver)
    d.model = dict(model or {})
    d.elem = {}
    return d


def _table(records, *, name_first: bool, key_size: int = 4):
    """A body and ``load_table``'s ``ent`` for (key, name, payload) rows."""
    body = bytearray()
    ent = []
    for key, name, payload in records:
        ent.append((key, len(body)))
        k = key.to_bytes(key_size, "little")
        n = struct.pack("<I", len(name)) + name
        body += (n + k if name_first else k + n) + payload
    return bytes(body), ent


#: ``_isBlocked`` then a u32, the shape of a small table.
_FIELDS = [("_isBlocked", "fixed", 1), ("_value", "fixed", 4)]
_ROWS = [(7, b"Alpha", b"\x00" + struct.pack("<I", 30)),
         (9, b"Bravo_2", b"\x01" + struct.pack("<I", 1500)),
         (12, b"", b"\x00" + struct.pack("<I", 0))]


# ── the order decides where the walk starts ─────────────────────────────

def test_a_name_first_table_tiles_when_told_its_order():
    body, ent = _table(_ROWS, name_first=True)
    assert table_header_order(body, dict(ent), 4) == NAME_FIRST
    assert _deriver().tiles(body, ent, 4, _FIELDS, {}, order=NAME_FIRST)


def test_left_out_the_order_is_key_first_as_before():
    """The default keeps every existing caller's reading."""
    name_first, ent_n = _table(_ROWS, name_first=True)
    key_first, ent_k = _table(_ROWS, name_first=False)
    d = _deriver()
    assert not d.tiles(name_first, ent_n, 4, _FIELDS, {})
    assert d.tiles(key_first, ent_k, 4, _FIELDS, {})
    assert d.tiles(key_first, ent_k, 4, _FIELDS, {}, order=KEY_FIRST)


def test_each_order_fails_on_the_other_orders_bytes():
    key_first, ent = _table(_ROWS, name_first=False)
    assert table_header_order(key_first, dict(ent), 4) == KEY_FIRST
    assert not _deriver().tiles(key_first, ent, 4, _FIELDS, {},
                                order=NAME_FIRST)


def test_a_two_byte_key_sits_after_the_name():
    """wantedinfo and regioninfo have 2-byte keys: the walk starts
    ``4 + name_len + 2`` bytes in, whichever order, and only the table's
    own order tiles it."""
    for name_first in (True, False):
        body, ent = _table(_ROWS, name_first=name_first, key_size=2)
        order, other = ((NAME_FIRST, KEY_FIRST) if name_first
                        else (KEY_FIRST, NAME_FIRST))
        assert table_header_order(body, dict(ent), 2) == order
        assert _deriver().tiles(body, ent, 2, _FIELDS, {}, order=order)
        assert not _deriver().tiles(body, ent, 2, _FIELDS, {}, order=other)


# ── search() hands the order to every tiling it does ────────────────────

def test_search_with_one_unknown_uses_the_order():
    fields = [("_isBlocked", "fixed", 1), ("_value", "call", 0x10)]
    d = _deriver()
    d.candidates = lambda va=None: [("fixed", n) for n in range(9)]
    body, ent = _table(_ROWS, name_first=True)
    assert d.search(body, ent, 4, fields, [0x10],
                    order=NAME_FIRST) == [(("fixed", 4),)]
    assert d.search(body, ent, 4, fields, [0x10]) == []


def test_search_with_two_unknowns_uses_the_order():
    """All four of the two-unknown path's tilings get the order: drop it
    from any one and the answer below is lost."""
    rows = [(1, b"one", b"\x01" + struct.pack("<I", 0)),
            (2, b"two", b"\x00" + struct.pack("<I", 2) + b"\xAA\xBB" * 2),
            (3, b"three", b"\x01" + struct.pack("<I", 3) + b"\xCC\xDD" * 3)]
    fields = [("_isBlocked", "call", 0x10), ("_list", "call", 0x20)]
    d = _deriver()
    d.candidates = {0x10: [("fixed", 1), ("fixed", 2)],
                    0x20: [("list", 2), ("list", 3)]}.__getitem__
    body, ent = _table(rows, name_first=True)
    assert d.search(body, ent, 4, fields, [0x10, 0x20],
                    order=NAME_FIRST) == [(("fixed", 1), ("list", 2))]
    assert d.search(body, ent, 4, fields, [0x10, 0x20]) == []


# ── the committed 2.04.08 tables ────────────────────────────────────────
#
# The three tables #494 names, with the models this tool derives for them
# from the 2.04.08 exe: the fields ``field_reads`` gives, in read order and
# without ``_stringKey`` and ``_key``, and the readers they call, keyed by
# their 2.04.08 address. ``_STAGE1`` is what ``solve_reader`` solved and
# ``_STAGE1B`` the lists stage 1b pinned from their loop bodies, the two
# dicts main() hands tiles() as ``model`` and ``elem``.

_STAGE1 = {
    0x141702130: ("fixed", 2),
    0x14172D440: ("fixed", 4),
    0x1416D99D0: ("fixed", 4),
    0x1414A0DB0: ("strplus", 9),      # 9 fixed bytes, then a CString
}
_STAGE1B = {
    0x1417356D0: ("list", 8),
    0x14172DAA0: ("list", 2),
    0x141736C40: ("list", 1),
    0x14173D3F0: ("list", 12),
    0x14172DB80: ("list", 4),
    0x141736D20: ("list", 4),
    0x14172E760: ("list", 4),
}

_MODELS = {
    "wantedinfo": [
        ("_isBlocked", "fixed", 1), ("_useTargetPrice", "fixed", 1),
        ("_increasePrice", "fixed", 8)],
    "statusgroupinfo": [
        ("_isBlocked", "fixed", 1),
        ("_regenerateStatusInfoList", "call", 0x141736D20),
        ("_statusInfoList", "call", 0x141736D20),
        ("_elementalStatusInfoList", "call", 0x141736D20),
        ("_statusIndexList", "call", 0x14172E760),
        ("_regenStatusIndexList", "call", 0x14172E760)],
    "regioninfo": [
        ("_isBlocked", "fixed", 1), ("_isUIMapDisable", "fixed", 1),
        ("_isNonePlayZone", "fixed", 1), ("_isWild", "fixed", 1),
        ("_isTown", "fixed", 1), ("_regionType", "fixed", 1),
        ("_limitVehicleRun", "fixed", 1), ("_accompanyAllowType", "fixed", 1),
        ("_isWorldMapRoadPathFindable", "fixed", 1),
        ("_regionInventoryInfo", "fixed", 2),
        ("_parentRegionInfo", "fixed", 2),
        ("_bitmapColor", "call", 0x141702130),
        ("_knowledgeInfo", "call", 0x14172D440),
        ("_fogClearCondition", "call", 0x1416D99D0),
        ("_overriedMaxHeight", "fixed", 4),
        ("_displayRegionName", "call", 0x1414A0DB0),
        ("_regionEnterknowledgeInfoList", "call", 0x1417356D0),
        ("_childRegionInfoList", "call", 0x14172DAA0),
        ("_forbiddenMercenaryKeyList", "call", 0x141736C40),
        ("_domainFactionList", "call", 0x14173D3F0),
        ("_tagList", "call", 0x14172DB80)],
}


def _load(loader, stem):
    body = loader(f"{stem}.pabgb")
    key_size, offsets = parse_pabgh_index(loader(f"{stem}.pabgh"), stem)
    return body, key_size, offsets, sorted(offsets.items(), key=lambda kv: kv[1])


@pytest.mark.parametrize("stem, records", [
    ("wantedinfo", 35), ("statusgroupinfo", 9), ("regioninfo", 1012)])
def test_2_04_08_tables_tile_name_first_and_never_key_first(stem, records):
    body, ks, offsets, ent = _load(load_vanilla20408, stem)
    assert len(ent) == records
    order = table_header_order(body, offsets, ks)
    assert order == NAME_FIRST
    d = _deriver(_STAGE1)
    assert d.tiles(body, ent, ks, _MODELS[stem], _STAGE1B, order=order)
    # Key first, which is all tiles() could do before: not one record.
    assert not any(d.tiles(body, ent, ks, _MODELS[stem], _STAGE1B, [i])
                   for i in range(len(ent)))


def test_a_pre_2_04_table_still_tiles_key_first():
    """1.15's statusgroupinfo has the same fields as 2.04.08's. Its order
    comes out key first, and that is the reading it tiles under."""
    body, ks, offsets, ent = _load(load_vanilla115, "statusgroupinfo")
    order = table_header_order(body, offsets, ks)
    assert order == KEY_FIRST
    d = _deriver(_STAGE1)
    fields = _MODELS["statusgroupinfo"]
    assert d.tiles(body, ent, ks, fields, _STAGE1B, order=order)
    assert d.tiles(body, ent, ks, fields, _STAGE1B)
    assert not any(d.tiles(body, ent, ks, fields, _STAGE1B, [i], NAME_FIRST)
                   for i in range(len(ent)))


# ── main() itself, with the exe analysis stubbed out ────────────────────
#
# Run over the three 2.04.08 tables, a name-first table whose first record
# fits both readings, 1.15's key-first statusgroupinfo and a table that
# fits neither order. main() loads them in this order, so its loading loop
# ends on a table that isn't name first: a stage 2 that read every table
# with the last order it saw, instead of each table's own, would read the
# 2.04.08 tables key first and prove none of them.

_RUN = {
    "wantedinfo": ("WantedInfo", _MODELS["wantedinfo"]),
    "statusgroupinfo": ("StatusGroupInfo", _MODELS["statusgroupinfo"]),
    "regioninfo": ("RegionInfo", _MODELS["regioninfo"]),
    "bothinfo": ("BothInfo", _FIELDS),
    "oldstatusgroupinfo": ("OldStatusGroupInfo", _MODELS["statusgroupinfo"]),
    "neitherinfo": ("NeitherInfo", _FIELDS),
}
#: Name first. Its first record is built like formationinfo's record 1 on
#: 2.04.08, named "1" with key 1: read key first, that's key 1 and then a
#: 305-byte name, which fits too. Only the other records settle the order,
#: so it has to be decided over all of them, not from the first.
_BOTH_ROWS = ([(1, b"1", _ROWS[0][2])]
              + [(k, b"row%03d" % k, _ROWS[0][2]) for k in range(2, 26)])
#: regioninfo's ``_tagList`` reader. Stage 1b is told nothing about it, so
#: stage 2 has to find it, and main()'s search() call runs as well.
_SEARCHED = 0x14172DB80


class _NoExeDeriver(dtl.Deriver):
    """Answers stage 1 and 1b from the models above instead of the exe.
    ``tiles()`` and ``search()`` are the real ones."""

    def __init__(self, game_dir):
        self.model, self.elem, self._memo = {}, {}, {}
        self.by_cls = {cls: [] for cls, _fields in _RUN.values()}

    def field_reads(self, cls):
        return next(list(f) for c, f in _RUN.values() if c == cls)

    def solve_reader(self, va):
        return _STAGE1.get(va)

    def reader_parts(self, va):
        return None

    def list_element(self, va):
        m = None if va == _SEARCHED else _STAGE1B.get(va)
        return None if m is None else ("fixed", m[1])


def _load_table(game_dir, stem):
    if stem == "bothinfo":
        body, ent = _table(_BOTH_ROWS, name_first=True)
        return body, ent, 4
    if stem == "neitherinfo":
        # Key first, but its index gives the first record a key the body
        # doesn't hold, so neither order fits every record.
        body, ent = _table(_ROWS, name_first=False)
        return body, [(ent[0][0] + 100, ent[0][1])] + ent[1:], 4
    if stem == "oldstatusgroupinfo":
        body, ks, _offsets, ent = _load(load_vanilla115, "statusgroupinfo")
    else:
        body, ks, _offsets, ent = _load(load_vanilla20408, stem)
    return body, ent, ks


def test_main_reads_each_table_in_its_own_order(monkeypatch, capsys):
    monkeypatch.setattr(dtl, "Deriver", _NoExeDeriver)
    monkeypatch.setattr(dtl, "load_table", _load_table)
    assert dtl.main(["--game-dir", "unused", "--tables", *_RUN]) == 0
    out = capsys.readouterr().out

    assert ("record headers: 4 name first (game 2.04), 1 key first, "
            "1 neither (read key first)") in out
    assert (f"round 1: sub_{_SEARCHED:X} = 4 + count*4   "
            f"(from regioninfo, 1012 records)") in out
    proven = out.split("PROVEN", 1)[1].split("Reminder", 1)[0]
    assert "6 tables" in proven.splitlines()[0]
    old = len(_load(load_vanilla115, "statusgroupinfo")[3])
    for stem, n in (("wantedinfo", 35), ("statusgroupinfo", 9),
                    ("regioninfo", 1012), ("bothinfo", len(_BOTH_ROWS)),
                    ("oldstatusgroupinfo", old), ("neitherinfo", len(_ROWS))):
        assert re.search(rf"^   {stem} +{n} records$", proven, re.MULTILINE), out


# ── and a source check, for calls added later ───────────────────────────

def _section(start: str, end: str) -> str:
    src = _SRC.read_text(encoding="utf-8")
    i = src.index(start)
    return src[i:src.index(end, i + 1)]


def _calls(text: str, callee: str) -> list[str]:
    """The argument text of each ``callee(...)`` call in ``text``."""
    out = []
    for m in re.finditer(re.escape(callee) + r"\(", text):
        depth, j = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        out.append(text[m.end():j - 1])
    return out


@pytest.mark.parametrize("start, end, callees", [
    ("    def search(self", "\ndef load_table", ("self.tiles",)),
    ("def main(", '\nif __name__ == "__main__"', ("d.tiles", "d.search")),
], ids=["search", "main"])
def test_every_tiling_call_names_the_order(start, end, callees):
    """Like ``_consume``'s mapping, checked on the source: a call added
    later without the order would read its table key first, and only a
    2.04 install would show it. The tests above check the calls there
    are now by running them."""
    calls = [c for name in callees for c in _calls(_section(start, end), name)]
    assert calls
    assert all("order=order" in c for c in calls)
