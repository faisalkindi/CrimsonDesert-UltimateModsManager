"""Audit findings A + B (2026-06-10): size-changing iteminfo
whole-table changes shipped with a stale companion .pabgh (the
index that maps record keys to byte offsets), so the game would
read garbage entry headers for every record after the first grown
one. And the production flush (the has-schema path) never attached
``_f3_rebuild``, so the v3.3.20 live-buffer rebuild only ever
protected skill.

These tests prove, against the real CD 1.10 extract:
  1. the pabgh offset-rewrite identity property (rewriting the
     vanilla index with identity-serialize offsets reproduces it
     byte-for-byte, which also pins the offset semantics),
  2. a growing socket intent emits a companion whose offsets all
     point at real record starts in the grown table,
  3. a same-size intent emits no companion,
  4. the production flush routes the companion to iteminfo.pabgh
     and attaches _f3_rebuild to the table change.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import pytest

from tests.fixture_loaders import has_vanilla110, load_vanilla110

pytestmark = pytest.mark.skipif(
    not (has_vanilla110("iteminfo.pabgb")
         and has_vanilla110("iteminfo.pabgh")),
    reason="1.10 iteminfo fixtures absent")


@dataclass
class _Intent:
    entry: str
    key: int
    field: str
    op: str
    new: Any
    old: Any = None


@pytest.fixture(scope="module")
def vanilla():
    return load_vanilla110("iteminfo.pabgb")


@pytest.fixture(scope="module")
def header():
    return load_vanilla110("iteminfo.pabgh")


@pytest.fixture(scope="module")
def growth_change(vanilla, header):
    """One real Sockets-module intent (grows the record) built WITH
    the companion header."""
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    items = parse_iteminfo_from_bytes(vanilla)
    key = next(i["key"] for i in items
               if i.get("string_key") == "Tarif_Necklace")
    intents = [_Intent(
        entry="Tarif_Necklace", key=key,
        field="drop_default_data.add_socket_material_item_list",
        op="set", new=[{"item": 1, "value": 500}])]
    change = build_iteminfo_intent_change(
        vanilla, intents, vanilla_header=header)
    assert change is not None
    return change


def test_pabgh_rewrite_identity(vanilla, header):
    """Index-framed parse (what the writer uses when the .pabgh is
    available): identity serialize must reproduce vanilla AND the
    identity offsets must reproduce the vanilla index byte-for-byte.
    The sniff walk can NOT be used here: its key ceiling swallows
    Delesyian_Flag (key 254M) into the previous record's tail, which
    is exactly the audit-M12 bug the index framing fixes."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes, serialize_iteminfo,
    )
    from cdumm.engine.pabgh_rewrite import rewrite_pabgh_offsets
    from cdumm.semantic.parser import parse_pabgh_index

    _, idx = parse_pabgh_index(header, "iteminfo")
    items = parse_iteminfo_from_bytes(
        vanilla, record_offsets=list(idx.values()))
    assert len(items) == len(idx), (
        f"index-framed parse returned {len(items)} items for "
        f"{len(idx)} index entries")
    offsets: dict[int, int] = {}
    ident = serialize_iteminfo(items, offsets_out=offsets)
    assert ident == vanilla, "identity serialize must reproduce vanilla"
    rewritten = rewrite_pabgh_offsets(header, "iteminfo", offsets)
    assert rewritten == header, (
        "identity offsets must reproduce the vanilla index "
        "byte-for-byte (pins pabgh offset semantics)")


def test_growth_emits_companion_with_valid_offsets(growth_change, vanilla):
    from cdumm.semantic.parser import parse_pabgh_index

    patched = bytes.fromhex(growth_change["patched"])
    assert len(patched) > len(vanilla), "fixture intent must grow the table"

    companion = growth_change.get("_pabgh_companion")
    assert companion is not None, (
        "size-changing change emitted no .pabgh companion")
    new_header = bytes.fromhex(companion["patched"])

    key_size, offsets = parse_pabgh_index(new_header, "iteminfo")
    assert offsets, "companion index did not parse"
    # Every index entry must point at a record whose leading eid
    # equals the key, in the GROWN table.
    fmt = "<H" if key_size == 2 else "<I"
    for key, off in offsets.items():
        eid = struct.unpack_from(fmt, patched, off)[0]
        assert eid == key, (
            f"index says key {key} at 0x{off:X}, but record there "
            f"starts with eid {eid}: stale/wrong offset")


