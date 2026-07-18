"""End-to-end Format 3 apply test against the live (post-2026-04-29
game patch) iteminfo binary, exercising the full path:

    Format 3 intent
        -> build_iteminfo_intent_change (iteminfo_writer)
            -> parse_iteminfo_from_bytes (native parser)
            -> mutate dict
            -> serialize_iteminfo (native parser)
        -> v2 change dict (offset/original/patched)
        -> apply patched bytes
        -> parse the patched binary back
        -> verify the targeted item now reflects the intent

Pinned against Faisal's extracted live iteminfo at
C:/Users/faisa/AppData/Local/Temp/iteminfo_postpatch.pabgb. Skips
when the fixture is absent.
"""
from __future__ import annotations


import pytest

from tests.fixture_loaders import vanilla113_file


_LIVE_BODY = vanilla113_file("iteminfo.pabgb")


def _have_live_fixture() -> bool:
    return _LIVE_BODY.exists()


def _table():
    """The 1.13 table, parsed with the layout it is ACTUALLY in.

    These three tests were permanently skipped with the reason "selects
    specific candidate records that do not exist in the committed CD 1.13
    table (next() raises StopIteration)". That was a misdiagnosis. They
    parsed the table with NO layout, so every record came back garbage and
    of course no candidate matched. Parsed with the detected layout, the
    candidates are abundant:

        records with max_stack_count >= 1      : 6,508
        records with a non-empty passive list  :   407
        records with BOTH (mixed-intent test)  :   407

    So the tests were fine; the harness was wrong. Restored rather than
    deleted -- they cover the full Format 3 apply path end to end.
    """
    from cdumm.engine.iteminfo_native_parser import (
        detect_iteminfo_layout, parse_iteminfo_from_bytes,
    )
    from cdumm.semantic.parser import parse_pabgh_index

    body = _LIVE_BODY.read_bytes()
    header = _LIVE_BODY.with_suffix(".pabgh").read_bytes()
    _, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())
    fields = detect_iteminfo_layout(body, starts)
    assert fields is not None, "no iteminfo layout round-trips this fixture"
    items = parse_iteminfo_from_bytes(body, record_offsets=starts,
                                      fields=fields)
    return body, header, starts, fields, items


def _reparse(new_bytes, header, fields):
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.semantic.parser import parse_pabgh_index
    _, offsets = parse_pabgh_index(header, "iteminfo")
    return parse_iteminfo_from_bytes(
        new_bytes, record_offsets=sorted(offsets.values()), fields=fields)


@pytest.mark.slow
@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="CD 1.13 iteminfo fixture not present",
)
def test_format3_max_stack_count_apply_round_trip():
    """Set max_stack_count on a real item via the full Format 3 path
    and verify the patched binary parses back with the intended value.
    """
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change

    body, header, starts, fields, items = _table()

    target = next(it for it in items if it.get("max_stack_count", 0) >= 1)
    target_key = target["key"]
    new_value = 9999

    intent = Format3Intent(
        entry=target.get("string_key", str(target_key)),
        key=target_key,
        field="max_stack_count",
        op="set",
        new=new_value,
    )

    change = build_iteminfo_intent_change(body, [intent],
                                          vanilla_header=header)
    assert change is not None, (
        "build_iteminfo_intent_change returned None for a valid intent")

    new_bytes = bytes.fromhex(change["patched"])
    assert len(new_bytes) == len(body), (
        f"patched length {len(new_bytes)} != vanilla length {len(body)} "
        f"(a fixed-size primitive must not change the total size)"
    )

    new_items = _reparse(new_bytes, header, fields)
    new_target = next(it for it in new_items if it["key"] == target_key)
    assert new_target["max_stack_count"] == new_value, (
        f"after-patch max_stack_count = {new_target['max_stack_count']}, "
        f"expected {new_value}"
    )

    # Every OTHER item must be untouched. An apply that edits one item and
    # quietly perturbs a second is the corruption class this whole suite
    # exists to catch.
    before = {it["key"]: it for it in items}
    other_changed = [
        it["key"] for it in new_items
        if it["key"] != target_key and it != before[it["key"]]
    ]
    assert not other_changed, (
        f"{len(other_changed)} non-target items mutated during apply; "
        f"first: key={other_changed[0]}"
    )


