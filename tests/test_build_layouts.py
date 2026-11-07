"""Game 2.04.08's table layouts (``schemas/layouts/2.04.08.json``), read-only.

The oracle for the layouts is the game's own reader. Each was derived from
the 2.04.08 executable, and the build file records, per table, a digest of
every field's end offset in every record as that derivation walked it.
These tests compile the file into CDUMM descriptors, walk the committed
2.04.08 fixtures with ``pabgb_types.consume_bytes``, and require:

* every record to tile: the fields end exactly on the next record;
* the same field ends as the derivation, record by record (the digest).

Tiling and the digest pin each field's offset and width. They can't tell
two same-width fields apart, so values count as checked only for the
fields a table lists as ``verified``. Those checks are below, on the same
fixture bytes.

The fixtures are base-game tables only, and their records use 343 of the
354 layout features any 2.04.08 base record uses. The rest sit in tables
too big to commit (characterinfo, gimmickinfo, gimmickgroupinfo), so
small synthetic records test them: a null flag that is null, a tag with a
default, an optional LocalizableString, a nested condition logic. So do a
3-byte tag (bitmapposition), a union default no record takes, and a
condition tree too deep to walk. Four condition functions (176, 212, 224,
416) occur only in gimmickinfo and get no test here.
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from cdumm.engine.schema_verify import layout_fit, schema_for_table
from cdumm.semantic import build_layouts as BL
from cdumm.semantic import pabgb_types as PT
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import (
    get_schema,
    parse_pabgh_index,
    parse_records_display,
)
from cdumm.semantic.record_header import NAME_FIRST, header_end
from tests.fixture_loaders import load_vanilla20408

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_LAYOUT_FILE = (Path(__file__).resolve().parents[1] / "schemas" / "layouts"
                / "2.04.08.json")

_TABLES_204 = sorted(p.name.split(".")[0] for p in
                     (_FIXTURES / "vanilla20408").glob("*.pabgb.zlib"))


def _load(stem: str) -> tuple[bytes, bytes]:
    return (load_vanilla20408(f"{stem}.pabgb"),
            load_vanilla20408(f"{stem}.pabgh"))


def _build() -> BL.BuildLayouts:
    (b,) = [b for b in BL.builds() if b.build == "2.04.08"]
    return b


def _pre_204_tables() -> list[tuple[str, str]]:
    out = []
    for d in sorted(p for p in _FIXTURES.iterdir()
                    if p.is_dir() and p.name != "vanilla20408"):
        for b in sorted(d.glob("*.pabgb.zlib")):
            t = b.name.split(".")[0]
            if (d / f"{t}.pabgh.zlib").exists():
                out.append((d.name, t))
    return out


def _load_pre(build: str, stem: str) -> tuple[bytes, bytes]:
    import zlib
    d = _FIXTURES / build
    return (zlib.decompress((d / f"{stem}.pabgb.zlib").read_bytes()),
            zlib.decompress((d / f"{stem}.pabgh.zlib").read_bytes()))


def _field_ends(descs: list[str], body: bytes, offsets: dict[int, int],
                key_size: int) -> tuple[int, str]:
    """(records that tile, digest of every field end), in offset order.

    The digest is the build file's ``field_ends_sha256``: per record, u32le
    of the field count, then each field's end relative to the payload start
    as u32le. A record that fails to walk adds nothing, so the digest can
    only match when every record walks.
    """
    ent = sorted(offsets.values())
    h = hashlib.sha256()
    tiled = 0
    nf = len(descs)
    for i, off in enumerate(ent):
        end = ent[i + 1] if i + 1 < len(ent) else len(body)
        pay = p = header_end(body, off, key_size, NAME_FIRST)
        ends = []
        for d in descs:
            n = PT.consume_bytes(d, body, p, end)
            if n is None:
                break
            p += n
            ends.append(p - pay)
        else:
            h.update(struct.pack("<I", nf) + struct.pack(f"<{nf}I", *ends))
            tiled += p == end
    return tiled, h.hexdigest()


def _raw_fields(schema, body: bytes, offsets: dict[int, int],
                key_size: int, want: set[str]) -> dict[int, dict[str, int]]:
    """``{key: {field: payload offset}}`` for the ``want`` fields."""
    by_off = {off: key for key, off in offsets.items()}
    ent = sorted(offsets.values())
    out = {}
    for i, off in enumerate(ent):
        end = ent[i + 1] if i + 1 < len(ent) else len(body)
        p = header_end(body, off, key_size, NAME_FIRST)
        got = {}
        for f in schema.fields:
            if f.name in want:
                got[f.name] = p
            p += PT.consume_bytes(f.type_descriptor, body, p, end)
        out[by_off[off]] = got
    return out


# ── the file and what it compiles to ───────────────────────────────────────

def test_the_build_file_covers_every_2_04_08_table():
    data = json.loads(_LAYOUT_FILE.read_text(encoding="utf-8"))
    assert data["format"] == BL.FORMAT
    assert data["paver"]["version"] == "2.4.8.1"
    assert len(data["tables"]) == 137
    assert sum(t["records"] for t in data["tables"].values()) == 313_535
    assert {t["class"] for t in data["tables"].values()} == set(data["classes"])


def test_every_compiled_descriptor_is_one_the_walker_knows():
    b = _build()
    pref = b.build + "/"
    descs = [d for fields in b.fields.values() for _n, d in fields]
    for name, members in PT.SUBSTRUCT_DEFS.items():
        if name.startswith(pref):
            descs += [d for _m, d in members]
    for name, v in PT.TAGGED_VARIANT_DEFS.items():
        if name.startswith(pref):
            descs += [d for _m, d in v.get("fixed_prefix", [])]
            descs += [d for d in v["variants"].values() if d]
    unknown = sorted({d for d in descs if not PT.is_known_type(d)})
    assert not unknown, unknown[:10]


def test_compiled_field_names_are_unique_and_clear_of_the_header_columns():
    for cls, fields in _build().fields.items():
        names = [n for n, _d in fields]
        assert len(names) == len(set(names)), cls
        assert not {"_key", "_name"} & set(names), cls
    # characterinfo reads _IgnoreScale twice; the grid would show one.
    names = [n for n, _d in _build().fields["CharacterInfo"]]
    assert "_IgnoreScale" in names and "_IgnoreScale#2" in names


def test_the_fixtures_are_the_bytes_the_layouts_were_proven_on():
    assert len(_TABLES_204) == 21, _TABLES_204
    for stem in _TABLES_204:
        body, header = _load(stem)
        t = _build().tables[stem]
        assert hashlib.sha256(body).hexdigest() == t.pabgb_sha256, stem
        assert hashlib.sha256(header).hexdigest() == t.pabgh_sha256, stem


@pytest.mark.parametrize("stem", _TABLES_204)
def test_every_record_tiles_and_ends_each_field_where_the_game_does(stem):
    body, header = _load(stem)
    b = _build()
    t = b.tables[stem]
    key_size, offsets = parse_pabgh_index(header, stem)
    assert len(offsets) == t.records
    descs = [d for _n, d in b.fields[t.cls]]
    tiled, digest = _field_ends(descs, body, offsets, key_size)
    assert tiled == t.records, (stem, tiled, t.records)
    assert digest == t.field_ends_sha256, stem
    assert BL.tiles(b.schema(stem, pinned=False), body, offsets, key_size)


# ── choosing a layout ──────────────────────────────────────────────────────

@pytest.mark.parametrize("stem", _TABLES_204)
def test_the_grid_gets_the_2_04_08_layout_on_2_04_08_bytes(stem):
    body, header = _load(stem)
    s = schema_for_table(stem, body, header)
    assert s.build == "2.04.08"
    assert s.no_null_skip and not s.no_entry_header
    assert s.verified_fields == _build().tables[stem].verified
    fit = layout_fit(stem, body, header)
    assert fit.checked and fit.exact == fit.checked == fit.complete


@pytest.mark.parametrize("build,stem", _pre_204_tables())
def test_a_pre_2_04_table_gets_no_build_layout(build, stem):
    """Key-first bytes never reach a build layout, so ``schema_for_table``
    goes on exactly as it did before for every pre-2.04 table."""
    body, header = _load_pre(build, stem)
    assert BL.schema_for_bytes(stem, body, header) is None


def test_get_schema_never_returns_a_build_layout():
    BL.builds()
    tables = parser_mod.supported_tables()
    assert all(get_schema(t).build is None for t in tables)
    # The 2.04 stems with no pre-2.04 schema stay unknown to the writers'
    # lookups; only the grid's own lookup names them.
    only_204 = sorted(set(_build().tables) - set(tables))
    assert only_204 and not any(parser_mod.has_schema(s) for s in only_204)
    assert parser_mod.identify_table_from_path(
        f"gamedata/{only_204[0]}.pabgb") is None
    assert BL.table_for_path(
        f"gamedata/{only_204[0]}.pabgb") == only_204[0]


def test_other_bytes_that_still_tile_keep_the_layout_but_lose_the_checks():
    body, header = _load("wantedinfo")
    key_size, offsets = parse_pabgh_index(header, "wantedinfo")
    first = min(offsets.values())
    price = header_end(body, first, key_size, NAME_FIRST) + 2
    changed = bytearray(body)
    changed[price] ^= 0x01
    s = BL.schema_for_bytes("wantedinfo", bytes(changed), header)
    assert s is not None and s.build == "2.04.08"
    assert s.verified_fields == frozenset()


def test_bytes_that_dont_tile_get_no_layout_and_the_old_schema_back():
    body, header = _load("wantedinfo")
    short = body[:-1]            # the last record loses its last byte
    assert BL.schema_for_bytes("wantedinfo", short, header) is None
    s = schema_for_table("wantedinfo", short, header)
    assert s is get_schema("wantedinfo")


# ── the verified fields ────────────────────────────────────────────────────

def test_wantedinfo_prices_are_the_tiers_recorded_before_2_04():
    """The verified-only pass that gated ``_increasePrice`` (2026-07-03)
    recorded the values it read: 30/50/100/500/1500 gold, the wanted tiers.
    Read in 2.04's order, the 2.04.08 records hold exactly those tiers.
    Read in the shipped pre-2.04 order, the grid showed 256 times the
    price plus ``_useTargetPrice`` (#458)."""
    body, header = _load("wantedinfo")
    recs = parse_records_display("wantedinfo", body, header)
    assert len(recs) == 35
    prices = [r["_increasePrice"] for r in recs.values()]
    assert set(prices) == {30, 50, 100, 500, 1500}
    assert sorted(prices.count(p) for p in set(prices)) == [5, 5, 5, 10, 10]

    old = get_schema("wantedinfo")
    assert [f.name for f in old.fields] == [
        "_isBlocked", "_increasePrice", "_useTargetPrice"]
    key_size, offsets = parse_pabgh_index(header, "wantedinfo")
    for key, off in offsets.items():
        p = header_end(body, off, key_size, NAME_FIRST)
        old_price = struct.unpack_from("<Q", body, p + 1)[0]
        r = recs[key]
        assert old_price == 256 * r["_increasePrice"] + r["_useTargetPrice"]


def test_regioninfo_parents_and_child_lists_agree():
    """Each region's parent is a region whose child list names it, and
    each listed child is a region that names that parent or none. A u16
    read at the wrong offset, or a list with the wrong element width,
    wouldn't produce 857 consistent pairs."""
    body, header = _load("regioninfo")
    key_size, offsets = parse_pabgh_index(header, "regioninfo")
    s = BL.schema_for_bytes("regioninfo", body, header)
    at = _raw_fields(s, body, offsets, key_size,
                     {"_parentRegionInfo", "_childRegionInfoList"})
    keys = set(at)
    parent = {k: struct.unpack_from("<H", body, a["_parentRegionInfo"])[0]
              for k, a in at.items()}
    children = {}
    for k, a in at.items():
        o = a["_childRegionInfoList"]
        n = struct.unpack_from("<I", body, o)[0]
        children[k] = list(struct.unpack_from(f"<{n}H", body, o + 4))

    with_parent = [k for k, p in parent.items() if p]
    assert len(with_parent) == 857
    assert all(parent[k] in keys and k in children[parent[k]]
               for k in with_parent)
    listed = [c for cs in children.values() for c in cs]
    assert len(listed) == 1_008
    assert len(listed) == len(set(listed))       # no region has two parents
    assert all(c in keys for c in listed)
    assert all(parent[c] in (k, 0) for k, cs in children.items() for c in cs)
    assert sum(1 for c in listed if parent[c] == 0) == 151


# ── spellings no committed fixture reaches ─────────────────────────────────

_SEQ = iter(range(10_000))


def _mini_build(fields: list, condition: dict | None = None
                ) -> tuple[BL.BuildLayouts, str]:
    """A one-table build named so it can't clash with another test's."""
    name = f"test-{next(_SEQ)}"
    data = {
        "format": BL.FORMAT, "build": name,
        "tables": {"t": {"class": "T", "key_size": 4, "records": 1,
                         "pabgb_sha256": "", "pabgh_sha256": "",
                         "field_ends_sha256": ""}},
        "classes": {"T": [["_f", sp] for sp in fields]},
        "readers": {},
    }
    if condition is not None:
        data["condition"] = condition
    return BL.BuildLayouts(data), name


def _width(fields: list, data: bytes, condition: dict | None = None):
    b, _name = _mini_build(fields, condition)
    (_n, d), = b.fields["T"]
    return PT.consume_bytes(d, data, 0, len(data))


def test_a_null_flag_reads_its_entry_only_when_the_flag_is_zero():
    sp = [["nullflag", ["fixed", 4]]]
    assert _width(sp, b"\x00\x01\x02\x03\x04") == 5
    assert _width(sp, b"\x01") == 1
    assert _width(sp, b"\x02") == 1        # 2.04.08's DLC holds 2 as null
    assert _width(sp, b"\x00\x01") is None


def test_a_union_falls_back_to_its_default_and_refuses_without_one():
    with_default = [["union", ["fixed", 1], [[3, ["fixed", 2]]], ["fixed", 4]]]
    assert _width(with_default, b"\x03\xaa\x01\x02") == 4
    assert _width(with_default, b"\x07\xaa\x01\x02\x03\x04") == 6
    without = [["union", ["fixed", 1], [[3, ["fixed", 2]]], None]]
    assert _width(without, b"\x03\xaa\x01\x02") == 4
    assert _width(without, b"\x07\xaa\x01\x02\x03\x04") is None


def test_a_three_byte_tag_reads_its_case_and_refuses_an_unknown_one():
    sp = [["tagswitch", 3, ["fixed", 1],
           [[0, ["fixed", 0]], [1, ["fixed", 2]], [65536, ["fixed", 4]]]]]
    assert _width(sp, b"\x00\x00\x00\xbb") == 4
    assert _width(sp, b"\x01\x00\x00\xbb\x01\x02") == 6
    assert _width(sp, b"\x00\x00\x01\xbb\x01\x02\x03\x04") == 8
    assert _width(sp, b"\x00\x01\x00\xbb") is None


def test_a_u32_kind_with_a_default_reads_nothing_more_for_other_kinds():
    sp = [["tagswitchd", 4, ["fixed", 0],
           [[1, ["fixed", 1]], [2, ["fixed", 2]]], ["fixed", 0]]]
    assert _width(sp, struct.pack("<I", 1) + b"\x09") == 5
    assert _width(sp, struct.pack("<I", 2) + b"\x09\x09") == 6
    assert _width(sp, struct.pack("<I", 7)) == 4
    assert _width(sp, struct.pack("<I", 0x0100_0001)) == 4


def test_an_optional_localizable_string_reads_its_flag_then_the_string():
    sp = [["optloc"]]
    assert _width(sp, b"\x00") == 1
    loc = b"\x00" + struct.pack("<Q", 7) + struct.pack("<I", 2) + b"hi"
    assert _width(sp, b"\x01" + loc) == 1 + len(loc)
    assert _width(sp, b"\x01" + loc[:-1]) is None


def _cstr(s: bytes) -> bytes:
    return struct.pack("<I", len(s)) + s


def test_a_condition_function_with_a_nested_logic_walks_it_absent_or_present():
    """Function 226 reads a whole condition logic inside its own reads.
    2.04.08's records only ever leave it absent (gimmickinfo, too big to
    commit); a present one walks the same grammar one level down."""
    model = json.loads(_LAYOUT_FILE.read_text(encoding="utf-8"))["condition"]
    b, _name = _mini_build([["cond"]], model)
    (_n, d), = b.fields["T"]

    def logic(node: bytes) -> bytes:
        return b"\x01\x01" + node + b"\x00\x00\x00"

    def f226(nested: bytes) -> bytes:
        return (b"\x03" + struct.pack("<H", 226)
                + b"\x01" + b"\x05" + nested + _cstr(b"a") + _cstr(b"")
                + b"\x00" + _cstr(b"") + b"\x00\x00"
                + b"\x00")                   # the +0x98 target flag, 0
    leaf = b"\x06" + b"\x00" * 4
    for nested in (b"\x00", logic(leaf)):
        data = logic(f226(nested))
        assert PT.consume_bytes(d, data, 0, len(data)) == len(data)
    # A set target flag is unmodelled, so it refuses.
    bad = logic(f226(b"\x00")[:-1] + b"\x01")
    assert PT.consume_bytes(d, bad, 0, len(bad)) is None


def test_a_condition_tree_too_deep_to_walk_fails_tiling_instead_of_raising():
    model = json.loads(_LAYOUT_FILE.read_text(encoding="utf-8"))["condition"]
    b, _name = _mini_build([["cond"]], model)
    (_n, d), = b.fields["T"]
    leaf = b"\x06" + b"\x00" * 4             # node type 6: a u32
    shallow = b"\x01\x01" + b"\x02" * 10 + leaf + b"\x00\x00\x00"
    assert PT.consume_bytes(d, shallow, 0, len(shallow)) == len(shallow)
    deep = b"\x01\x01" + b"\x02" * 5_000 + leaf + b"\x00\x00\x00"
    with pytest.raises(RecursionError):
        PT.consume_bytes(d, deep, 0, len(deep))
    name = b"x"
    record = struct.pack("<I", len(name)) + name + struct.pack("<I", 9) + deep
    assert BL.tiles(b.schema("t", pinned=False), record, {9: 0}, 4) is False