def test_same_size_intent_emits_no_companion(vanilla, header):
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change

    change = build_iteminfo_intent_change(
        vanilla,
        [_Intent(entry="Pyeonjeon_Arrow", key=2200,
                 field="max_stack_count", op="set", new=4321)],
        vanilla_header=header)
    assert change is not None
    assert "_pabgh_companion" not in change, (
        "fixed-size edit must not touch the index")


def test_cooltime_primitive_lands_via_native_writer(vanilla, header):
    """Audit finding C, end to end on the real 1.10 table: `cooltime`
    sits AFTER _itemIconList, where the stale schema walk mis-counts
    bytes on a 1.10 binary (silent drop at best, wrong-offset write
    at worst). Routed through the native writer it must land exactly,
    with the rest of the table untouched."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.semantic.parser import parse_pabgh_index

    _, idx = parse_pabgh_index(header, "iteminfo")
    items = parse_iteminfo_from_bytes(
        vanilla, record_offsets=list(idx.values()))
    target = next(i for i in items if i.get("string_key") == "Pyeonjeon_Arrow")
    assert target["cooltime"] != 7777, "fixture assumption broken"

    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", vanilla, header,
        [_Intent(entry="Pyeonjeon_Arrow", key=target["key"],
                 field="cooltime", op="set", new=7777)])
    table = [c for c in changes if c.get("_target_file") != "iteminfo.pabgh"]
    assert len(table) == 1, "expected one whole-table change"
    assert "rel_offset" not in table[0], (
        "cooltime took the stale schema walk (finding C regression)")

    patched = bytes.fromhex(table[0]["patched"])
    assert len(patched) == len(vanilla), "fixed-size edit changed size"
    new_items = parse_iteminfo_from_bytes(
        patched, record_offsets=list(idx.values()))
    new_target = next(i for i in new_items
                      if i.get("string_key") == "Pyeonjeon_Arrow")
    assert new_target["cooltime"] == 7777, "cooltime did not land"
    # Exactly one record's bytes changed.
    diffs = sum(1 for a, b in zip(items, new_items) if a != b)
    assert diffs == 1, f"{diffs} records changed, expected 1"


def test_flush_routes_companion_and_attaches_rebuild(vanilla, header):
    """Production path: _intents_to_v2_changes on iteminfo.pabgb with
    a growing intent must emit BOTH changes, the companion routed to
    iteminfo.pabgh (finding A) and the table change carrying
    _f3_rebuild (finding B)."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes

    items = parse_iteminfo_from_bytes(vanilla)
    key = next(i["key"] for i in items
               if i.get("string_key") == "Tarif_Necklace")
    intents = [_Intent(
        entry="Tarif_Necklace", key=key,
        field="drop_default_data.add_socket_material_item_list",
        op="set", new=[{"item": 1, "value": 500}])]

    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", vanilla, header, intents)
    assert changes, "flush produced nothing"

    table = [c for c in changes if c.get("_target_file") != "iteminfo.pabgh"]
    comp = [c for c in changes if c.get("_target_file") == "iteminfo.pabgh"]
    assert len(table) == 1, f"expected 1 table change, got {len(table)}"
    assert len(comp) == 1, "companion was not routed to iteminfo.pabgh"
    assert "_f3_rebuild" in table[0], (
        "production flush did not attach _f3_rebuild (finding B)")
    assert table[0]["_f3_rebuild"]["table"] == "iteminfo"
    assert "_pabgh_companion" not in table[0], "companion not popped"
