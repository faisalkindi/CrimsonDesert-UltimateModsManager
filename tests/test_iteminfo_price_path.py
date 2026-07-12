"""Format 3 nested-path writes for iteminfo item prices (GitHub #259).

"Cheap Gold Bars" and similar price mods target the dotted/indexed path
``price_list[0].price.price`` (an item's sell/buy value). Two things have to
line up for that intent to apply, and before #259 the first one blocked it:

1. The import-time validator (``_diagnose_unsupported_intent``) must NOT reject
   the dotted path -- ``price_list[`` is on the iteminfo nested-path allowlist.
2. The 1.13 relocated-layout writer (``_build_change_relocated_layout``) must
   resolve the path on the parsed record and assign the new value.

price_list decodes as ``[{"key": N, "price": {"price": P, ...}}]``, so the
editable value is the path ``price_list[0].price.price``. These pin both halves
with no game fixture (CI-runnable). The whole-table byte-exact apply is verified
live against the installed 1.13 iteminfo (GoldBar key 53, 50000 -> 1: exactly
the 2 bytes of the u64 price change, 0 collateral records).
"""
from __future__ import annotations

from cdumm.engine.format3_handler import _diagnose_unsupported_intent
from cdumm.engine.iteminfo_writer import _resolve_path_target, shape_matches


def _item():
    # shape exactly as the native parser emits for a decoded 1.13 record
    return {
        "key": 53,
        "price_list": [
            {"key": 1, "price": {"price": 50000, "sym_no": 0,
                                 "item_info_wrapper": 1}}
        ],
    }


# --- validator gate (#259) ------------------------------------------------

def test_price_path_passes_validation():
    # The dotted price path must NOT be rejected at import time.
    assert _diagnose_unsupported_intent(
        "price_list[0].price.price", 1, "iteminfo") is None


def test_unrelated_iteminfo_nested_path_still_rejected():
    # The allowlist stays specific: an unknown nested path is still refused,
    # so the gate keeps its "refuse rather than guess" discipline.
    msg = _diagnose_unsupported_intent(
        "some_unknown_list[0].value", 1, "iteminfo")
    assert msg is not None and "nested" in msg.lower()


# --- writer path resolution -----------------------------------------------

def test_price_path_resolves_to_the_inner_price_value():
    it = _item()
    target = _resolve_path_target(it, "price_list[0].price.price")
    assert target is not None
    parent, seg = target
    # parent is the nested price dict; seg the final assignable key
    assert parent is it["price_list"][0]["price"]
    assert seg == "price"
    assert parent[seg] == 50000


def test_price_path_assignment_sets_the_value():
    it = _item()
    parent, seg = _resolve_path_target(it, "price_list[0].price.price")
    parent[seg] = 1  # exactly what the writer branch does
    assert it["price_list"][0]["price"]["price"] == 1


def test_price_value_shape_gate():
    # int -> int passes; a wrong-shaped new value is refused before serialize
    assert shape_matches(50000, 1) is True
    assert shape_matches(50000, [1, 2]) is False
    assert shape_matches(50000, {"x": 1}) is False


def test_price_path_bad_segments_return_none():
    it = _item()
    assert _resolve_path_target(it, "price_list[9].price.price") is None  # OOR
    assert _resolve_path_target(it, "price_list[0].nope.price") is None   # missing key