@pytest.mark.slow
@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="CD 1.13 iteminfo fixture not present",
)
def test_format3_list_of_dict_apply_round_trip():
    """Set equip_passive_skill_list (a list-of-dict field) on a real item
    via the full Format 3 path and verify the patched binary parses back
    with the intended value. This is the list-of-dict writer path that was
    the original GitHub #62 bug -- and a size-CHANGING edit, so it also
    proves the .pabgh index gets rewritten correctly.
    """
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change

    body, header, starts, fields, items = _table()

    target = next(it for it in items if it.get("equip_passive_skill_list"))
    target_key = target["key"]
    new_value = []      # strip the list — the simplest distinct value

    intent = Format3Intent(
        entry=target.get("string_key", str(target_key)),
        key=target_key,
        field="equip_passive_skill_list",
        op="set",
        new=new_value,
    )

    change = build_iteminfo_intent_change(body, [intent],
                                          vanilla_header=header)
    assert change is not None

    new_bytes = bytes.fromhex(change["patched"])
    # The record shrank, so the record offsets moved: re-read the index the
    # writer rebuilt rather than reusing the vanilla one.
    companion = change.get("_pabgh_companion")
    new_header = (bytes.fromhex(companion["patched"])
                  if companion else header)
    new_items = _reparse(new_bytes, new_header, fields)
    new_target = next(it for it in new_items if it["key"] == target_key)
    assert new_target["equip_passive_skill_list"] == new_value


@pytest.mark.slow
@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="CD 1.13 iteminfo fixture not present",
)
def test_format3_mixed_primitive_and_list_of_dict_apply():
    """Real production load shape: a single mod batches multiple
    intents on the same item, mixing primitive and list-of-dict
    fields. Batched through one build_iteminfo_intent_change call,
    re-parsed, both fields must reflect their intents.
    """
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change

    body, header, starts, fields, items = _table()

    # One item with BOTH a non-zero max_stack_count and a non-empty passive
    # list, so a single batch mutates a fixed-size field and a size-changing
    # one at once — the shape a real mod actually ships.
    target = next(
        it for it in items
        if it.get("max_stack_count", 0) >= 1
        and it.get("equip_passive_skill_list")
    )
    target_key = target["key"]
    new_stack = 7777
    new_passives: list = []

    intents = [
        Format3Intent(
            entry=target.get("string_key", str(target_key)),
            key=target_key,
            field="max_stack_count",
            op="set",
            new=new_stack,
        ),
        Format3Intent(
            entry=target.get("string_key", str(target_key)),
            key=target_key,
            field="equip_passive_skill_list",
            op="set",
            new=new_passives,
        ),
    ]

    change = build_iteminfo_intent_change(body, intents,
                                          vanilla_header=header)
    assert change is not None, (
        "build_iteminfo_intent_change returned None for two valid intents")

    new_bytes = bytes.fromhex(change["patched"])
    companion = change.get("_pabgh_companion")
    new_header = (bytes.fromhex(companion["patched"])
                  if companion else header)
    new_items = _reparse(new_bytes, new_header, fields)
    new_target = next(it for it in new_items if it["key"] == target_key)
    assert new_target["max_stack_count"] == new_stack
    assert new_target["equip_passive_skill_list"] == new_passives


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="iteminfo_postpatch.pabgb fixture not present",
)
def test_format3_unknown_key_skipped_gracefully():
    """An intent targeting a key that doesn't exist in the table
    should be skipped, not crash. With no real intents surviving,
    build_iteminfo_intent_change should return None.
    """
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    from cdumm.engine.format3_handler import Format3Intent

    body = _LIVE_BODY.read_bytes()
    intent = Format3Intent(
        entry="NoSuchItem",
        key=999_999_999,
        field="max_stack_count",
        op="set",
        new=42,
    )
    change = build_iteminfo_intent_change(body, [intent])
    assert change is None, (
        "Expected None when no intent matched any real item, "
        "got a change dict instead"
    )
