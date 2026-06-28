"""falobos76's v3.3.20 retest on #191: the merged Stack 999,999 +
1 Socket On Accessories whole-table change still skipped, this time
with "offset 0 exceeds file size 5532062" and an empty "got".

Root cause (the REAL one behind both retests): socket intents
(``add_socket_material_item_list``) grow iteminfo records, so the
writer's ``patched`` table is ~2 KB longer than the live file. The
apply loop's bounds check used ``len(patched_bytes)`` even though a
replace's footprint in the current buffer is the ORIGINAL's length
(replaces support size deltas). Any size-growing whole-table change
therefore tripped the bounds check at offset 0 and skipped before
the compare, the v3.3.20 rebuild, or anything else could run.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.fixture_loaders import has_vanilla110, load_vanilla110

pytestmark = pytest.mark.slow

# A real intent from the Sockets 1 module of CD QOL Suite (nexus 1591):
# appends a socket-material element, growing the record.
_GROWTH_INTENT = dict(
    entry="Tarif_Necklace", key=0,
    field="drop_default_data.add_socket_material_item_list",
    op="set", new=[{"item": 1, "value": 500}])


@dataclass
class _Intent:
    entry: str
    key: int
    field: str
    op: str
    new: Any
    old: Any = None


def _growth_change(vanilla: bytes):
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    items = parse_iteminfo_from_bytes(vanilla)
    key = next(i["key"] for i in items
               if i.get("string_key") == "Tarif_Necklace")
    intents = [_Intent(**{**_GROWTH_INTENT, "key": key})]
    change = build_iteminfo_intent_change(vanilla, intents)
    assert change is not None
    assert len(bytes.fromhex(change["patched"])) > len(
        bytes.fromhex(change["original"])), (
        "fixture lost its point: socket intent no longer grows the table")
    change["_f3_rebuild"] = {
        "table": "iteminfo",
        "intents": [{"entry": i.entry, "key": i.key, "field": i.field,
                     "op": i.op, "new": i.new, "old": i.old}
                    for i in intents],
    }
    change.setdefault("offset", 0)
    return change, intents


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"), reason="1.10 iteminfo fixture absent")
def test_growing_whole_table_change_applies_on_clean_buffer():
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    vanilla = load_vanilla110("iteminfo.pabgb")
    change, _ = _growth_change(vanilla)
    patched = bytes.fromhex(change["patched"])

    data = bytearray(vanilla)
    skipped: list[dict] = []
    applied, mismatched, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=skipped)
    assert applied == 1, (
        f"size-growing whole-table change skipped: "
        f"{[(s.get('label'), s.get('reason')) for s in skipped]}")
    assert mismatched == 0
    assert bytes(data) == patched, "buffer is not the writer's grown table"


def test_tail_growing_replace_applies_synthetic():
    """Fixture-free pin on the bounds rule itself: a replace whose
    patched bytes overhang the old EOF must still apply, because its
    footprint in the current buffer is the ORIGINAL's length."""
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    data = bytearray(range(32))
    change = {
        "offset": 28,
        "original": bytes(range(28, 32)).hex(),
        "patched": b"\xaa\xbb\xcc\xdd\xee\xff\x11\x22".hex(),
    }
    applied, mismatched, _ = _apply_byte_patches(
        data, [change], skipped_out=[])
    assert applied == 1 and mismatched == 0
    assert bytes(data) == bytes(range(28)) + b"\xaa\xbb\xcc\xdd\xee\xff\x11\x22"


def _stack_intents():
    return [_Intent(entry="Pyeonjeon_Arrow", key=2200,
                    field="max_stack_count", op="set", new=4321)]


def _whole_table_change(base: bytes, intents):
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    change = build_iteminfo_intent_change(base, intents)
    assert change is not None
    change["_f3_rebuild"] = {
        "table": "iteminfo",
        "intents": [{"entry": i.entry, "key": i.key, "field": i.field,
                     "op": i.op, "new": i.new, "old": i.old}
                    for i in intents],
    }
    change.setdefault("offset", 0)
    return change


