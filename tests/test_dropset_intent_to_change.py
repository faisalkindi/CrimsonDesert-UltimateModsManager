"""Translate a Format 3 `field=drops, op=set, new=[...]` intent into
a v2-style change dict that replaces the entire dropsetinfo record's
bytes. Round-trip the result and verify the new drops list matches
the JSON.

Bug from kori228 / nizamintestino / UnLuckyLust (GitHub #41 + #55):
NattKh's tool exports Format 3 mods that set the `drops` list on
dropsetinfo.pabgb. CDUMM's primitive-only writer skips them with a
"coming in v3.3" message. This module adds list-writer dispatch.
"""
from __future__ import annotations
import struct
from pathlib import Path

import pytest


_VANILLA_PABGB = Path(r"C:\Users\faisa\AppData\Local\Temp\dropsetinfo.pabgb")
_VANILLA_PABGH = Path(r"C:\Users\faisa\AppData\Local\Temp\dropsetinfo.pabgh")


def _have_vanilla() -> bool:
    return _VANILLA_PABGB.exists() and _VANILLA_PABGH.exists()


def _record_bytes_for(key: int) -> tuple[bytes, int, int]:
    pabgb = _VANILLA_PABGB.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    count = struct.unpack_from("<H", pabgh, 0)[0]
    records = []
    for i in range(count):
        off = 2 + i * 8
        k, body_off = struct.unpack_from("<II", pabgh, off)
        records.append((k, body_off))
    sorted_recs = sorted(records, key=lambda r: r[1])
    boundaries = {}
    for i, (k, o) in enumerate(sorted_recs):
        end = sorted_recs[i + 1][1] if i + 1 < len(sorted_recs) else len(pabgb)
        boundaries[k] = (o, end)
    body_off, body_end = boundaries[key]
    return pabgb[body_off:body_end], body_off, body_end


@pytest.mark.skipif(not _have_vanilla(), reason="vanilla extract not present")
def test_drops_intent_produces_record_replacement():
    """Build a `drops` intent, run the converter, splice the patched
    body bytes back onto the original header, parse, verify the new
    drops list matches the JSON.

    `change["original"]` and `change["patched"]` are body-only (the
    record bytes AFTER the `key + name_len + name` header). The
    name-offsets resolver in `_apply_byte_patches` anchors there.
    """
    from cdumm.engine.dropset_writer import (
        build_drops_replacement_change,
        parse_dropset_record,
    )

    record_bytes, _, _ = _record_bytes_for(175001)  # DropSet_Faction_Graymane
    new_drops_json = [
        {"item_key": 30010, "rates": 1000000, "rates_100": 100,
         "min_amt": 3, "max_amt": 2},
        {"item_key": 103, "rates": 1000000, "rates_100": 100,
         "min_amt": 2, "max_amt": 1},
    ]

    change = build_drops_replacement_change(
        record_bytes, intent_key=175001, intent_entry="DropSet_Faction_Graymane",
        new_drops_json=new_drops_json)
    assert change is not None, "Expected a change dict, got None"
    assert change["entry"] == "DropSet_Faction_Graymane"
    assert change["rel_offset"] == 0
    assert "original" in change and "patched" in change

    # `original` must match the body slice of the vanilla record.
    parsed_old = parse_dropset_record(record_bytes)
    name_bytes = parsed_old.name.encode("latin-1")
    header_len = 4 + 4 + len(name_bytes)
    assert bytes.fromhex(change["original"]) == record_bytes[header_len:]

    # Re-attach the original header so we can re-parse cleanly.
    new_full = record_bytes[:header_len] + bytes.fromhex(change["patched"])
    parsed_new = parse_dropset_record(new_full)
    assert parsed_new.key == 175001
    assert parsed_new.name == "DropSet_Faction_Graymane"
    assert len(parsed_new.drops) == 2, (
        f"Expected 2 drops in new record, got {len(parsed_new.drops)}")
    assert parsed_new.drops[0].item_key == 30010
    assert parsed_new.drops[0].rates == 1000000
    assert parsed_new.drops[0].rates_100 == 100
    assert parsed_new.drops[0].min_amt == 3
    assert parsed_new.drops[0].max_amt == 2
    assert parsed_new.drops[1].item_key == 103


@pytest.mark.skipif(not _have_vanilla(), reason="vanilla extract not present")
def test_drops_intent_preserves_record_metadata():
    """The drops replacement must NOT touch other fields of the record
    (name, drop_roll_count, total_drop_rate, etc.). Verified by
    re-attaching the header to the patched body and re-parsing."""
    from cdumm.engine.dropset_writer import (
        build_drops_replacement_change,
        parse_dropset_record,
    )

    record_bytes, _, _ = _record_bytes_for(175001)
    parsed_old = parse_dropset_record(record_bytes)
    name_bytes = parsed_old.name.encode("latin-1")
    header_len = 4 + 4 + len(name_bytes)

    change = build_drops_replacement_change(
        record_bytes, intent_key=175001,
        intent_entry="DropSet_Faction_Graymane",
        new_drops_json=[{"item_key": 99, "rates": 1, "rates_100": 1,
                         "min_amt": 1, "max_amt": 1}])
    new_full = record_bytes[:header_len] + bytes.fromhex(change["patched"])
    parsed_new = parse_dropset_record(new_full)

    # Non-drops fields must be unchanged
    assert parsed_new.key == parsed_old.key
    assert parsed_new.name == parsed_old.name
    assert parsed_new.is_blocked == parsed_old.is_blocked
    assert parsed_new.drop_roll_type == parsed_old.drop_roll_type
    assert parsed_new.drop_roll_count == parsed_old.drop_roll_count
    assert parsed_new.drop_condition_string == parsed_old.drop_condition_string
    assert parsed_new.drop_tag_name_hash == parsed_old.drop_tag_name_hash
    assert parsed_new.nee_slot_count == parsed_old.nee_slot_count
    assert parsed_new.need_weight == parsed_old.need_weight
    assert parsed_new.total_drop_rate == parsed_old.total_drop_rate
    assert parsed_new.original_string == parsed_old.original_string
