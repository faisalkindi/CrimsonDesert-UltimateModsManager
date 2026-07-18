"""array_append on iteminfo nested lists.

Appending one element to a list inside an iteminfo record (a socket-material
list, an item-tag list, ...) is mechanically a nested `set` of
`current_list + [element]`. iteminfo round-trips byte-exact through the
whole-table writer, so array_append reuses that path: grow exactly one
record, rebuild the .pabgh index, touch nothing else.

Before this, array_append was supported only for dropsetinfo.drops; every
other list field got an actionable skip. iteminfo lists were a documented
gap.
"""
from __future__ import annotations


from tests.fixture_loaders import load_vanilla113

from cdumm.engine.format3_apply import _expand_append_intents
from cdumm.engine.format3_handler import Format3Intent, validate_intents
from cdumm.engine.iteminfo_native_parser import (
    detect_iteminfo_layout, parse_iteminfo_from_bytes)
from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change

TARGET = "gamedata/binary__/client/bin/iteminfo.pabgb"
SOCKET = "drop_default_data.add_socket_material_item_list"


def _table():
    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    n = int.from_bytes(header[:2], "little")
    starts = sorted(
        int.from_bytes(header[2 + i * 8 + 4:2 + i * 8 + 8], "little")
        for i in range(n))
    items = parse_iteminfo_from_bytes(
        body, starts, fields=detect_iteminfo_layout(body, starts))
    return body, header, starts, items


def _append(entry, key, field, new, match=None):
    return Format3Intent(entry=entry, key=key, field=field,
                         op="array_append", new=new, old=None, match=match)


# ── validation ───────────────────────────────────────────────────────────

def test_iteminfo_nested_append_is_supported():
    v = validate_intents(
        "iteminfo.pabgb",
        [_append("Pyeonjeon_Arrow", 2200, SOCKET, {"item": 1, "value": 2})])
    assert len(v.supported) == 1 and not v.skipped


def test_iteminfo_bare_field_append_is_skipped_with_reason():
    v = validate_intents(
        "iteminfo.pabgb",
        [_append("Pyeonjeon_Arrow", 2200, "max_stack_count", 5)])
    assert not v.supported and len(v.skipped) == 1
    assert "nested list path" in v.skipped[0][1]


def test_dropset_drops_append_still_works():
    v = validate_intents(
        "dropsetinfo.pabgb",
        [_append("X", 1, "drops", {"flag": 1, "item_key": 1, "rates": 1})])
    assert len(v.supported) == 1 and not v.skipped


def test_other_table_append_still_skipped():
    v = validate_intents(
        "buffinfo.pabgb", [_append("X", 1, "some_list", {"a": 1})])
    assert not v.supported and len(v.skipped) == 1


# ── expansion ────────────────────────────────────────────────────────────

def test_expand_append_by_key_reads_current_list_and_appends():
    body, header, _s, items = _table()
    # an item whose socket list has entries, so we prove "current + new"
    src = next((it for it in items
                if (it.get("drop_default_data") or {})
                .get("add_socket_material_item_list")), None)
    assert src is not None
    key = src["key"]
    before = list(src["drop_default_data"]["add_socket_material_item_list"])
    elem = {"item": 424242, "value": 777}

    out = _expand_append_intents(
        TARGET, body, header,
        [_append(src["string_key"], key, SOCKET, elem)])
    assert len(out) == 1
    got = out[0]
    assert got.op == "set" and got.match is None
    assert got.field == SOCKET
    assert got.new == before + [elem]        # current list + element
    assert got.key == key


def test_expand_append_with_match_hits_every_matched_record():
    body, header, _s, items = _table()
    # append a socket-material entry to every item that already has a socket
    out = _expand_append_intents(
        TARGET, body, header,
        [_append("", 0, SOCKET, {"item": 9, "value": 9},
                 match={"drop_default_data.use_socket": 1})])
    socketed = [it for it in items
                if (it.get("drop_default_data") or {}).get("use_socket") == 1]
    assert len(out) == len(socketed) > 100
    assert all(o.op == "set" and o.field == SOCKET for o in out)
    # each carries that record's own current list + the new element
    assert all(o.new[-1] == {"item": 9, "value": 9} for o in out)


def test_expand_append_on_non_list_field_is_dropped():
    body, header, _s, _items = _table()
    out = _expand_append_intents(
        TARGET, body, header,
        [_append("Pyeonjeon_Arrow", 2200, "max_stack_count", 5)])
    assert out == []          # not a list -> no set emitted, nothing applied


def test_non_append_intents_pass_through():
    body, header, _s, _items = _table()
    plain = Format3Intent(entry="Notepad", key=1, field="max_stack_count",
                          op="set", new=5, old=None, match=None)
    assert _expand_append_intents(TARGET, body, header, [plain]) == [plain]


# ── byte-exact integration ───────────────────────────────────────────────

def test_append_grows_exactly_one_record_byte_exact():
    body, header, starts, items = _table()
    by_key = {i["key"]: i for i in items}
    KEY = 2200                                # Pyeonjeon_Arrow, empty list
    before = list(by_key[KEY]["drop_default_data"]
                  ["add_socket_material_item_list"])
    elem = {"item": 424242, "value": 777}

    expanded = _expand_append_intents(
        TARGET, body, header,
        [_append("Pyeonjeon_Arrow", KEY, SOCKET, elem)])
    change = build_iteminfo_intent_change(body, expanded, vanilla_header=header)
    assert change is not None
    new_body = bytes.fromhex(change["patched"])

    comp = change.get("_pabgh_companion")
    assert comp is not None, "growing a record must rebuild the .pabgh index"
    new_header = bytes.fromhex(comp["patched"])
    n2 = int.from_bytes(new_header[:2], "little")
    starts2 = sorted(
        int.from_bytes(new_header[2 + i * 8 + 4:2 + i * 8 + 8], "little")
        for i in range(n2))
    items2 = parse_iteminfo_from_bytes(
        new_body, starts2, fields=detect_iteminfo_layout(new_body, starts2))
    by2 = {i["key"]: i for i in items2}

    after = by2[KEY]["drop_default_data"]["add_socket_material_item_list"]
    assert after == before + [elem]

    assert len(items2) == len(items)
    assert [i for i in items2 if "_opaque_record" in i] == []
    differing = [k for k in by_key if by_key[k] != by2.get(k)]
    assert differing == [KEY], f"expected only {KEY} to change: {differing[:6]}"
