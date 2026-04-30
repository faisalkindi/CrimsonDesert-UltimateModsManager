"""Iteminfo list-of-dict field writer (enchant_data_list,
equip_passive_skill_list, etc.) using the vendored crimson_rs Rust
parser/serializer.

Bug from UnLuckyLust on GitHub #55: NattKh-exported Format 3 mods
targeting iteminfo.pabgb with `enchant_data_list` etc. were
skipped. crimson_rs (MPL-2.0, vendored at src/cdumm/_vendor/) does
byte-perfect parse + serialize on the full iteminfo table, so we
can intercept these intents, apply them in-memory, and produce the
new bytes for the apply pipeline.
"""
from __future__ import annotations
from pathlib import Path

import pytest


_VANILLA_ITEMINFO = Path(r"C:\Users\faisa\AppData\Local\Temp\iteminfo.pabgb")


def _have_iteminfo() -> bool:
    return _VANILLA_ITEMINFO.exists()


def test_crimson_rs_loader_returns_module_on_py313():
    """The loader must successfully import the vendored crimson_rs
    Rust extension on Python 3.13 (CDUMM's PyInstaller build target)."""
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        pytest.skip("crimson_rs not loadable in this environment")
    assert hasattr(crimson_rs, "parse_iteminfo_from_bytes")
    assert hasattr(crimson_rs, "serialize_iteminfo")


@pytest.mark.skipif(not _have_iteminfo(),
                    reason="vanilla iteminfo extract not present")
def test_iteminfo_roundtrip_byte_identical():
    """crimson_rs.parse + serialize on vanilla iteminfo.pabgb
    produces byte-identical output. This is our trust anchor for
    using the parser to apply Format 3 intents."""
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        pytest.skip("crimson_rs not loadable")
    vanilla = _VANILLA_ITEMINFO.read_bytes()
    items = crimson_rs.parse_iteminfo_from_bytes(vanilla)
    re_encoded = crimson_rs.serialize_iteminfo(items)
    assert re_encoded == vanilla, (
        f"crimson_rs round-trip not byte-identical "
        f"(orig={len(vanilla)} bytes, new={len(re_encoded)} bytes)")


@pytest.mark.skipif(not _have_iteminfo(),
                    reason="vanilla iteminfo extract not present")
def test_iteminfo_writer_applies_enchant_data_list_intent():
    """A Format 3 intent setting `enchant_data_list` on an iteminfo
    record produces a v2-style change whose `patched` bytes, when
    applied to vanilla, yield a table where the target item's
    enchant_data_list equals the intent's `new` value."""
    from cdumm.engine.iteminfo_writer import (
        build_iteminfo_intent_change,
    )
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        pytest.skip("crimson_rs not loadable")

    vanilla = _VANILLA_ITEMINFO.read_bytes()
    items = crimson_rs.parse_iteminfo_from_bytes(vanilla)

    # Pick an item that has at least one enchant entry
    target = next(it for it in items if it.get("enchant_data_list"))
    target_key = target["key"]
    new_value = [{
        "level": 0,
        "enchant_stat_data": {
            "max_stat_list": [],
            "regen_stat_list": [],
            "stat_list_static": [],
            "stat_list_static_level": [
                {"stat": 1000011, "change_mb": 15}
            ],
        },
        "buy_price_list": [
            {"key": 1, "price": {"price": 1, "sym_no": 0,
                                  "item_info_wrapper": 1}},
        ],
        "equip_buffs": [
            {"buff": 1000100, "level": 15},
            {"buff": 1000107, "level": 15},
        ],
    }]

    intent = Format3Intent(
        entry=target.get("string_key", ""),
        key=target_key,
        field="enchant_data_list",
        op="set",
        new=new_value,
    )

    change = build_iteminfo_intent_change(vanilla, [intent])
    assert change is not None, "Expected a change dict"
    # The change is whole-file: offset 0, original=vanilla, patched=new bytes
    assert change["offset"] == 0
    assert bytes.fromhex(change["original"]) == vanilla

    new_bytes = bytes.fromhex(change["patched"])
    new_items = crimson_rs.parse_iteminfo_from_bytes(new_bytes)
    new_items_by_key = {it["key"]: it for it in new_items}
    assert target_key in new_items_by_key
    edl = new_items_by_key[target_key].get("enchant_data_list")
    assert edl is not None and len(edl) == 1
    assert len(edl[0]["equip_buffs"]) == 2
    assert edl[0]["equip_buffs"][0]["buff"] == 1000100


@pytest.mark.skipif(not _have_iteminfo(),
                    reason="vanilla iteminfo extract not present")
def test_iteminfo_writer_handles_unknown_key_gracefully():
    """An intent targeting a non-existent item key should be skipped
    (return None) rather than crashing."""
    from cdumm.engine.iteminfo_writer import (
        build_iteminfo_intent_change,
    )
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        pytest.skip("crimson_rs not loadable")

    vanilla = _VANILLA_ITEMINFO.read_bytes()
    intent = Format3Intent(
        entry="DoesNotExist", key=999_999_999,
        field="enchant_data_list", op="set", new=[],
    )
    change = build_iteminfo_intent_change(vanilla, [intent])
    # No matching key, no changes applied -> no change emitted
    assert change is None
