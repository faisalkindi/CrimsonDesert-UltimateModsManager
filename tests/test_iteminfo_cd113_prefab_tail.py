"""CD 1.13 relocated prefab_data_list to the end of the item record (#285).

The bug this pins is not "a field was missing". It's that **the field was
missing and every check we had still passed**:

    * the whole table round-tripped byte-exact -- because the parser carried
      the last 76-139 bytes of EVERY record opaquely as ``_tail_slack``;
    * so did every per-record test;
    * and the validator cheerfully accepted `prefab_data_list` intents,
      reported them "ready", and then wrote nothing.

A byte-exact round-trip proves the bytes are PRESERVED, not that they are
UNDERSTOOD. That is why the assertion below is on ``_tail_slack`` being
EMPTY, not merely on the round-trip -- the round-trip alone is exactly the
check that lied to us for months.

The wire shape itself was cracked with the mod as a Rosetta stone: AerowynX's
"Equip All V6" ships DECODED values for the very struct we couldn't parse. It
says ``equip_slot_list: [7]`` where the bytes read ``01000000 0700`` -- so the
list is u16, not u32. An EMPTY u16 array and an EMPTY u32 array are the same
four zero bytes, which is why a u32 reading fit 6272 records and desynced on
the 236 where the list is non-empty.
"""
from __future__ import annotations

import pytest

from tests.fixture_loaders import load_vanilla113, load_vanilla110

from cdumm.engine import iteminfo_native_parser as P
from cdumm.engine.iteminfo_writer import (
    normalize_prefab_data_list, SUPPORTED_FIELDS)
from cdumm.engine.format3_handler import (
    Format3Intent, LIST_WRITERS, validate_intents)
from cdumm.semantic.parser import parse_pabgh_index


def _table(loader):
    body = loader("iteminfo.pabgb")
    header = loader("iteminfo.pabgh")
    _k, offs = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offs.values())
    return body, offs, starts, P.detect_iteminfo_layout(body, starts)


@pytest.fixture(scope="module")
def v113():
    return _table(load_vanilla113)


def _records(body, starts, fields):
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(body)
        r = P._Reader(body, s, rec_end=e)
        yield s, e, r, P._read_item(r, fields=fields)


# ── the bar ─────────────────────────────────────────────────────────────

def test_no_record_has_uninterpreted_tail_bytes(v113):
    """THE test. Round-tripping is not enough; nothing may be left over."""
    body, _offs, starts, fields = v113
    slack = [s for s, e, r, _it in _records(body, starts, fields)
             if r.pos != e]
    assert not slack, (
        f"{len(slack)} record(s) still carry uninterpreted tail bytes. The "
        f"table will still round-trip byte-exact -- that is precisely the "
        f"trap. Decode the tail, don't preserve it opaquely.")


def test_whole_table_still_round_trips_byte_exact(v113):
    body, _offs, starts, fields = v113
    for s, e, _r, it in _records(body, starts, fields):
        w = P._Writer()
        P._write_item(w, it, fields=fields)
        assert bytes(w.buf) == bytes(body[s:e])


def test_prefab_data_list_is_actually_exposed(v113):
    body, _offs, starts, fields = v113
    n = sum(1 for _s, _e, _r, it in _records(body, starts, fields)
            if it.get("prefab_data_list"))
    assert n > 5000, f"only {n} records expose prefab_data_list"


def test_equip_slot_list_is_u16_not_u32(v113):
    """The one fact that hid the whole field.

    Ground truth: the mod ships `equip_slot_list: [7]` / `[8]` for the two
    elements of Folorin_Earring (key 8509), and the bytes there read
    `01000000 0700` / `01000000 0800`. A u32 reading would produce
    0x00050007 -- and would still "work" on the 6272 records where the list
    is empty, because an empty u16 array and an empty u32 array are the same
    four zero bytes.
    """
    body, offs, starts, fields = v113
    s = offs[8509]
    i = starts.index(s)
    e = starts[i + 1] if i + 1 < len(starts) else len(body)
    r = P._Reader(body, s, rec_end=e)
    it = P._read_item(r, fields=fields)
    slots = [el["equip_slot_list"] for el in it["prefab_data_list"]]
    assert slots[:2] == [[7], [8]], slots


# ── the writer ──────────────────────────────────────────────────────────

