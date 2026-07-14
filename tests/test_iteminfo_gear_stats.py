"""Gear stats (armour / weapon) are readable AND editable on CD 1.13.

Two things had to be true and only one of them was.

READ was unblocked by the equipment-record desync fix: all 3151 equipment
records now decode, so `sharpness_data.stat_list` and the per-tier
`enchant_data_list[N].enchant_stat_data.*` lists carry real values.

WRITE was still silently broken. On CD 1.13 the writer takes the
"relocated layout" path, and that path only ever did FLAT field
resolution -- the nested-path branch existed solely in the default
(pre-1.13) writer. So every gear-stat intent was counted as an
"unwritable field" and dropped, on the only game version anyone is
playing. The mod applied cleanly and changed nothing: no error, no
warning, no effect. Both writers now share one apply helper, so they
cannot drift apart again.

The path dialect was also split: `match` accepted `list.0.field` while
`set` only accepted `list[0].field`. A mod author has no way to guess
that. Both are accepted on both sides now, and pinned here.
"""
from __future__ import annotations


from tests.fixture_loaders import load_vanilla113

from cdumm.engine.iteminfo_native_parser import (
    detect_iteminfo_layout, parse_iteminfo_from_bytes, serialize_iteminfo)
from cdumm.engine.iteminfo_writer import (
    apply_nested_intent, build_iteminfo_intent_change, is_nested_path)

HELM_KEY = 14510                       # Marni_Devotee_PlateArmor_Helm
HELM_NAME = "Marni_Devotee_PlateArmor_Helm"

SHARP_PATH = "sharpness_data.stat_list.0.change_mb"          # dotted
ENCHANT_PATH = ("enchant_data_list[0].enchant_stat_data"     # bracket
                ".stat_list_static[0].change_mb")
MAXSHARP_PATH = "sharpness_data.max_sharpness"


class _Intent:
    def __init__(self, entry, key, field, new, op="set", old=None):
        self.entry, self.key, self.field = entry, key, field
        self.op, self.new, self.old = op, new, old


def _table():
    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    n = int.from_bytes(header[:2], "little")
    starts = sorted(
        int.from_bytes(header[2 + i * 8 + 4:2 + i * 8 + 8], "little")
        for i in range(n))
    fields = detect_iteminfo_layout(body, starts)
    items = parse_iteminfo_from_bytes(body, starts, fields=fields)
    return body, header, starts, fields, items


# ── read ────────────────────────────────────────────────────────────────

def test_equipment_stats_decode_with_real_values():
    _b, _h, _s, _f, items = _table()
    equip = [i for i in items if i.get("equip_type_info")]
    assert len(equip) > 3000, f"only {len(equip)} equipment records"
    assert [i for i in items if "_opaque_record" in i] == []

    with_stats = [
        i for i in equip
        if any((t.get("enchant_stat_data") or {}).get(k)
               for t in (i.get("enchant_data_list") or [])
               for k in ("max_stat_list", "regen_stat_list",
                         "stat_list_static", "stat_list_static_level"))]
    assert len(with_stats) > 3000, (
        f"only {len(with_stats)} equipment records carry any stat list; "
        f"the equipment desync fix is what makes these readable")

    helm = next(i for i in items if i["key"] == HELM_KEY)
    assert helm["sharpness_data"]["stat_list"] == [
        {"stat": 1000003, "change_mb": 1000}]
    assert (helm["enchant_data_list"][0]["enchant_stat_data"]
            ["stat_list_static"] == [{"stat": 1000003, "change_mb": 2000}])


def test_enchant_stat_progression_never_decreases():
    """ORACLE. Enchanting an item makes it better, so a stat's value
    across tier 0..N must be non-decreasing.

    This is the check that a wrong-but-same-size layout cannot survive:
    such a layout still round-trips byte-exact and still "decodes", but
    it scrambles which bytes belong to which tier, and the monotonic
    order collapses. Passing this is what makes the numbers trustworthy
    rather than merely well-formed.
    """
    _b, _h, _s, _f, items = _table()
    checked = violations = 0
    for it in items:
        tiers = it.get("enchant_data_list") or []
        if len(tiers) < 3:
            continue
        series: dict[int, list[int]] = {}
        for t in tiers:
            for s in ((t.get("enchant_stat_data") or {})
                      .get("stat_list_static") or []):
                series.setdefault(s["stat"], []).append(s["change_mb"])
        for stat, vals in series.items():
            if len(vals) != len(tiers):
                continue
            checked += 1
            if any(b < a for a, b in zip(vals, vals[1:])):
                violations += 1
    assert checked > 1000, f"only {checked} stat series checked"
    assert violations == 0, f"{violations} of {checked} series decrease"


# ── path dialect ────────────────────────────────────────────────────────

