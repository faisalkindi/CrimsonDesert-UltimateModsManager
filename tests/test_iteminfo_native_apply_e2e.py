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

from pathlib import Path

import pytest

from tests.fixture_loaders import vanilla113_file


_LIVE_BODY = vanilla113_file("iteminfo.pabgb")


def _have_live_fixture() -> bool:
    return _LIVE_BODY.exists()


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="iteminfo_postpatch.pabgb fixture not present",
)
@pytest.mark.skip(
    reason="pinned to the CONTENTS of a pre-1.13 extract: it selects specific candidate records that do not exist in the committed CD 1.13 table (next() raises StopIteration). Re-picking candidates would change what the test asserts, so it stays skipped rather than be quietly rewritten. The apply round-trip it covers is exercised on real 1.13 bytes by test_iteminfo_gear_stats.py and test_format3_array_append_iteminfo.py. Was previously skipped via a hardcoded C:/Users/faisa/... path.")
def test_format3_max_stack_count_apply_round_trip():
    """Set max_stack_count on a real item via the full Format 3 path
    and verify the patched binary parses back with the intended value.
    """
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    from cdumm.engine.format3_handler import Format3Intent

    body = _LIVE_BODY.read_bytes()
    items = parse_iteminfo_from_bytes(body)

    # Pick the first item that has a non-zero max_stack_count we can
    # bump to a sentinel value distinct from anything else.
    target = next(
        it for it in items
        if it.get("max_stack_count", 0) >= 1
    )
    target_key = target["key"]
    new_value = 9999

    intent = Format3Intent(
        entry=target.get("string_key", str(target_key)),
        key=target_key,
        field="max_stack_count",
        op="set",
        new=new_value,
    )

    change = build_iteminfo_intent_change(body, [intent])
    assert change is not None, (
        "build_iteminfo_intent_change returned None for a valid intent")

    new_bytes = bytes.fromhex(change["patched"])
    assert len(new_bytes) == len(body), (
        f"patched length {len(new_bytes)} != vanilla length {len(body)} "
        f"(fixed-size primitive shouldn't change total size)"
    )

    new_items = parse_iteminfo_from_bytes(new_bytes)
    new_target = next(it for it in new_items if it["key"] == target_key)
    assert new_target["max_stack_count"] == new_value, (
        f"after-patch max_stack_count = {new_target['max_stack_count']}, "
        f"expected {new_value}"
    )

    # All OTHER items must be unchanged byte-for-byte (round-trip
    # identity for non-targeted items).
    other_changed = [
        new_it for new_it in new_items
        if new_it["key"] != target_key
        and new_it != next(
            it for it in items if it["key"] == new_it["key"]
        )
    ]
    assert not other_changed, (
        f"{len(other_changed)} non-target items mutated during apply; "
        f"first: key={other_changed[0]['key']}"
    )


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="iteminfo_postpatch.pabgb fixture not present",
)
@pytest.mark.skip(
    reason="pinned to the CONTENTS of a pre-1.13 extract: it selects specific candidate records that do not exist in the committed CD 1.13 table (next() raises StopIteration). Re-picking candidates would change what the test asserts, so it stays skipped rather than be quietly rewritten. The apply round-trip it covers is exercised on real 1.13 bytes by test_iteminfo_gear_stats.py and test_format3_array_append_iteminfo.py. Was previously skipped via a hardcoded C:/Users/faisa/... path.")
def test_format3_list_of_dict_apply_round_trip():
    """Set equip_passive_skill_list (a list-of-dict field) on a real
    item via the full Format 3 path and verify the patched binary
    parses back with the intended value. This exercises the
    list-of-dict writer path that was the original GitHub #62 bug
    (originally reported on enchant_data_list, which the post-1.0.4.1
    schema removed; equip_passive_skill_list is the equivalent
    list-of-dict path that still exists).
    """
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    from cdumm.engine.format3_handler import Format3Intent

    body = _LIVE_BODY.read_bytes()
    items = parse_iteminfo_from_bytes(body)

    target = next(
        it for it in items
        if it.get("equip_passive_skill_list")
        and len(it["equip_passive_skill_list"]) > 0
    )
    target_key = target["key"]
    new_value = []  # Strip the list — simplest distinct value.

    intent = Format3Intent(
        entry=target.get("string_key", str(target_key)),
        key=target_key,
        field="equip_passive_skill_list",
        op="set",
        new=new_value,
    )

    change = build_iteminfo_intent_change(body, [intent])
    assert change is not None

    new_bytes = bytes.fromhex(change["patched"])
    new_items = parse_iteminfo_from_bytes(new_bytes)
    new_target = next(it for it in new_items if it["key"] == target_key)
    assert new_target["equip_passive_skill_list"] == new_value


@pytest.mark.skipif(
    not _have_live_fixture(),
    reason="iteminfo_postpatch.pabgb fixture not present",
)
@pytest.mark.skip(
    reason="pinned to the CONTENTS of a pre-1.13 extract: it selects specific candidate records that do not exist in the committed CD 1.13 table (next() raises StopIteration). Re-picking candidates would change what the test asserts, so it stays skipped rather than be quietly rewritten. The apply round-trip it covers is exercised on real 1.13 bytes by test_iteminfo_gear_stats.py and test_format3_array_append_iteminfo.py. Was previously skipped via a hardcoded C:/Users/faisa/... path.")
def test_format3_mixed_primitive_and_list_of_dict_apply():
    """Real production load shape: a single mod batches multiple
    intents on the same item, mixing primitive and list-of-dict
    fields. Batched through one build_iteminfo_intent_change call,
    re-parsed, both fields must reflect their intents.
    """
    from cdumm.engine.iteminfo_native_parser import parse_iteminfo_from_bytes
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change
    from cdumm.engine.format3_handler import Format3Intent

    body = _LIVE_BODY.read_bytes()
    items = parse_iteminfo_from_bytes(body)

    # Pick a single item that has BOTH a non-zero max_stack_count
    # and a non-empty equip_passive_skill_list, so we can mutate
    # both via the same batch.
    target = next(
        it for it in items
        if it.get("max_stack_count", 0) >= 1
        and it.get("equip_passive_skill_list")
        and len(it["equip_passive_skill_list"]) > 0
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

    change = build_iteminfo_intent_change(body, intents)
    assert change is not None, (
        "build_iteminfo_intent_change returned None for two valid intents")

    new_bytes = bytes.fromhex(change["patched"])
    new_items = parse_iteminfo_from_bytes(new_bytes)
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
