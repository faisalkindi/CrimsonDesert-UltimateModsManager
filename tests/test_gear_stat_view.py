"""The gear-stat locator addresses every stat exactly, and nothing else.

These assert against the real CD 1.13 iteminfo table (the committed
fixture), not synthetic records, because the whole point of this module is
that it doesn't guess -- and a synthetic record can't prove that.

The counts below are measured from vanilla. If a future game version moves
the data, these fail loudly instead of the editor quietly showing the wrong
numbers, which is exactly the failure the old byte-scanner had.
"""
from __future__ import annotations

import pytest

from tests.fixture_loaders import load_vanilla113

from cdumm.engine.format3_handler import Format3Intent, validate_intents
from cdumm.engine.gear_stat_view import (
    ENCHANT_STAT_LISTS, GearStat, locate_all_gear_stats, locate_gear_stats)
from cdumm.engine.iteminfo_native_parser import (
    detect_iteminfo_layout, parse_iteminfo_from_bytes, serialize_iteminfo)
from cdumm.engine.iteminfo_writer import (apply_nested_intent,
                                          build_iteminfo_intent_change)

HELM_KEY = 14510                       # Marni_Devotee_PlateArmor_Helm
HELM_NAME = "Marni_Devotee_PlateArmor_Helm"
TARGET = "iteminfo.pabgb"

# Measured from vanilla CD 1.13 (tests/fixtures/vanilla113/iteminfo).
VANILLA_STAT_ENTRIES = 28_081
VANILLA_RECORDS_WITH_STATS = 3_319


@pytest.fixture(scope="module")
def table():
    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    n = int.from_bytes(header[:2], "little")
    starts = sorted(
        int.from_bytes(header[2 + i * 8 + 4:2 + i * 8 + 8], "little")
        for i in range(n))
    fields = detect_iteminfo_layout(body, starts)
    items = parse_iteminfo_from_bytes(body, starts, fields=fields)
    return body, starts, fields, items


@pytest.fixture(scope="module")
def by_key(table):
    _body, _starts, _fields, items = table
    return {it["key"]: it for it in items if "key" in it}


# ── it finds all of them ────────────────────────────────────────────────

def test_finds_every_stat_in_the_real_table(by_key):
    found = locate_all_gear_stats(by_key)
    total = sum(len(v) for v in found.values())
    assert total == VANILLA_STAT_ENTRIES, (
        f"located {total} stats, vanilla has {VANILLA_STAT_ENTRIES}")
    assert len(found) == VANILLA_RECORDS_WITH_STATS


def test_it_beats_the_scanner_it_replaces(by_key):
    """The byte-scanner in #261/#269 found 21,420 of the 28,081 real stats
    and invented some that don't exist. Whatever else changes, this locator
    must not regress below the truth."""
    total = sum(len(v) for v in locate_all_gear_stats(by_key).values())
    assert total > 21_420


def test_non_equipment_records_have_no_stats(by_key):
    found = locate_all_gear_stats(by_key)
    # most of the table is consumables/materials/quest items
    assert len(found) < len(by_key)
    for stats in found.values():
        assert stats, "a record in the result must never have an empty list"


# ── every stat is addressed exactly ─────────────────────────────────────

def test_the_helm_reads_its_real_base_and_tier_values(by_key):
    stats = locate_gear_stats(by_key[HELM_KEY])
    base = [s for s in stats if s.group == "Base"]
    assert base == [GearStat(
        path="sharpness_data.stat_list[0].change_mb",
        stat=1000003, value=1000, group="Base", kind="")]

    tier0 = [s for s in stats if s.group == "Enhance +0" and s.kind == "flat"]
    assert tier0 == [GearStat(
        path=("enchant_data_list[0].enchant_stat_data"
              ".stat_list_static[0].change_mb"),
        stat=1000003, value=2000, group="Enhance +0", kind="flat")]


def test_the_same_stat_appears_on_every_tier_separately(by_key):
    """The bug the old editor had: it deduped by stat id and wrote only the
    FIRST occurrence, so editing a stat changed the base and left every
    enhancement tier alone. Each tier must be its own addressable entry."""
    stats = locate_gear_stats(by_key[HELM_KEY])
    dpv = [s for s in stats if s.stat == 1000003]
    assert len(dpv) > 1, "stat 1000003 occurs on the base AND every tier"
    assert len({s.path for s in dpv}) == len(dpv), "paths must be unique"
    groups = {s.group for s in dpv}
    assert "Base" in groups and len(groups) > 2


def test_every_path_is_unique_across_the_whole_table(by_key):
    for key, stats in locate_all_gear_stats(by_key).items():
        paths = [s.path for s in stats]
        assert len(paths) == len(set(paths)), f"duplicate path on item {key}"


# ── the paths actually resolve and write ────────────────────────────────

def test_every_located_path_resolves_on_its_own_record(by_key):
    """A path we show the user must be one the writer can reach. Not a
    sample -- every stat in the table, or the guarantee is worthless."""
    found = locate_all_gear_stats(by_key)
    checked = 0
    for key, stats in found.items():
        record = by_key[key]
        for s in stats:
            assert apply_nested_intent(record, s.path, s.value) == "ok", (
                f"item {key}: {s.path} did not resolve")
            checked += 1
    assert checked == VANILLA_STAT_ENTRIES