@pytest.fixture(scope="module")
def vanilla():
    if not has_vanilla110("iteminfo.pabgb"):
        pytest.skip("1.10 iteminfo fixture absent")
    return load_vanilla110("iteminfo.pabgb")


@pytest.fixture(scope="module")
def grown_table(vanilla):
    """A live table another mod already grew (socket intent)."""
    change, _ = _growth_change(vanilla)
    return bytes.fromhex(change["patched"])


def test_rebuild_sees_full_extent_of_grown_live_table(vanilla, grown_table):
    """Two separately-applied whole-table mods on the same table: the
    second change's original is vanilla-length, but the live table is
    LONGER. The rebuild must parse the table's real extent (offset to
    EOF), not a vanilla-length slice that cuts it mid-record."""
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    change = _whole_table_change(vanilla, _stack_intents())
    data = bytearray(grown_table)
    skipped: list[dict] = []
    applied, _, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=skipped)
    assert applied == 1, (
        f"rebuild on grown live table failed: "
        f"{[(s.get('label'), s.get('reason')) for s in skipped]}")

    expected = build_iteminfo_intent_change(grown_table, _stack_intents())
    assert bytes(data) == bytes.fromhex(expected["patched"]), (
        "rebuilt bytes differ from writer-on-grown-buffer output")


def test_rebuild_fires_when_live_table_shorter_than_original(
        vanilla, grown_table):
    """Mirror image: the change was built against a longer table, the
    live one is shorter, so the bounds check fires. A whole-table
    change must rebuild against the real extent instead of skipping."""
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    change = _whole_table_change(grown_table, _stack_intents())
    assert len(bytes.fromhex(change["original"])) > len(vanilla)

    data = bytearray(vanilla)
    skipped: list[dict] = []
    applied, _, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=skipped)
    assert applied == 1, (
        f"bounds-failure rebuild did not fire: "
        f"{[(s.get('label'), s.get('reason')) for s in skipped]}")

    expected = build_iteminfo_intent_change(vanilla, _stack_intents())
    assert bytes(data) == bytes.fromhex(expected["patched"]), (
        "rebuilt bytes differ from writer-on-vanilla output")


@pytest.mark.skipif(not has_vanilla110("iteminfo.pabgb"), reason="1.10 iteminfo fixture absent")
def test_growing_whole_table_change_rebuilds_on_contaminated_buffer():
    from cdumm.engine.iteminfo_native_parser import parse_first_record_size
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    vanilla = load_vanilla110("iteminfo.pabgb")
    change, intents = _growth_change(vanilla)

    # One stale VALUE byte in the second record (max_stack low byte),
    # like a tainted backup. A NAME byte would break the sniff-walk
    # framing and the pre-flight would correctly REFUSE the rebuild
    # (see test_name_corruption_refuses_rebuild_safely in
    # test_f3_whole_table_rebuild.py); real contamination flips
    # values.
    import struct as _struct
    first = parse_first_record_size(vanilla)
    strlen = _struct.unpack_from("<I", vanilla, first + 4)[0]
    spot = first + 8 + strlen + 1
    contaminated = bytearray(vanilla)
    contaminated[spot] ^= 0xFF

    data = bytearray(contaminated)
    skipped: list[dict] = []
    applied, _, _ = _apply_byte_patches(
        data, [dict(change)], skipped_out=skipped)
    assert applied == 1, (
        f"rebuild did not fire for the grown table: "
        f"{[(s.get('label'), s.get('reason')) for s in skipped]}")

    expected = build_iteminfo_intent_change(bytes(contaminated), intents)
    assert bytes(data) == bytes.fromhex(expected["patched"]), (
        "rebuilt bytes differ from writer-on-buffer output")
