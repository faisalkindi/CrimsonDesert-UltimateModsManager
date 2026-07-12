"""Nested iteminfo paths must survive VALIDATION, not just the writer.

GitHub #259 (Cheap Gold Bars, Srimk1 + lupo1190): the mod edits
`price_list[0].price.price`. The writer has handled that since the
nested-path fix -- but `validate_intents` refused it up front against a
hardcoded allowlist of prefixes, so the intent never reached the writer
and the user got "author needs to add a field_schema entry": advice they
cannot act on, for a field the engine could already write.

The allowlist had been grown one bug report at a time (prefab_data_list[,
drop_default_data., gimmick_visual_prefab_data_list[, docking_child_data),
which meant every new nested path a mod author invented was a code change.

The part that should sting: **gear stats were behind the same gate.**
`sharpness_data.stat_list[0].change_mb` and the enchant paths were refused
here too, so the entire gear-stat feature was dead end-to-end -- while its
tests passed, because they called `build_iteminfo_intent_change` directly
and never crossed validation. An allowlist nobody tests is a feature flag
nobody knows is off.

So these tests deliberately go through the FULL path (validate -> write),
which is the one thing the gear-stat tests didn't do.
"""
from __future__ import annotations

import pytest

from tests.fixture_loaders import load_vanilla113

from cdumm.engine.format3_handler import Format3Intent, validate_intents
from cdumm.engine.iteminfo_native_parser import (
    detect_iteminfo_layout, parse_iteminfo_from_bytes)
from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change

TARGET = "iteminfo.pabgb"

# Every nested shape a real mod has actually used. Each was, at some point,
# either allowlisted by hand or silently rejected.
NESTED_PATHS = [
    "price_list[0].price.price",                       # #259 Cheap Gold Bars
    "sharpness_data.stat_list[0].change_mb",           # gear stats
    "sharpness_data.max_sharpness",                    # gear stats
    ("enchant_data_list[0].enchant_stat_data"
     ".stat_list_static[0].change_mb"),                # gear stats
    "drop_default_data.use_socket",                    # sockets
    "drop_default_data.add_socket_material_item_list",
    "prefab_data_list[0].tribe_gender_list",
    "gimmick_visual_prefab_data_list[0].x",
    "docking_child_data.inherit_summoner",
]


def _intent(field, new=1, key=1000080, entry="X"):
    return Format3Intent(entry=entry, key=key, field=field, op="set",
                         new=new, old=None, match=None)


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


@pytest.mark.parametrize("field", NESTED_PATHS)
def test_nested_iteminfo_path_passes_validation(field):
    v = validate_intents(TARGET, [_intent(field)])
    assert len(v.supported) == 1 and not v.skipped, (
        f"{field!r} was refused at validation: "
        f"{v.skipped[0][1] if v.skipped else ''}")


def test_flat_fields_still_validate():
    for f in ("max_stack_count", "cooltime", "equipable_hash"):
        v = validate_intents(TARGET, [_intent(f)])
        assert len(v.supported) == 1, f"{f} refused"


def test_gear_stat_intent_survives_validate_then_write():
    """The end-to-end the gear-stat tests skipped.

    Writer-only coverage said gear stats worked. They did not: validation
    dropped them first. Assert the whole chain.
    """
    body, header, starts, items = _table()
    helm = next(i for i in items
                if (i.get("sharpness_data") or {}).get("stat_list"))

    intent = _intent("sharpness_data.stat_list[0].change_mb", new=7777,
                     key=helm["key"], entry=helm["string_key"])
    v = validate_intents(TARGET, [intent])
    assert len(v.supported) == 1, "gear-stat intent refused at validation"

    change = build_iteminfo_intent_change(
        body, v.supported, vanilla_header=header)
    assert change is not None, "validated gear-stat intent produced no change"

    new_body = bytes.fromhex(change["patched"])
    items2 = parse_iteminfo_from_bytes(
        new_body, starts, fields=detect_iteminfo_layout(new_body, starts))
    by2 = {i["key"]: i for i in items2}
    assert (by2[helm["key"]]["sharpness_data"]["stat_list"][0]["change_mb"]
            == 7777)


def test_price_edit_survives_validate_then_write_byte_exact():
    """GitHub #259, the whole way through: validate -> write -> re-parse."""
    body, header, starts, items = _table()
    before = {i["key"]: i for i in items}

    item = next(i for i in items if i.get("price_list"))
    old_price = item["price_list"][0]["price"]["price"]

    intent = _intent("price_list[0].price.price", new=1,
                     key=item["key"], entry=item["string_key"])
    v = validate_intents(TARGET, [intent])
    assert len(v.supported) == 1, (
        f"price intent refused at validation: "
        f"{v.skipped[0][1] if v.skipped else ''}")

    change = build_iteminfo_intent_change(
        body, v.supported, vanilla_header=header)
    assert change is not None
    new_body = bytes.fromhex(change["patched"])
    assert len(new_body) == len(body), "a scalar price set must not resize"

    items2 = parse_iteminfo_from_bytes(
        new_body, starts, fields=detect_iteminfo_layout(new_body, starts))
    after = {i["key"]: i for i in items2}

    assert after[item["key"]]["price_list"][0]["price"]["price"] == 1
    assert old_price != 1, "pick an item whose price actually changes"

    # zero collateral
    assert [i for i in items2 if "_opaque_record" in i] == []
    differing = [k for k in before if before[k] != after.get(k)]
    assert differing == [item["key"]], (
        f"expected only {item['key']} to change: {differing[:6]}")


def test_typoed_root_is_still_rejected_at_import():
    """The fix must not swing to the other extreme.

    Accepting *any* nested path would turn a typo into a silent "applied,
    0 changes" — worse than the bug it fixes. Acceptance is on the ROOT
    field existing on an iteminfo record, so a real root gets through and
    a made-up one still gets a real error at import.
    """
    v = validate_intents(TARGET, [_intent("not_a_field.nope.nope")])
    assert not v.supported and len(v.skipped) == 1


def test_root_that_exists_but_leaf_that_does_not_is_a_clean_skip():
    """A real root with a bogus leaf can only be caught by the decoded
    record, so validation lets it through and the WRITER refuses it —
    0 byte changes, never a corrupted table."""
    body, header, _starts, _items = _table()
    v = validate_intents(
        TARGET, [_intent("price_list[0].nope.nope", key=1000425)])
    assert len(v.supported) == 1          # plausible root: defer to writer
    change = build_iteminfo_intent_change(
        body, v.supported, vanilla_header=header)
    assert change is None                  # writer emits nothing


def test_engine_dialect_root_is_accepted():
    """Mods in the underscored/camelCase dialect (_priceList) resolve to
    the same root."""
    v = validate_intents(TARGET, [_intent("_priceList[0].price.price")])
    assert len(v.supported) == 1