def test_an_edit_through_a_located_path_round_trips_byte_exact(table, by_key):
    body, starts, fields, _items = table
    record = by_key[HELM_KEY]
    target = next(s for s in locate_gear_stats(record)
                  if s.group == "Base")

    assert apply_nested_intent(record, target.path, 7777) == "ok"
    items = parse_iteminfo_from_bytes(body, starts, fields=fields)
    items = [record if it.get("key") == HELM_KEY else it for it in items]
    rebuilt = serialize_iteminfo(items, fields=fields)

    reread = parse_iteminfo_from_bytes(
        rebuilt, sorted(_starts_of(rebuilt, starts, body)), fields=fields)
    helm = next(i for i in reread if i.get("key") == HELM_KEY)
    assert helm["sharpness_data"]["stat_list"][0]["change_mb"] == 7777
    # and the value we located is the one that moved
    assert locate_gear_stats(helm)[0].value == 7777


def _starts_of(rebuilt, starts, body):
    """Record starts survive a same-width scalar edit, so reuse them."""
    assert len(rebuilt) == len(body), "a scalar set must not resize the table"
    return starts


# ── shape guards ────────────────────────────────────────────────────────

def test_every_vanilla_stat_entry_has_the_shape_we_assume(by_key):
    """The locator only addresses entries shaped {stat, change_mb}. If the
    game ever ships a different shape we want to know, not skip silently."""
    seen = set()
    for record in by_key.values():
        sd = record.get("sharpness_data") or {}
        for e in sd.get("stat_list") or []:
            seen.add(tuple(sorted(e)))
        for tier in record.get("enchant_data_list") or []:
            block = (tier or {}).get("enchant_stat_data") or {}
            for name, _kind in ENCHANT_STAT_LISTS:
                for e in block.get(name) or []:
                    seen.add(tuple(sorted(e)))
    assert seen == {("change_mb", "stat")}


def test_junk_records_do_not_raise():
    assert locate_gear_stats({}) == []
    assert locate_gear_stats({"sharpness_data": None}) == []
    assert locate_gear_stats({"sharpness_data": {"stat_list": "nope"}}) == []
    assert locate_gear_stats({"enchant_data_list": [None, 5]}) == []
    # an entry missing change_mb is skipped, not guessed at
    assert locate_gear_stats(
        {"sharpness_data": {"stat_list": [{"stat": 1}]}}) == []
    assert locate_all_gear_stats({1: None, 2: {}}) == {}


def test_where_label_reads_for_humans():
    base = GearStat("p", 1, 2, "Base", "")
    tier = GearStat("p", 1, 2, "Enhance +3", "flat")
    assert base.where == "Base"
    assert tier.where == "Enhance +3 (flat)"


# ── the whole chain, the way the editor actually drives it ──────────────

def test_editing_two_tiers_of_one_stat_writes_both(table, by_key):
    """The end-to-end guarantee, through validation and the writer.

    This is the test the old gear-stat work never had: it goes through
    `validate_intents` (the gate that silently refused every nested iteminfo
    path until #281) and then the real writer, rather than calling the
    writer directly. And it edits the SAME stat on two different tiers,
    which is precisely what the deduping editor could not do — it would
    have written one of them and dropped the other.
    """
    body, _starts, _fields, _items = table
    header = load_vanilla113("iteminfo.pabgh")

    stats = locate_gear_stats(by_key[HELM_KEY])
    dpv = [s for s in stats if s.stat == 1000003]
    base = next(s for s in dpv if s.group == "Base")
    tier = next(s for s in dpv if s.group != "Base")
    assert base.path != tier.path

    intents = [
        Format3Intent(entry=HELM_NAME, key=HELM_KEY, field=base.path,
                      op="set", new=4321, old=base.value),
        Format3Intent(entry=HELM_NAME, key=HELM_KEY, field=tier.path,
                      op="set", new=8765, old=tier.value),
    ]

    result = validate_intents(TARGET, intents)
    assert not result.skipped, (
        "the editor's own paths were refused by validation: "
        f"{[(i.field, why) for i, why in result.skipped]}")
    assert len(result.supported) == 2

    change = build_iteminfo_intent_change(body, intents,
                                          vanilla_header=header)
    assert change is not None, (
        "the writer emitted no change — the editor's gear-stat intents are "
        "being dropped")
    new_body = bytes.fromhex(change["patched"])
    assert len(new_body) == len(body), "a scalar set must not resize the table"

    reread = parse_iteminfo_from_bytes(
        new_body, _starts, fields=_fields)
    helm = next(i for i in reread if i.get("key") == HELM_KEY)
    after = {s.path: s.value for s in locate_gear_stats(helm)}
    assert after[base.path] == 4321
    assert after[tier.path] == 8765, (
        "the enhancement tier was not written — this is the exact bug the "
        "deduping editor shipped")
