"""dropsetinfo's two drop amounts, under the game's names (#491).

The 2.04 exe reads a drop entry's two u64 amounts as _minValue, then
_maxValue. CDUMM called them max_amt and min_amt, the wrong way round, so
ItemDrop now calls them min_value and max_value.

Format 3 JSON keeps the old keys with their old bytes: "max_amt" still
writes the first u64 and "min_amt" the second, because exports rely on
that (tests/fixtures/format3/dropsetinfo_5x_drops.json is one). The JSON
also takes min_value/max_value, and a drop that mixes the two pairs is
refused.

Oracles: the 1.13 fixture's own bytes, and the writer's output on master
579e9e9, measured before the rename. Amounts are read back by byte
position, not through ItemDrop's names, so a rename can't make a test
pass on its own.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import struct
from functools import lru_cache
from pathlib import Path

import pytest

from cdumm.engine.dropset_writer import (
    _drop_dict_to_item_drop,
    build_drop_append_change,
    build_drops_replacement_change,
    parse_dropset_record,
)
from cdumm.engine.format3_apply import _intents_to_v2_changes
from cdumm.engine.format3_handler import Format3Intent
from cdumm.semantic.parser import parse_pabgh_index
from tests.fixture_loaders import load_vanilla113

_FIVE_X = (Path(__file__).resolve().parent / "fixtures" / "format3"
           / "dropsetinfo_5x_drops.json")
_GRAYMANE = 175001
_GRAYMANE_NAME = "DropSet_Faction_Graymane"

# sha256 of every patched body build_drops_replacement_change returns for
# the 5x fixture's 695 intents, in file order. Measured on master 579e9e9.
_FIVE_X_PATCHED_SHA256 = (
    "7070769fa45daaf67e350284edce1b07e2d9709939563cf3be730b88f17a2ed5")

# Drop entry: 68 bytes, then a trailer chosen by unk4 (the u32 at +9).
# The two amounts are the u64s at +46 and +54.
_TRAILER = {13: 1, 10: 4, 7: 28}


@lru_cache(maxsize=1)
def _records() -> dict[int, bytes]:
    body = load_vanilla113("dropsetinfo.pabgb")
    _ks, offsets = parse_pabgh_index(
        load_vanilla113("dropsetinfo.pabgh"), "dropsetinfo")
    ents = sorted(offsets.items(), key=lambda kv: kv[1])
    return {
        key: body[off:(ents[i + 1][1] if i + 1 < len(ents) else len(body))]
        for i, (key, off) in enumerate(ents)
    }


def _header_len(rec: bytes) -> int:
    return 8 + struct.unpack_from("<I", rec, 4)[0]


def _entry_amounts(rec: bytes) -> list[tuple[int, int]]:
    """(first u64, second u64) of each drop entry in a full record."""
    pos = _header_len(rec) + 1 + 1 + 4
    pos += 4 + struct.unpack_from("<I", rec, pos)[0] + 4
    count = struct.unpack_from("<I", rec, pos)[0]
    pos += 4
    out = []
    for _ in range(count):
        unk4 = struct.unpack_from("<I", rec, pos + 9)[0]
        out.append(struct.unpack_from("<QQ", rec, pos + 46))
        pos += 68 + _TRAILER.get(unk4, 0)
    return out


def _after_set(drops: list[dict]) -> bytes:
    rec = _records()[_GRAYMANE]
    change = build_drops_replacement_change(
        rec, _GRAYMANE, _GRAYMANE_NAME, drops)
    assert change is not None
    return rec[:_header_len(rec)] + bytes.fromhex(change["patched"])


def _after_append(drop: dict) -> bytes:
    rec = _records()[_GRAYMANE]
    change = build_drop_append_change(rec, _GRAYMANE, _GRAYMANE_NAME, drop)
    assert change is not None
    return rec[:_header_len(rec)] + bytes.fromhex(change["patched"])


def test_the_first_amount_is_never_the_larger_on_113():
    """min_value is the first u64 and max_value the second, and on 1.13
    the first is never the larger: 16,793 equal, 1,127 smaller, 0 larger."""
    seen = {"equal": 0, "smaller": 0, "larger": 0}
    for rec in _records().values():
        drops = parse_dropset_record(rec).drops
        assert [(x.min_value, x.max_value) for x in drops] == _entry_amounts(rec)
        for x in drops:
            seen["equal" if x.min_value == x.max_value else
                 "smaller" if x.min_value < x.max_value else "larger"] += 1
    assert seen == {"equal": 16793, "smaller": 1127, "larger": 0}


def test_legacy_keys_write_what_they_wrote_before_on_the_5x_fixture():
    """The 5x fixture (author "CrimsonGameMods DropSets") writes the same
    bytes as on master: max_amt into the first u64, min_amt into the
    second, on all 3,789 drops."""
    mod = json.loads(_FIVE_X.read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    drops = 0
    for intent in mod["intents"]:
        rec = _records()[intent["key"]]
        change = build_drops_replacement_change(
            rec, intent["key"], intent["entry"], intent["new"])
        assert change is not None, intent["entry"]
        patched = bytes.fromhex(change["patched"])
        digest.update(patched)
        written = _entry_amounts(rec[:_header_len(rec)] + patched)
        assert written == [(d["max_amt"], d["min_amt"]) for d in intent["new"]]
        drops += len(written)
    assert (len(mod["intents"]), drops) == (695, 3789)
    assert digest.hexdigest() == _FIVE_X_PATCHED_SHA256


@pytest.mark.parametrize("fields, first_second, record_sha256", [
    # The rows in #491's table, then a drop with no amount keys. Each
    # record's sha256 is master 579e9e9's output for the same drop.
    ({"min_amt": 2, "max_amt": 5}, (5, 2),
     "a828d73baec8bcb52e9c6665c9df0b060c2ea26a83ccca2bcbb67fc7b98d784d"),
    ({"max_amt": 5}, (5, 0),
     "16a017ed427cb2d7c6b096147964199e8412af8d01559203d403513c6cd3ed18"),
    ({"rates_100": 100, "min_amt": 15, "max_amt": 10}, (10, 15),
     "a30979d873d97129b5fb9a314479840992fed07504c31a910a5e97806851c3b0"),
    ({}, (0, 0),
     "742e795f25e59acf56a396f434dc39cfd4d2fc482162e9e15c38d8c3d070b58e"),
], ids=["min2-max5", "max5-alone", "5x-fixture-drop", "no-amounts"])
def test_legacy_keys_write_what_they_wrote_before_on_graymane(
        fields, first_second, record_sha256):
    drop = {"item_key": 30010, "rates": 1000000, **fields}
    full = _after_set([drop])
    assert _entry_amounts(full) == [first_second]
    assert hashlib.sha256(full).hexdigest() == record_sha256
    assert _entry_amounts(_after_append(drop))[-1] == first_second


def test_legacy_keys_append_writes_what_it_wrote_before():
    full = _after_append(
        {"item_key": 30010, "rates": 1000000, "min_amt": 2, "max_amt": 5})
    assert hashlib.sha256(full).hexdigest() == (
        "21c1df24e59db12b1912cd71d35eb7b7dbe548bb2eb11f66fe1dc5e844a7efed")


@pytest.mark.parametrize("fields, first_second", [
    ({"min_value": 2, "max_value": 5}, (2, 5)),
    ({"min_value": 3, "max_value": 3}, (3, 3)),
    # A missing amount is 0 under either pair; #491 leaves that open.
    ({"max_value": 5}, (0, 5)),
], ids=["min2-max5", "min3-max3", "max5-alone"])
def test_min_value_and_max_value_take_the_game_order(fields, first_second):
    """min_value writes the first u64 and max_value the second, and the
    record matches the legacy pair spelled the crossed way."""
    drop = {"item_key": 30010, "rates": 1000000, **fields}
    legacy = {"item_key": 30010, "rates": 1000000}
    if "min_value" in fields:
        legacy["max_amt"] = fields["min_value"]
    if "max_value" in fields:
        legacy["min_amt"] = fields["max_value"]
    full = _after_set([drop])
    assert _entry_amounts(full) == [first_second]
    assert full == _after_set([legacy])
    assert _entry_amounts(_after_append(drop))[-1] == first_second
    assert _after_append(drop) == _after_append(legacy)


def _spelled_out(x, amounts: tuple[int, int], keys: tuple[str, str]) -> dict:
    """Vanilla drop ``x`` as JSON, its first u64 under keys[0] and its
    second under keys[1]."""
    d = {"flag": x.flag, "item_key": x.item_key, "unk3": x.unk3,
         "unk4": x.unk4, "unk_cond_flag": x.unk_cond_flag,
         "unk_post_cond": x.unk_post_cond, "rates": x.rates,
         "rates_100": x.rates_100, "unk2": x.unk2,
         keys[0]: amounts[0], keys[1]: amounts[1],
         "unk3_flags": x.unk3_flags, "item_key_dup": x.item_key_dup}
    if x.extra_u8 is not None:
        d["extra_u8"] = x.extra_u8
    if x.extra_u32 is not None:
        d["extra_u32"] = x.extra_u32
    if x.friendly_data is not None:
        d["friendly_data"] = x.friendly_data.hex()
    return d


@pytest.mark.parametrize("keys, rebuilt", [
    (("max_amt", "min_amt"), 14542),
    (("min_value", "max_value"), 14542),
    # The legacy names taken at their word: 323 records come out wrong,
    # so the check can tell the two orders apart.
    (("min_amt", "max_amt"), 14219),
], ids=["legacy", "min_value-max_value", "legacy-taken-literally"])
def test_spelling_out_vanilla_rebuilds_it_byte_for_byte(keys, rebuilt):
    """Every vanilla drop written back as JSON. A set rebuilds the
    record exactly when the keys put each u64 back where it came from.

    unk1_flag isn't a JSON key (each drop takes the record's first), so
    the 29 records whose drops differ in it are left out, as are the 4
    with no drops. That leaves 14,542."""
    same = differ = 0
    for key, rec in _records().items():
        ds = parse_dropset_record(rec)
        if not ds.drops or len({x.unk1_flag for x in ds.drops}) != 1:
            continue
        drops = [_spelled_out(x, a, keys)
                 for x, a in zip(ds.drops, _entry_amounts(rec))]
        change = build_drops_replacement_change(rec, key, ds.name, drops)
        assert change is not None, ds.name
        if change["patched"] == change["original"]:
            same += 1
        else:
            differ += 1
    assert (same, same + differ) == (rebuilt, 14542)


_MIXED = [
    {"max_amt": 5, "max_value": 7},
    {"max_amt": 5, "min_value": 7},
    {"min_amt": 5, "max_value": 7},
    {"min_amt": 5, "min_value": 7},
    # Both pairs complete and agreeing: still refused, not guessed at.
    {"max_amt": 2, "min_amt": 5, "min_value": 2, "max_value": 5},
]


@pytest.mark.parametrize("fields", _MIXED, ids=lambda f: "+".join(f))
def test_a_drop_that_mixes_the_two_pairs_is_refused(fields, caplog):
    drop = {"item_key": 30010, "rates": 1000000, **fields}
    with pytest.raises(ValueError, match="mixes the older amount keys") as exc:
        _drop_dict_to_item_drop(drop)
    for key in fields:
        assert key in str(exc.value)

    rec = _records()[_GRAYMANE]
    fine = {"item_key": 103, "rates": 1000000, "max_amt": 1, "min_amt": 2}
    with caplog.at_level(logging.WARNING, logger="cdumm.engine.dropset_writer"):
        assert build_drops_replacement_change(
            rec, _GRAYMANE, _GRAYMANE_NAME, [fine, drop]) is None
        assert build_drop_append_change(
            rec, _GRAYMANE, _GRAYMANE_NAME, drop) is None
    refusals = [r for r in caplog.records
                if "mixes the older amount keys" in r.getMessage()]
    assert len(refusals) == 2


def test_a_mixed_drop_refuses_only_its_own_intent():
    """Through the Format 3 expander, the intent with the mixed drop
    emits no change and the one next to it still does."""
    mod = json.loads(_FIVE_X.read_text(encoding="utf-8"))
    first, second = copy.deepcopy(mod["intents"][:2])
    second["new"][0]["max_value"] = 1
    intents = [Format3Intent(entry=i["entry"], key=i["key"], field="drops",
                             op="set", new=i["new"]) for i in (first, second)]
    changes = _intents_to_v2_changes(
        "dropsetinfo.pabgb", load_vanilla113("dropsetinfo.pabgb"),
        load_vanilla113("dropsetinfo.pabgh"), intents)
    assert [c["entry"] for c in changes] == [first["entry"]]
