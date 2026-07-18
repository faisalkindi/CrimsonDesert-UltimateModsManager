"""The stat table is checkable against the game data, so check it.

The widely-circulated community mapping (`buff_names_community.json`) is
wrong on at least seven ids -- it has 1000006/1000007 SWAPPED, calls
1000012 "Casting Speed Rate" when the game calls it ClimbSpeedRate, and
so on. Every entry in it is marked `verified: true`. A mod built against
it silently boosts a different stat than the author intended.

So the snapshot in stat_names.py is not asserted by fiat. Two independent
properties of the vanilla data have to hold, and both run in CI against
the committed 1.13 iteminfo fixture.
"""
from __future__ import annotations

from tests.fixture_loaders import load_vanilla113

from cdumm.engine.iteminfo_native_parser import (
    detect_iteminfo_layout, parse_iteminfo_from_bytes)
from cdumm.engine.stat_names import (
    STAT_NAMES_CD113, load_stat_names, parse_stat_names, stat_label)

BASELINE = {1000002, 1000003}     # DDD + DPV: on every Item_Stat_* carrier


def _items():
    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    n = int.from_bytes(header[:2], "little")
    starts = sorted(
        int.from_bytes(header[2 + i * 8 + 4:2 + i * 8 + 8], "little")
        for i in range(n))
    return parse_iteminfo_from_bytes(
        body, starts, fields=detect_iteminfo_layout(body, starts))


def _stat_ids(it) -> set[int]:
    out: set[int] = set()
    for s in (it.get("sharpness_data") or {}).get("stat_list") or []:
        out.add(s["stat"])
    for t in it.get("enchant_data_list") or []:
        esd = t.get("enchant_stat_data") or {}
        for ln in ("max_stat_list", "regen_stat_list", "stat_list_static",
                   "stat_list_static_level"):
            for s in esd.get(ln) or []:
                out.add(s["stat"])
    return out


def test_item_names_agree_with_the_stat_table():
    """The game names some items after the stat they grant. Those two
    tables are independent, so they must agree on the NAME -- not merely
    on the number.

    This is what caught the community mapping being wrong:
    Item_Stat_AbyssGear_CriticalRate_LV1 carries stat 1000007, and the
    game's statusinfo calls 1000007 CriticalRate. The community list
    calls it "Critical Damage".
    """
    checked = 0
    for it in _items():
        name = it.get("string_key") or ""
        if not (name.startswith("Item_Stat_") and name.endswith("_LV1")):
            continue
        residual = _stat_ids(it) - BASELINE
        if len(residual) != 1:
            continue          # carrier expresses itself via buffs, not stats
        stat_id = next(iter(residual))
        claimed = name.split("_")[3]        # Item_Stat_<Set>_<Thing>_LV1
        actual = STAT_NAMES_CD113.get(stat_id)
        assert actual is not None, f"{stat_id} missing from the stat table"
        assert claimed.lower() == actual.lower(), (
            f"{name} grants stat {stat_id}, which the stat table calls "
            f"{actual!r} -- the item name says {claimed!r}. One of the two "
            f"is wrong and it is probably the table.")
        checked += 1
    assert checked >= 3, (
        f"only {checked} self-labelling items cross-checked; this test is "
        f"the main guard on the stat table and it needs to actually run")


def test_guard_stat_lands_on_shields():
    """Semantic corroboration, independent of any naming convention.

    1000043 is GuardPVRate. Guarding is what shields do, so if the table
    is right that id should concentrate on shields rather than scatter.
    Measured on vanilla 1.13, and it holds in BOTH directions:

      * 76 of the 83 items carrying it (92%) are named *Shield*
      * 76 of the 79 equippable shields (96%) carry it

    The handful of non-shield carriers are crowns / masks / Animal_Spirit
    -- plausible guard-granting accessories, not noise.

    The community mapping calls 1000019 "Guard PV Rate". The game calls
    1000019 EquipMainWeapon, and puts GuardPVRate at 1000043. This test
    is why I believe the game.
    """
    items = _items()
    carriers = [it.get("string_key") or "" for it in items
                if 1000043 in _stat_ids(it)]
    assert len(carriers) > 50, f"only {len(carriers)} carriers of 1000043"

    shields = [c for c in carriers if "Shield" in c]
    assert len(shields) >= len(carriers) * 0.85, (
        f"only {len(shields)} of {len(carriers)} carriers of 1000043 "
        f"(GuardPVRate) are shields -- the id may be mislabelled")

    equippable_shields = [
        it.get("string_key") or "" for it in items
        if "Shield" in (it.get("string_key") or "") and it.get("equip_type_info")]
    covered = [s for s in equippable_shields if s in set(carriers)]
    assert len(covered) >= len(equippable_shields) * 0.9, (
        f"only {len(covered)} of {len(equippable_shields)} shields carry "
        f"GuardPVRate")


def test_every_stat_used_by_vanilla_gear_has_a_name():
    """No gear stat should render to a modder as a bare number."""
    used: set[int] = set()
    for it in _items():
        used |= _stat_ids(it)
    unnamed = sorted(s for s in used if s not in STAT_NAMES_CD113)
    assert not unnamed, f"stat ids used by vanilla gear with no name: {unnamed}"


def test_snapshot_covers_the_contiguous_block():
    assert len(STAT_NAMES_CD113) == 75
    assert set(STAT_NAMES_CD113) == set(range(1000000, 1000075))


def test_parse_stat_names_reads_the_record_envelope():
    body = (b"\x07\x00\x0f\x00"                    # key 1000007 (LE)
            b"\x0c\x00\x00\x00" + b"CriticalRate")
    key = int.from_bytes(body[:4], "little")
    header = (b"\x01\x00" + key.to_bytes(4, "little")
              + (0).to_bytes(4, "little"))
    assert parse_stat_names(body, header) == {key: "CriticalRate"}


def test_load_falls_back_to_the_snapshot_without_a_game():
    assert load_stat_names() == STAT_NAMES_CD113
    assert load_stat_names(None, None) == STAT_NAMES_CD113


def test_unknown_ids_render_as_the_bare_number():
    assert stat_label(1000007) == "1000007 (CriticalRate)"
    assert stat_label(999999) == "999999"      # never invent a name