def test_both_index_dialects_resolve():
    """`match` accepts list.0.field; `set` historically only accepted
    list[0].field. Both must work on both sides -- a mod author cannot
    be expected to guess which verb wants which syntax."""
    rec = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert apply_nested_intent(rec, "a.b.1.c", 9) == "ok"
    assert rec["a"]["b"][1]["c"] == 9
    assert apply_nested_intent(rec, "a.b[0].c", 8) == "ok"
    assert rec["a"]["b"][0]["c"] == 8


def test_is_nested_path():
    assert is_nested_path("a.b") and is_nested_path("a[0]")
    assert not is_nested_path("max_stack_count")


def test_nested_misses_are_reported_not_silently_applied():
    rec = {"a": {"b": [{"c": 1}]}}
    assert apply_nested_intent(rec, "a.b.9.c", 1) == "unresolved"
    assert apply_nested_intent(rec, "a.nope.c", 1) == "unresolved"
    # shape gate: a dict where an int lives must be refused, not written
    assert apply_nested_intent(rec, "a.b.0.c", {"x": 1}) == "shape"
    assert rec["a"]["b"][0]["c"] == 1


# ── write ───────────────────────────────────────────────────────────────

def test_gear_stat_intents_apply_on_the_1_13_writer():
    """The regression guard. Before the fix this emitted NO change at
    all: the 1.13 relocated writer dropped all three as 'unwritable
    field'. `build_iteminfo_intent_change` returning None here means
    gear stats have silently become uneditable again."""
    body, header, starts, _f, items = _table()
    weapon = next(i for i in items
                  if (i.get("sharpness_data") or {}).get("max_sharpness"))

    intents = [
        _Intent(HELM_NAME, HELM_KEY, SHARP_PATH, 7777),
        _Intent(HELM_NAME, HELM_KEY, ENCHANT_PATH, 8888),
        _Intent(weapon["string_key"], weapon["key"], MAXSHARP_PATH, 999),
    ]
    change = build_iteminfo_intent_change(body, intents,
                                          vanilla_header=header)
    assert change is not None, (
        "writer emitted no change -- gear-stat intents are being dropped "
        "again (they were counted as 'unwritable field' before this fix)")

    new_body = bytes.fromhex(change["patched"])
    assert len(new_body) == len(body), "a scalar set must not resize"

    fields2 = detect_iteminfo_layout(new_body, starts)
    items2 = parse_iteminfo_from_bytes(new_body, starts, fields=fields2)
    by2 = {i["key"]: i for i in items2}

    helm = by2[HELM_KEY]
    assert helm["sharpness_data"]["stat_list"][0]["change_mb"] == 7777
    assert (helm["enchant_data_list"][0]["enchant_stat_data"]
            ["stat_list_static"][0]["change_mb"] == 8888)
    assert by2[weapon["key"]]["sharpness_data"]["max_sharpness"] == 999

    # no records lost, none carried opaque, and the result round-trips
    assert len(items2) == len(items)
    assert [i for i in items2 if "_opaque_record" in i] == []
    assert serialize_iteminfo(items2, fields=fields2) == new_body


def test_gear_stat_edit_has_zero_collateral():
    """Editing one item's stats must not touch any other item.

    A whole-table rebuild is the mechanism here, so 'it applied' proves
    nothing on its own -- the interesting question is what ELSE moved.
    """
    body, header, starts, _f, items = _table()
    before = {i["key"]: i for i in items}

    change = build_iteminfo_intent_change(
        body, [_Intent(HELM_NAME, HELM_KEY, SHARP_PATH, 7777)],
        vanilla_header=header)
    assert change is not None
    new_body = bytes.fromhex(change["patched"])

    items2 = parse_iteminfo_from_bytes(
        new_body, starts, fields=detect_iteminfo_layout(new_body, starts))
    after = {i["key"]: i for i in items2}

    assert set(before) == set(after), "records added or lost"
    differing = [k for k in before if before[k] != after[k]]
    assert differing == [HELM_KEY], (
        f"{len(differing)} records changed, expected only {HELM_KEY}: "
        f"{differing[:8]}")

    # 1000 -> 7777 differs in the low two bytes of the i64 only
    diff = [i for i in range(len(body)) if body[i] != new_body[i]]
    assert len(diff) == 2, f"{len(diff)} bytes changed, expected 2"
    assert diff == list(range(diff[0], diff[0] + 2))


def test_unresolvable_gear_path_is_skipped_not_guessed():
    """A path that does not exist must produce no change, rather than
    landing the value somewhere plausible-looking."""
    body, header, _s, _f, _i = _table()
    change = build_iteminfo_intent_change(
        body,
        [_Intent(HELM_NAME, HELM_KEY,
                 "sharpness_data.stat_list.99.change_mb", 1)],
        vanilla_header=header)
    assert change is None
