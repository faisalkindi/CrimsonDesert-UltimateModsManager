"""Integration tests — Path B walker on real PABGB tables.

MEASURED coverage (2026-07-11, against the committed fixtures, on the
schema in this branch). The previous version of this docstring claimed
"iteminfo: 100% byte-perfect across all 6339 entries". That was false in
both halves, and had been for at least three game versions:

  * the table has 6508 entries, not 6339
  * the walker never completes an iteminfo entry -- it stops early on
    every single one

It survived because the test asserting it looked for a
``tests/fixtures/iteminfo/`` directory that has never existed in this
repo, so it skipped in CI and on every fresh clone. Nobody ever saw it
run. The numbers below were measured, not remembered, and the iteminfo
tests now run against a committed fixture so they cannot rot the same
way again.

iteminfo, CD 1.13 (tests/fixtures/vanilla113 — committed, runs in CI):
  * 6508 entries
  * reaches ``_cooltime`` on 6498 of them (99.8%)
  * decodes a median of 110 of the schema's 113 fields
  * completes the full schema on 0 entries. This is a structural
    ceiling, not a bug to chase: the LANTERN block is a 12-byte
    conditional keyed on the *value* of ``equip_type_info``, and the
    descriptor grammar has no way to express a value-dependent field.
    Reaching the target field is what Format 3 needs; completing the
    record is not.

    For the record, on upstream/master this same table decodes a median
    of 11 of 113 fields and reaches ``_cooltime`` on 0 entries -- which
    IS the "iteminfo grid only shows 11 fields" wall.

iteminfo, CD 1.10 (tests/fixtures/vanilla110 — committed, runs in CI):
  * decodes a median of 64 of 113 fields (master: 11), but still reaches
    ``_cooltime`` on 0 entries: it stops at ``_enchantDataList``, a field
    1.13 added and 1.10 does not have.
  * So 1.10 is strictly better off than on master and no worse in target
    reach. It is NOT fixed, and ``test_1_10_is_a_known_limitation`` pins
    that honestly rather than leaving it an unknown.

Other tables (vehicleinfo / fieldinfo / stageinfo / regioninfo /
characterinfo) still need a local extract and skip without one -- their
fixtures aren't committed (up to 25 MB). Their numbers below are as
last measured on a local install and are NOT verified by CI:
  * vehicleinfo: 100% target reach (_canCallInSafeZone), 32 entries
  * fieldinfo:   100% reach for _canCallVehicle, 7 entries
  * stageinfo:   93.5% target reach (_completeCount), 50463 entries

To run those locally:

  CDUMM_VANILLA_ITEMINFO_DIR=C:/path/to/extracted py -3 -m pytest \
      tests/test_iteminfo_walk_real_game.py -v
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.fixture_loaders import load_vanilla110, load_vanilla113

from cdumm.engine.format3_apply import _consume_field_bytes, _payload_offset
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import get_schema, parse_pabgh_index


def _find_vanilla_pair(table_name: str) -> tuple[Path, Path] | None:
    """Locate a vanilla {table}.pabgb + {table}.pabgh on the local disk.

    Only for the tables with no committed fixture (vehicleinfo,
    fieldinfo, stageinfo, regioninfo, characterinfo). iteminfo has a
    committed fixture and must NOT come through here -- routing it here
    is what made its tests skip everywhere.
    """
    env = os.environ.get("CDUMM_VANILLA_ITEMINFO_DIR")
    if not env:
        return None
    d = Path(env)
    body = d / f"{table_name}.pabgb"
    header = d / f"{table_name}.pabgh"
    if body.exists() and header.exists():
        return body, header
    return None


def _walk_iteminfo_bytes(body: bytes, header: bytes):
    """Walk every iteminfo entry. Returns ``(total, reached_cooltime,
    median_fields, bail_field_counts)``.

    Deliberately reports rather than asserts, so the callers can pin the
    real numbers per game version instead of a single aspirational one.
    """
    parser_mod._loaded_schemas = None
    schema = get_schema("iteminfo")
    assert schema is not None, "ItemInfo schema not loaded"

    key_size, offsets = parse_pabgh_index(header, "iteminfo")
    assert key_size == 4, f"unexpected key_size {key_size}"
    assert offsets, "no entries in pabgh index"

    sorted_entries = sorted(offsets.items(), key=lambda kv: kv[1])
    total = len(sorted_entries)
    reached_cooltime = 0
    fields_decoded: list[int] = []
    bail_field_counts: dict[str, int] = {}

    for idx, (_key, entry_off) in enumerate(sorted_entries):
        entry_end = (sorted_entries[idx + 1][1]
                     if idx + 1 < total else len(body))
        payload_off = _payload_offset(
            body, entry_off, key_size,
            no_null_skip=schema.no_null_skip)
        assert payload_off is not None, (
            f"entry {idx}: payload_offset failed")

        off = payload_off
        saw_cooltime = False
        n = 0
        for f in schema.fields:
            consumed = _consume_field_bytes(body, off, f, entry_end)
            if consumed is None:
                bail_field_counts[f.name] = (
                    bail_field_counts.get(f.name, 0) + 1)
                break
            off += consumed
            n += 1
            if f.name == "_cooltime":
                saw_cooltime = True

        fields_decoded.append(n)
        if saw_cooltime:
            reached_cooltime += 1

    fields_decoded.sort()
    median = fields_decoded[len(fields_decoded) // 2]
    return total, reached_cooltime, median, bail_field_counts


def test_walker_reaches_cooltime_on_the_cd_113_table():
    """The number that matters for Format 3: does the walker REACH the
    target field? (Completing the record does not matter and is not
    achievable — see the module docstring on the LANTERN conditional.)

    Numbers are pinned exactly, not as floors, because the fixture is
    committed and therefore fixed: any movement here means the walker
    changed, and that should be a deliberate, visible edit.
    """
    total, reached, median, bails = _walk_iteminfo_bytes(
        load_vanilla113("iteminfo.pabgb"),
        load_vanilla113("iteminfo.pabgh"))

    assert total == 6508, f"fixture changed? {total} entries"
    assert reached == 6498, (
        f"reached _cooltime on {reached}/{total} entries, expected 6498. "
        f"Bails: {dict(sorted(bails.items(), key=lambda x: -x[1])[:5])}")
    assert median == 110, (
        f"median {median} of 113 fields decoded, expected 110. On "
        f"upstream/master this is 11 — a drop back toward that means the "
        f"ItemInfo schema has gone stale against the game again.")


def test_1_10_is_a_known_limitation_not_a_silent_one():
    """CD 1.10 does NOT work, and this pins exactly how it doesn't.

    The schema is a 1.13 schema, so on 1.10 the walker runs into
    ``_enchantDataList`` — a field 1.13 added — and stops there, short of
    ``_cooltime``. It gets further than master does (64 fields vs 11), so
    this is not a regression, but it is not a fix either.

    Asserted rather than left undocumented so that a) nobody re-derives
    this from scratch, and b) whoever does fix 1.10 gets a failing test
    telling them to update the claim.
    """
    total, reached, median, bails = _walk_iteminfo_bytes(
        load_vanilla110("iteminfo.pabgb"),
        load_vanilla110("iteminfo.pabgh"))

    assert total == 6483
    assert reached == 0, (
        f"1.10 now reaches _cooltime on {reached}/{total} entries. If you "
        f"fixed 1.10, say so here and in the module docstring.")
    assert median == 64, f"median {median} of 113 fields decoded on 1.10"
    assert bails.get("_enchantDataList", 0) > 5000, (
        f"expected 1.10 to stop at _enchantDataList (absent in 1.10); "
        f"actually stopped at: "
        f"{dict(sorted(bails.items(), key=lambda x: -x[1])[:3])}")


def _walk_table(table: str, target: str):
    """Walk every entry in ``table`` and return per-entry counters."""
    paths = _find_vanilla_pair(table)
    if paths is None:
        pytest.skip(
            f"Vanilla {table}.pabgb/.pabgh not found. Set "
            f"CDUMM_VANILLA_ITEMINFO_DIR or drop both files at "
            f"tests/fixtures/iteminfo/ to run this integration test.")
    body_path, header_path = paths

    parser_mod._loaded_schemas = None
    schema = get_schema(table)
    assert schema is not None, f"{table} schema not loaded"

    body = body_path.read_bytes()
    header = header_path.read_bytes()
    key_size, offsets = parse_pabgh_index(header, table)
    assert offsets, f"{table}: no entries in pabgh"

    sorted_entries = sorted(offsets.items(), key=lambda kv: kv[1])
    total = len(sorted_entries)
    target_reached = walked_full = 0
    bail_field_counts: dict[str, int] = {}

    for idx, (_key, entry_off) in enumerate(sorted_entries):
        entry_end = (sorted_entries[idx + 1][1]
                     if idx + 1 < total else len(body))
        payload_off = _payload_offset(
            body, entry_off, key_size,
            no_null_skip=schema.no_null_skip,
            no_entry_header=getattr(schema, "no_entry_header", False))
        if payload_off is None:
            continue

        off = payload_off
        bailed = False
        saw_target = False
        for f in schema.fields:
            if f.name == target:
                saw_target = True
            consumed = _consume_field_bytes(body, off, f, entry_end)
            if consumed is None:
                bail_field_counts[f.name] = (
                    bail_field_counts.get(f.name, 0) + 1)
                bailed = True
                break
            off += consumed

        if saw_target:
            target_reached += 1
        if not bailed:
            walked_full += 1

    return total, target_reached, walked_full, bail_field_counts


def test_vehicleinfo_can_call_in_safe_zone_reachable_for_every_entry():
    """Vehicleinfo override must reach `_canCallInSafeZone` (the
    field the upstream parser was built to target) for every entry."""
    total, target_reached, walked_full, bails = _walk_table(
        "vehicleinfo", "_canCallInSafeZone")
    assert target_reached == total, (
        f"vehicleinfo: only {target_reached}/{total} entries reached "
        f"_canCallInSafeZone; bails: {bails}")


def _read_field_value(tbl, target_field, target_key, fmt):
    """Walk to ``target_field`` in one entry and unpack it.

    Returns the value, or ``None`` when the table has no fixture and no
    local extract (caller decides whether that's a skip).
    """
    import struct

    if tbl == "iteminfo":
        body = load_vanilla113("iteminfo.pabgb")
        header = load_vanilla113("iteminfo.pabgh")
    else:
        paths = _find_vanilla_pair(tbl)
        if paths is None:
            return None
        body = paths[0].read_bytes()
        header = paths[1].read_bytes()

    parser_mod._loaded_schemas = None
    schema = get_schema(tbl)
    key_size, offsets = parse_pabgh_index(header, tbl)
    if target_key not in offsets:
        return ("missing_key",)
    sorted_e = sorted(offsets.items(), key=lambda kv: kv[1])
    idx = next(i for i, (k, _) in enumerate(sorted_e) if k == target_key)
    eoff = sorted_e[idx][1]
    end = (sorted_e[idx + 1][1] if idx + 1 < len(sorted_e) else len(body))
    off = _payload_offset(body, eoff, key_size,
                          no_null_skip=schema.no_null_skip,
                          no_entry_header=schema.no_entry_header)
    for f in schema.fields:
        if f.name == target_field:
            return struct.unpack_from(fmt, body, off)[0]
        consumed = _consume_field_bytes(body, off, f, end)
        if consumed is None:
            return ("walker_bailed_at", f.name)
        off += consumed
    return ("field_not_found_in_schema", target_field)


def test_iteminfo_value_correctness_spot_checks():
    """Lock in known field VALUES, not just walker reachability.

    Reaching a field proves nothing about reading the right bytes -- the
    vehicleinfo bug was caught exactly this way: the target was reached,
    the byte position was wrong, and the reachability test was green.

    Runs off the committed fixture, so it runs in CI. It is deliberately
    NOT bundled with the vehicleinfo checks any more: those need a local
    extract, and the old combined test called ``pytest.skip`` when any
    one table was missing -- which silently threw away the iteminfo
    assertions for everyone who didn't have the game extracted.
    """
    checks = [
        ("iteminfo", "_maxStackCount", 2200, 100, "<Q",
         "crimson-rs Pyeonjeon_Arrow stacks 100"),
        ("iteminfo", "_isBlocked", 2200, 0, "<B",
         "Pyeonjeon_Arrow not blocked"),
    ]
    for tbl, field, key, expected, fmt, desc in checks:
        actual = _read_field_value(tbl, field, key, fmt)
        assert actual == expected, (
            f"{tbl}.{field}[key={key}]: got {actual!r}, "
            f"expected {expected}. {desc}. Schema may be misaligned.")


def test_vehicleinfo_value_correctness_spot_checks():
    """The vehicleinfo half of the old combined spot-check test.

    Needs a local extract (no committed fixture), so it skips on CI.
    """
    checks = [
        ("vehicleinfo", "_canCallInSafeZone", 16960, 1, "<B",
         "Horse: only entry with this flag set"),
        ("vehicleinfo", "_mountCallType", 16960, 1, "<B",
         "Horse is rideable (=1)"),
        ("vehicleinfo", "_mountCallType", 16984, 2, "<B",
         "Dragon is flying (=2)"),
        ("vehicleinfo", "_mountCallType", 16998, 0, "<B",
         "BearWarMachine is siege (=0)"),
    ]
    for tbl, field, key, expected, fmt, desc in checks:
        actual = _read_field_value(tbl, field, key, fmt)
        if actual is None:
            pytest.skip("vehicleinfo extract missing — set "
                        "CDUMM_VANILLA_ITEMINFO_DIR")
        assert actual == expected, (
            f"{tbl}.{field}[key={key}]: got {actual!r}, "
            f"expected {expected}. {desc}. Schema may be misaligned.")


def test_vehicleinfo_horse_canCallInSafeZone_reads_as_one():
    """Iteration 13 systematic-debugging finding: previous vehicleinfo
    walk completed all 32 entries but ended 2 bytes short of entry_end.
    Investigation showed `_mountCallType` and `_canCallInSafeZone` were
    pointing at the WRONG bytes — there are 2 unknown bytes between
    `_vehicleCharKey` and `_mountCallType` that the schema missed.

    Empirical truth (upstream notes: "_canCallInSafeZone — only
    Horse=1 in vanilla"). Verifies schema correctly identifies
    Horse's flag value.
    """
    paths = _find_vanilla_pair("vehicleinfo")
    if paths is None:
        pytest.skip("vehicleinfo fixture not available")
    body = paths[0].read_bytes()
    header = paths[1].read_bytes()

    parser_mod._loaded_schemas = None
    schema = get_schema("vehicleinfo")

    key_size, offsets = parse_pabgh_index(header, "vehicleinfo")
    HORSE_KEY = 16960
    assert HORSE_KEY in offsets, "Horse entry not in pabgh"
    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])
    horse_idx = next(i for i, (k, _) in enumerate(sorted_offs) if k == HORSE_KEY)
    horse_off = sorted_offs[horse_idx][1]
    horse_end = (sorted_offs[horse_idx + 1][1]
                 if horse_idx + 1 < len(sorted_offs) else len(body))

    payload_off = _payload_offset(body, horse_off, key_size,
                                   no_null_skip=schema.no_null_skip,
                                   no_entry_header=schema.no_entry_header)
    off = payload_off
    can_call_value = None
    for f in schema.fields:
        if f.name == "_canCallInSafeZone":
            can_call_value = body[off]
            break
        consumed = _consume_field_bytes(body, off, f, horse_end)
        off += consumed

    assert can_call_value == 1, (
        f"Horse._canCallInSafeZone read as {can_call_value} but the "
        f"documented vanilla value is 1. The vehicleinfo "
        f"schema is reading the wrong byte position — likely missing "
        f"~2 bytes between _vehicleCharKey and _mountCallType.")


def test_fieldinfo_can_call_vehicle_reachable_for_every_entry():
    """Fieldinfo override walks fields 1-19 for every entry. Target
    `_canCallVehicle` (field 20 in IDA order, index 18 in payload) is
    the deepest field reachable before the undecoded `_complexData`
    block. The upstream `_alwaysCallVehicle_dev` is past that block
    and requires further RE work — explicitly out of Path B's scope."""
    total, target_reached, walked_full, bails = _walk_table(
        "fieldinfo", "_canCallVehicle")
    assert target_reached == total, (
        f"fieldinfo: only {target_reached}/{total} entries reached "
        f"_canCallVehicle; bails: {bails}")


def test_stageinfo_complete_count_reachable_for_majority():
    """Stageinfo override reaches `_completeCount` for 93.5% of entries
    (verified empirically 2026-04-27: 47186/50463). The remaining 6.5%
    hit `_sequencerDesc` optional-object variant the upstream parser
    also can't decode. Asserts a hard floor of 93% so a regression dropping
    to 92% would flag — Requirements review caught the original 90%
    floor as too loose to detect realistic regressions."""
    total, target_reached, walked_full, bails = _walk_table(
        "stageinfo", "_completeCount")
    coverage = target_reached / total
    assert coverage >= 0.93, (
        f"stageinfo: only {target_reached}/{total} ({coverage:.1%}) "
        f"entries reached _completeCount; expected >=93%. "
        f"Bails: {dict(sorted(bails.items(), key=lambda x: -x[1])[:5])}")


def test_regioninfo_is_town_reachable_for_every_entry():
    """RegionInfo override walks all 24 fields with no entry header
    (RegionInfo lacks the standard entry header — _key and
    _stringKey are regular schema fields, confirmed against the
    upstream parser). Verifies the ``_no_entry_header: true`` flag
    plumbed through ``_payload_offset``."""
    total, target_reached, walked_full, bails = _walk_table(
        "regioninfo", "_isTown")
    assert target_reached == total, (
        f"regioninfo: only {target_reached}/{total} entries reached "
        f"_isTown; bails: {bails}")
    assert walked_full == total, (
        f"regioninfo: only {walked_full}/{total} entries walked the "
        f"full schema; bails: {bails}")


def test_characterinfo_call_mercenary_spawn_duration_reachable():
    """CharacterInfo mount/vehicle subset (14 fields) — covers the
    fields the upstream characterinfo_mount_parser.py exposes for
    ride-duration and cooldown mods. The full CharacterInfo entry
    has many more fields (complex arrays/stats in the tail) that
    the upstream mount parser deliberately skips; reaching them
    requires further RE."""
    total, target_reached, walked_full, bails = _walk_table(
        "characterinfo", "_callMercenarySpawnDuration")
    assert target_reached == total, (
        f"characterinfo: only {target_reached}/{total} entries reached "
        f"_callMercenarySpawnDuration; bails: {bails}")


def test_format3_intent_on_real_iteminfo_writes_exactly_the_target_bytes():
    """End-to-end WRITE proof: a Format 3 intent that sets ``_cooltime``
    on a real vanilla entry must change those 8 bytes and NOTHING else.

    Asserts the OUTCOME, not the mechanism. The old version of this test
    pinned the mechanism -- ``change["patched"] == <8-byte i64>`` and a
    name-end-relative ``rel_offset`` -- and on CD 1.13 that is simply not
    how the write happens any more: the writer detects that the schema
    can't decode 1.13 records, falls back to the native relocated-layout
    writer, and emits a whole-table ``_f3_rebuild`` instead of a surgical
    offset patch.

    That is correct behaviour, and stricter than what it replaced: the
    rebuilt 5.9 MB body is byte-identical to vanilla except for the 8
    bytes of the edit. But a test written against the old mechanism reads
    that as a failure. So this asserts what actually matters -- the bytes
    that end up on disk -- and stays true across both write paths.

    (It also would not have caught the 1.13 change at all before now,
    because it was gated on a fixtures directory that never existed.)
    """
    import struct
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )
    from cdumm.semantic.parser import parse_pabgh_index

    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")

    parser_mod._loaded_schemas = None

    # Pyeonjeon_Arrow (key=2200). SENTINEL is unmistakably distinct from
    # anything in vanilla data.
    SENTINEL = 0x4242424242424242
    intent = Format3Intent(
        entry="Pyeonjeon_Arrow", key=2200,
        field="_cooltime", op="set", new=SENTINEL)

    # 1. Validator accepts the intent (proves the field is reachable).
    validation = validate_intents("iteminfo.pabgb", [intent])
    assert len(validation.supported) == 1, (
        f"validator skipped intent: {validation.skipped}")

    # 2. Apply path produces exactly one change.
    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", body, header, validation.supported)
    assert len(changes) == 1, (
        f"_intents_to_v2_changes produced {len(changes)} changes, "
        f"expected 1")
    change = changes[0]

    # 3. Reconstruct the body that would land on disk, whichever write
    #    path was taken.
    if change.get("_f3_rebuild"):
        new_body = bytes.fromhex(change["patched"])
        assert bytes.fromhex(change["original"]) == body, (
            "the rebuild's `original` is not the vanilla body it claims "
            "to be replacing")
    else:
        entry_off = parse_pabgh_index(header, "iteminfo")[1][2200]
        name_len = struct.unpack_from("<I", body, entry_off + 4)[0]
        abs_off = entry_off + 8 + name_len + change["rel_offset"]
        buf = bytearray(body)
        patch = bytes.fromhex(change["patched"])
        buf[abs_off:abs_off + len(patch)] = patch
        new_body = bytes(buf)

    # 4. Exactly 8 contiguous bytes changed, and they are the sentinel.
    assert len(new_body) == len(body), (
        f"body length changed: {len(body)} -> {len(new_body)}. A `set` on "
        f"a fixed-width field must not resize the table.")
    diff = [i for i in range(len(body)) if body[i] != new_body[i]]
    assert len(diff) == 8, (
        f"{len(diff)} bytes changed, expected 8. A `set` on one i64 field "
        f"must not touch anything else. First 16: {diff[:16]}")
    assert diff == list(range(diff[0], diff[0] + 8)), (
        f"the 8 changed bytes are not contiguous: {diff}")
    assert new_body[diff[0]:diff[0] + 8] == struct.pack("<q", SENTINEL), (
        f"changed bytes are not the sentinel: "
        f"{new_body[diff[0]:diff[0] + 8].hex()}")

    # 5. And they land inside Pyeonjeon_Arrow's record, not some other
    #    item's -- which is the failure a byte-count check alone misses.
    _, offsets = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offsets.values())
    entry_off = offsets[2200]
    nxt = next((s for s in starts if s > entry_off), len(body))
    assert entry_off <= diff[0] and diff[0] + 8 <= nxt, (
        f"the write landed at {diff[0]}, outside record 2200's range "
        f"[{entry_off}, {nxt})")
