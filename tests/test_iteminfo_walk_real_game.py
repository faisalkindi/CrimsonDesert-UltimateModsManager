"""Integration tests — Path B walker on real installed PABGB tables.

Each test skips automatically when the user's installed Crimson Desert
game files aren't available (CI, fresh checkout, etc.). When the game IS
available, verifies the walker against vanilla bytes.

Per-table coverage status (verified 2026-04-27):
  * iteminfo:    100% byte-perfect across all 6339 entries
  * vehicleinfo: 100% target reach (_canCallInSafeZone) across all 32 entries
  * fieldinfo:   100% reach for _canCallVehicle across all 7 entries.
                 _alwaysCallVehicle_dev is not reachable via walker
                 (after undecoded variable-length _complexData field).
  * stageinfo:   93.5% target reach (_completeCount) across 50463 entries.
                 Remaining 6.5% need further RE on _sequencerDesc edge cases
                 and earlier list fields.

To run locally:

  CDUMM_VANILLA_ITEMINFO_DIR=C:/path/to/extracted py -3 -m pytest \
      tests/test_iteminfo_walk_real_game.py -v

Or place the files at:

  tests/fixtures/iteminfo/iteminfo.pabgb     (and .pabgh)
  tests/fixtures/iteminfo/vehicleinfo.pabgb  (and .pabgh)
  tests/fixtures/iteminfo/fieldinfo.pabgb    (and .pabgh)
  tests/fixtures/iteminfo/stageinfo.pabgb    (and .pabgh)

Fixtures are NOT committed (sizes range from 847 B to 25 MB).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cdumm.engine.format3_apply import _consume_field_bytes, _payload_offset
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import get_schema, parse_pabgh_index


def _find_vanilla_pair(table_name: str) -> tuple[Path, Path] | None:
    """Locate vanilla {table}.pabgb + {table}.pabgh. Returns
    ``(body_path, header_path)`` or ``None`` if not found.

    Lookup order:
      1. ``CDUMM_VANILLA_ITEMINFO_DIR`` env var (folder containing all tables)
      2. ``tests/fixtures/iteminfo/`` next to this file
    """
    env = os.environ.get("CDUMM_VANILLA_ITEMINFO_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).parent / "fixtures" / "iteminfo")

    for d in candidates:
        body = d / f"{table_name}.pabgb"
        header = d / f"{table_name}.pabgh"
        if body.exists() and header.exists():
            return body, header
    return None


def _find_vanilla_iteminfo() -> tuple[Path, Path] | None:
    """Backwards-compat alias for the original iteminfo-only test."""
    return _find_vanilla_pair("iteminfo")


def test_walker_handles_every_vanilla_iteminfo_entry_byte_perfect():
    """For every vanilla item, the walker must:
      * reach ``_cooltime``,
      * walk the full entry without bailing on any field,
      * land at exactly the next entry's start (no over- or under-read).
    """
    paths = _find_vanilla_iteminfo()
    if paths is None:
        pytest.skip(
            "Vanilla iteminfo.pabgb/.pabgh not found. Set "
            "CDUMM_VANILLA_ITEMINFO_DIR or drop both files at "
            "tests/fixtures/iteminfo/ to run this integration test.")
    body_path, header_path = paths

    # Fresh schema load so any cached state from earlier tests is gone.
    parser_mod._loaded_schemas = None
    schema = get_schema("iteminfo")
    assert schema is not None, "ItemInfo schema not loaded"

    body = body_path.read_bytes()
    header = header_path.read_bytes()

    key_size, offsets = parse_pabgh_index(header, "iteminfo")
    assert key_size == 4, f"unexpected key_size {key_size}"
    assert offsets, "no entries in pabgh index"

    sorted_entries = sorted(offsets.items(), key=lambda kv: kv[1])

    # Per-entry counters — accumulated for a clear failure message.
    total = len(sorted_entries)
    reached_cooltime = 0
    walked_full = 0
    exact_size = 0
    bail_field_counts: dict[str, int] = {}
    size_deltas: dict[int, int] = {}

    for idx, (_key, entry_off) in enumerate(sorted_entries):
        entry_end = (sorted_entries[idx + 1][1]
                     if idx + 1 < total else len(body))
        payload_off = _payload_offset(
            body, entry_off, key_size,
            no_null_skip=schema.no_null_skip)
        assert payload_off is not None, (
            f"entry {idx}: payload_offset failed")

        off = payload_off
        bailed = False
        saw_cooltime = False
        for f in schema.fields:
            consumed = _consume_field_bytes(body, off, f, entry_end)
            if consumed is None:
                bail_field_counts[f.name] = (
                    bail_field_counts.get(f.name, 0) + 1)
                bailed = True
                break
            off += consumed
            if f.name == "_cooltime":
                saw_cooltime = True

        if saw_cooltime:
            reached_cooltime += 1
        if not bailed:
            walked_full += 1
            delta = off - entry_end
            size_deltas[delta] = size_deltas.get(delta, 0) + 1
            if delta == 0:
                exact_size += 1

    # Hard assertions — anything less than total = regression.
    assert reached_cooltime == total, (
        f"only {reached_cooltime}/{total} entries reached _cooltime; "
        f"bail counts: {bail_field_counts}")
    assert walked_full == total, (
        f"only {walked_full}/{total} entries walked the full schema; "
        f"bail counts: {bail_field_counts}")
    assert exact_size == total, (
        f"only {exact_size}/{total} entries had exact size match; "
        f"size deltas: {size_deltas}")


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
    """Vehicleinfo override must reach `_canCallInSafeZone` (the field
    NattKh's parser was built to target) for every entry."""
    total, target_reached, walked_full, bails = _walk_table(
        "vehicleinfo", "_canCallInSafeZone")
    assert target_reached == total, (
        f"vehicleinfo: only {target_reached}/{total} entries reached "
        f"_canCallInSafeZone; bails: {bails}")


def test_value_correctness_spot_checks_across_path_b_tables():
    """Iteration 14-15 systematic-debugging: lock in known field VALUES
    against vanilla data, not just walker reachability. The vehicleinfo
    bug (reading wrong byte for _canCallInSafeZone) only surfaced
    because we checked actual values; the original walker test passed
    because target was reached, but the byte position was wrong.

    This test catches schema misalignments on any of the 6 ported
    tables by asserting known/documented field values match.
    """
    import struct
    from cdumm.engine.format3_apply import _consume_field_bytes, _payload_offset
    from cdumm.semantic.parser import get_schema, parse_pabgh_index

    def read_value(tbl, target_field, target_key, fmt):
        paths = _find_vanilla_pair(tbl)
        if paths is None:
            return None  # signal skip
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
        end = (sorted_e[idx + 1][1] if idx + 1 < len(sorted_e)
               else len(body))
        po = _payload_offset(body, eoff, key_size,
                              no_null_skip=schema.no_null_skip,
                              no_entry_header=schema.no_entry_header)
        off = po
        for f in schema.fields:
            if f.name == target_field:
                return struct.unpack_from(fmt, body, off)[0]
            consumed = _consume_field_bytes(body, off, f, end)
            if consumed is None:
                return ("walker_bailed_at", f.name)
            off += consumed
        return ("field_not_found_in_schema", target_field)

    # Ground-truth values: matched against crimson-rs roundtrip tests
    # (Pyeonjeon_Arrow) and NattKh's documented vehicleinfo values
    # (Horse, BearWarMachine, Dragon).
    checks = [
        ("iteminfo", "_maxStackCount", 2200, 100, "<Q",
         "crimson-rs Pyeonjeon_Arrow stacks 100"),
        ("iteminfo", "_isBlocked", 2200, 0, "<B",
         "Pyeonjeon_Arrow not blocked"),
        ("vehicleinfo", "_canCallInSafeZone", 16960, 1, "<B",
         "NattKh: Horse only entry with this flag set"),
        ("vehicleinfo", "_mountCallType", 16960, 1, "<B",
         "NattKh: Horse is rideable (=1)"),
        ("vehicleinfo", "_mountCallType", 16984, 2, "<B",
         "NattKh: Dragon is flying (=2)"),
        ("vehicleinfo", "_mountCallType", 16998, 0, "<B",
         "NattKh: BearWarMachine is siege (=0)"),
    ]
    skipped_any = False
    for tbl, field, key, expected, fmt, desc in checks:
        actual = read_value(tbl, field, key, fmt)
        if actual is None:
            skipped_any = True
            continue
        assert actual == expected, (
            f"{tbl}.{field}[key={key}]: got {actual!r}, "
            f"expected {expected}. {desc}. Schema may be misaligned.")

    if skipped_any:
        pytest.skip("Some vanilla fixtures missing — set "
                    "CDUMM_VANILLA_ITEMINFO_DIR")


def test_vehicleinfo_horse_canCallInSafeZone_reads_as_one():
    """Iteration 13 systematic-debugging finding: previous vehicleinfo
    walk completed all 32 entries but ended 2 bytes short of entry_end.
    Investigation showed `_mountCallType` and `_canCallInSafeZone` were
    pointing at the WRONG bytes — there are 2 unknown bytes between
    `_vehicleCharKey` and `_mountCallType` that the schema missed.

    Empirical truth (NattKh: "_canCallInSafeZone — only Horse=1 in
    vanilla"). Verifies schema correctly identifies Horse's flag value.
    """
    import struct
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
        f"Horse._canCallInSafeZone read as {can_call_value} but "
        f"NattKh's documented vanilla value is 1. The vehicleinfo "
        f"schema is reading the wrong byte position — likely missing "
        f"~2 bytes between _vehicleCharKey and _mountCallType.")