def test_a_mod_element_keeps_the_fields_it_does_not_set(v113):
    """1.13 merged PrefabData with GimmickVisualPrefabData, so the on-disk
    element carries `scale` / `animation_path_list` / two flag bytes that
    mods never ship. They are NOT constant (44 elements have a non-(1,1,1)
    scale, 68 carry animation entries), so defaulting them would quietly
    rewrite real data. They must be carried over per index."""
    existing = [{
        "scale": [1.8, 1.8, 1.8],
        "prefab_names": [111],
        "animation_path_list": [999],
        "equip_slot_list": [3],
        "tribe_gender_list": [42],
        "is_craft_material": 0,
        "unk_flag_b": 0,
        "unk_flag_c": 3,
    }]
    # what a mod actually ships: no scale, no animation, no flags
    new = [{"prefab_names": [111], "equip_slot_list": [3],
            "tribe_gender_list": [], "is_craft_material": 0}]

    out = normalize_prefab_data_list(existing, new)
    assert out[0]["tribe_gender_list"] == []        # the mod's edit lands
    assert out[0]["scale"] == [1.8, 1.8, 1.8]       # ...and this survives
    assert out[0]["animation_path_list"] == [999]
    assert out[0]["unk_flag_c"] == 3


def test_added_elements_get_defaults(v113):
    out = normalize_prefab_data_list(
        [], [{"prefab_names": [7], "equip_slot_list": [],
              "tribe_gender_list": []}])
    assert out[0]["scale"] == [1.0, 1.0, 1.0]
    assert out[0]["animation_path_list"] == []


def test_edited_record_reparses_with_zero_slack(v113):
    """Writing must not reintroduce the very thing this fixes."""
    body, offs, starts, fields = v113
    s = offs[8509]
    i = starts.index(s)
    e = starts[i + 1] if i + 1 < len(starts) else len(body)
    r = P._Reader(body, s, rec_end=e)
    it = P._read_item(r, fields=fields)

    it["prefab_data_list"] = normalize_prefab_data_list(
        it["prefab_data_list"],
        [{"prefab_names": el["prefab_names"],
          "equip_slot_list": el["equip_slot_list"],
          "tribe_gender_list": [],              # the Equip-Everything edit
          "is_craft_material": 0}
         for el in it["prefab_data_list"]])

    w = P._Writer()
    P._write_item(w, it, fields=fields)
    out = bytes(w.buf)

    r2 = P._Reader(out, 0, rec_end=len(out))
    it2 = P._read_item(r2, fields=fields)
    assert r2.pos == len(out), "edited record left tail slack"
    assert all(not el["tribe_gender_list"]
               for el in it2["prefab_data_list"])

    w2 = P._Writer()
    P._write_item(w2, it2, fields=fields)
    assert bytes(w2.buf) == out, "edited record is not byte-stable"


# ── the validation gate (Equip Everything / AXIOM QoL) ──────────────────

def test_prefab_data_list_is_registered_for_validation():
    """The writer has decoded and written prefab_data_list since #285, but
    the field was never added to LIST_WRITERS, so validate_intents refused
    every whole-list write with "this table doesn't have a list writer yet"
    and Equip Everything (Nexus 2571, 13k+ dls) / AXIOM QoL (2508, 22k)
    applied 0 of their ~2,548 intents. Pin BOTH sides so an accept always
    implies a real write: registered for validation AND writable. (The same
    accept/write drift hazard the #150 characterinfo comment warned about.)"""
    assert ("iteminfo", "prefab_data_list") in LIST_WRITERS
    assert "prefab_data_list" in SUPPORTED_FIELDS


def test_validator_accepts_a_real_prefab_data_list_intent(v113):
    """Build a genuine Equip-Everything-style intent from the 1.13 fixture
    (set prefab_data_list with tribe_gender_list cleared) and prove
    validate_intents now classifies it supported, not skipped. Before the
    LIST_WRITERS registration this returned 0 supported / 1 skipped with the
    "variable-length list-of-dicts ... doesn't have a list writer yet"
    reason -- reported "ready" to the user, then wrote nothing."""
    body, offs, starts, fields = v113
    key = 8509
    s = offs[key]
    i = starts.index(s)
    e = starts[i + 1] if i + 1 < len(starts) else len(body)
    it = P._read_item(P._Reader(body, s, rec_end=e), fields=fields)

    new = [{"prefab_names": el["prefab_names"],
            "equip_slot_list": el["equip_slot_list"],
            "tribe_gender_list": [],               # the mod's edit
            "is_craft_material": 0}
           for el in it["prefab_data_list"]]
    intent = Format3Intent(entry=it.get("string_key") or "", key=key,
                           field="prefab_data_list", op="set", new=new)

    result = validate_intents("iteminfo.pabgb", [intent])
    assert len(result.supported) == 1 and len(result.skipped) == 0, (
        "prefab_data_list set must validate as supported now that the "
        "native writer is registered; got "
        f"{len(result.supported)} supported / {len(result.skipped)} skipped"
        + (f" -- {result.skipped[0][1]}" if result.skipped else ""))


# ── no collateral damage ────────────────────────────────────────────────

def test_cd110_is_untouched():
    body, _offs, starts, fields = _table(load_vanilla110)
    lbl = next((name for name, f in P._ITEM_LAYOUTS if f is fields),
               "default")
    assert lbl == "default", (
        f"CD 1.10 now selects {lbl!r}; the 1.13 tail must not be applied to "
        f"builds that don't have it")