def test_fieldinfo_can_call_vehicle_reachable_for_every_entry():
    """Fieldinfo override walks fields 1-19 for every entry. Target
    `_canCallVehicle` (field 20 in IDA order, index 18 in payload) is
    the deepest field reachable before the undecoded `_complexData`
    block. NattKh's `_alwaysCallVehicle_dev` is past that block and
    requires further RE work — explicitly out of Path B's scope."""
    total, target_reached, walked_full, bails = _walk_table(
        "fieldinfo", "_canCallVehicle")
    assert target_reached == total, (
        f"fieldinfo: only {target_reached}/{total} entries reached "
        f"_canCallVehicle; bails: {bails}")


def test_stageinfo_complete_count_reachable_for_majority():
    """Stageinfo override reaches `_completeCount` for 93.5% of entries
    (verified empirically 2026-04-27: 47186/50463). The remaining 6.5%
    hit `_sequencerDesc` optional-object variant NattKh's parser also
    can't decode. Asserts a hard floor of 93% so a regression dropping
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
    """RegionInfo override walks all 24 fields with no entry header (NattKh
    confirms RegionInfo lacks the standard entry header — _key and
    _stringKey are regular schema fields). Verifies the
    ``_no_entry_header: true`` flag plumbed through ``_payload_offset``."""
    total, target_reached, walked_full, bails = _walk_table(
        "regioninfo", "_isTown")
    assert target_reached == total, (
        f"regioninfo: only {target_reached}/{total} entries reached "
        f"_isTown; bails: {bails}")
    assert walked_full == total, (
        f"regioninfo: only {walked_full}/{total} entries walked the "
        f"full schema; bails: {bails}")


def test_characterinfo_call_mercenary_spawn_duration_reachable():
    """CharacterInfo mount/vehicle subset (14 fields) — covers the fields
    NattKh's characterinfo_mount_parser.py exposes for ride-duration and
    cooldown mods. The full CharacterInfo entry has many more fields
    (complex arrays/stats in the tail) that NattKh's mount parser
    deliberately skips; reaching them requires further RE."""
    total, target_reached, walked_full, bails = _walk_table(
        "characterinfo", "_callMercenarySpawnDuration")
    assert target_reached == total, (
        f"characterinfo: only {target_reached}/{total} entries reached "
        f"_callMercenarySpawnDuration; bails: {bails}")


def test_format3_intent_on_real_iteminfo_produces_correct_v2_byte_patch():
    """End-to-end WRITE proof: take a Format 3 intent that sets
    `_cooltime` on a real vanilla iteminfo entry, run it through the
    actual ``_intents_to_v2_changes`` apply path, and assert the
    produced byte-patch dict has correct ``rel_offset`` + ``patched``
    bytes (i64 little-endian).

    This closes the adversarial reviewer's A1 finding — the previous
    walker test only proved the walker REACHES `_cooltime`, never that
    a write to that offset produces correct bytes against real data
    (where 22 variable-length predecessors are non-trivial).
    """
    import struct
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )

    paths = _find_vanilla_pair("iteminfo")
    if paths is None:
        pytest.skip("Vanilla iteminfo not available")
    body = paths[0].read_bytes()
    header = paths[1].read_bytes()

    parser_mod._loaded_schemas = None

    # Use a known-stable item: Pyeonjeon_Arrow (key=2200), the first
    # entry per our exploratory walk. Set cooltime to a sentinel value
    # that's unmistakably distinct from anything in vanilla data.
    SENTINEL = 0x4242424242424242
    intent = Format3Intent(
        entry="Pyeonjeon_Arrow", key=2200,
        field="_cooltime", op="set", new=SENTINEL)

    # 1. Validator must accept the intent (proves field is reachable)
    validation = validate_intents("iteminfo.pabgb", [intent])
    assert len(validation.supported) == 1, (
        f"validator skipped intent: {validation.skipped}")

    # 2. Apply path must produce one v2-style byte-patch dict
    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", body, header, validation.supported)
    assert len(changes) == 1, (
        f"_intents_to_v2_changes produced {len(changes)} changes, "
        f"expected 1")
    change = changes[0]

    # 3. The patched bytes must be SENTINEL packed as i64 little-endian
    expected_patched = struct.pack("<q", SENTINEL).hex()
    assert change["patched"] == expected_patched, (
        f"patched bytes wrong: got {change['patched']!r}, "
        f"expected {expected_patched!r}")

    # 4. Apply the change to the body and verify cooltime now reads
    #    SENTINEL when re-walked. Catches off-by-one errors that the
    #    walker-only test would miss.
    rel_off = change["rel_offset"]
    # Find entry 0's actual file offset via the pabgh index
    from cdumm.semantic.parser import parse_pabgh_index
    _, offsets = parse_pabgh_index(header, "iteminfo")
    entry_off = offsets[2200]
    abs_off = entry_off + rel_off

    new_body = bytearray(body)
    new_body[abs_off:abs_off + 8] = bytes.fromhex(change["patched"])

    # Re-walk to verify the value at cooltime's position is SENTINEL
    from cdumm.engine.format3_apply import _consume_field_bytes
    schema = get_schema("iteminfo")
    payload_off = _payload_offset(
        new_body, entry_off, 4,
        no_null_skip=schema.no_null_skip,
        no_entry_header=getattr(schema, "no_entry_header", False))
    sorted_offs = sorted(offsets.items(), key=lambda kv: kv[1])
    next_entry = sorted_offs[1][1]

    off = payload_off
    cooltime_value = None
    for f in schema.fields:
        if f.name == "_cooltime":
            cooltime_value = struct.unpack_from(
                "<q", new_body, off)[0]
            break
        consumed = _consume_field_bytes(
            bytes(new_body), off, f, next_entry)
        off += consumed

    assert cooltime_value == SENTINEL, (
        f"after-patch cooltime read got {cooltime_value:#x}, "
        f"expected {SENTINEL:#x}")
