"""Tests for the multichangeinfo.pabgb writer (GitHub #125).

These use a synthetic 3-record table built to the verified on-disk
shape rather than the real 4 MB game file, so the test is fast and
self-contained. The real-file round-trip (16806 records,
byte-identical) and the real-mod readback (9430 intents, 0 mismatch)
were verified during development against the vanilla 1.07.00 dump.

Verified format:
  pabgh: u16 count, then count*(u32 key, u32 offset).
  record: u32 key, u32 strlen, name[strlen], 0x00, 53 fixed bytes,
          u16 element_count, element_count * 30-byte elements,
          then trailing fields.
  element: u32 item_info @+0, ..., u64 count @+22.
"""
from __future__ import annotations

import struct

from cdumm.engine.multichangeinfo_writer import (
    apply_multichangeinfo,
    build_multichangeinfo_changes,
    build_pabgh,
    locate_fixed_material_list,
    parse_pabgh,
)

_PREARRAY = 53


def _make_element(item_info: int, count: int) -> bytes:
    e = bytearray(30)
    struct.pack_into("<I", e, 0, item_info)
    struct.pack_into("<Q", e, 22, count)
    return bytes(e)


def _make_record(key: int, name: str, elements: list[tuple[int, int]],
                 trailing: bytes = b"\x07\x07\x07\x07") -> bytes:
    nb = name.encode("latin-1")
    rec = bytearray()
    rec += struct.pack("<II", key, len(nb))
    rec += nb
    rec += b"\x00"
    rec += bytes(_PREARRAY)              # 53 fixed pre-array bytes
    rec += struct.pack("<H", len(elements))
    for item_info, count in elements:
        rec += _make_element(item_info, count)
    rec += trailing
    return bytes(rec)


def _make_table(records: list[bytes]) -> tuple[bytes, bytes]:
    """Return (pabgb, pabgh) for a list of record byte-strings."""
    pabgb = bytearray()
    entries: list[tuple[int, int]] = []
    for rec in records:
        key = struct.unpack_from("<I", rec, 0)[0]
        entries.append((key, len(pabgb)))
        pabgb += rec
    return bytes(pabgb), build_pabgh(entries)


def test_round_trip_no_intents_is_byte_identical():
    """The round-trip floor: applying no intents reproduces the input
    byte-for-byte (pabgb and pabgh)."""
    recs = [
        _make_record(1, "Arrow", []),
        _make_record(2, "Sword_Recipe", [(720001, 5), (720004, 3)]),
        _make_record(3, "Helm_Recipe", [(75001, 1)]),
    ]
    pabgb, pabgh = _make_table(recs)
    out_pabgb, out_pabgh = apply_multichangeinfo(pabgb, pabgh, {})
    assert out_pabgb == pabgb
    assert out_pabgh == pabgh


def test_locate_finds_array_via_formula():
    """The array sits at 8 + strlen + 1 + 53; locate returns that
    offset and the element count."""
    rec = _make_record(2, "Sword_Recipe", [(720001, 5), (720004, 3)])
    located = locate_fixed_material_list(rec)
    assert located is not None
    array_off, count = located
    assert array_off == 8 + len("Sword_Recipe") + 1 + _PREARRAY
    assert count == 2


def test_in_place_patch_item_info_and_count():
    """Patching an existing element's item_info and count writes the
    new values and leaves record size unchanged."""
    recs = [
        _make_record(2, "Sword_Recipe", [(720001, 5), (720004, 3)]),
    ]
    pabgb, pabgh = _make_table(recs)
    intents = {2: [(1, "item_info", 999999), (1, "count", 42)]}
    out_pabgb, out_pabgh = apply_multichangeinfo(pabgb, pabgh, intents)
    assert len(out_pabgb) == len(pabgb), "in-place patch must not resize"
    array_off, count = locate_fixed_material_list(out_pabgb)
    assert count == 2
    el1 = array_off + 2 + 30 * 1
    assert struct.unpack_from("<I", out_pabgb, el1)[0] == 999999
    assert struct.unpack_from("<Q", out_pabgb, el1 + 22)[0] == 42
    # element 0 untouched
    el0 = array_off + 2
    assert struct.unpack_from("<I", out_pabgb, el0)[0] == 720001


def test_extend_appends_elements_and_bumps_count():
    """An intent targeting an index past the current count extends the
    array: zeroed elements are appended, the u16 count is bumped, the
    record grows, and trailing fields survive."""
    recs = [
        _make_record(2, "Sword_Recipe", [(720001, 5)],
                     trailing=b"\xaa\xbb\xcc\xdd"),
    ]
    pabgb, pabgh = _make_table(recs)
    # element 0 exists; intent targets element 2 -> extend to 3.
    intents = {2: [(2, "item_info", 1), (2, "count", 200)]}
    out_pabgb, out_pabgh = apply_multichangeinfo(pabgb, pabgh, intents)
    assert len(out_pabgb) == len(pabgb) + 2 * 30, "two elements appended"
    array_off, count = locate_fixed_material_list(out_pabgb)
    assert count == 3
    # element 0 preserved
    assert struct.unpack_from("<I", out_pabgb, array_off + 2)[0] == 720001
    # element 2 set
    el2 = array_off + 2 + 30 * 2
    assert struct.unpack_from("<I", out_pabgb, el2)[0] == 1
    assert struct.unpack_from("<Q", out_pabgb, el2 + 22)[0] == 200
    # trailing fields survived the splice
    assert out_pabgb.endswith(b"\xaa\xbb\xcc\xdd")


def test_extend_rebuilds_pabgh_offsets_for_later_records():
    """When a record grows, every later record's pabgh offset must be
    rebuilt so the index still points at the right bytes."""
    recs = [
        _make_record(1, "First_Recipe", [(100, 1)]),
        _make_record(2, "Second_Recipe", [(200, 1)]),
    ]
    pabgb, pabgh = _make_table(recs)
    # extend record 1 -> record 2 shifts later in the file.
    intents = {1: [(3, "item_info", 5), (3, "count", 5)]}
    out_pabgb, out_pabgh = apply_multichangeinfo(pabgb, pabgh, intents)
    new_entries = dict(parse_pabgh(out_pabgh))
    rec2_off = new_entries[2]
    # the pabgh offset for record 2 must land on record 2's key (2).
    assert struct.unpack_from("<I", out_pabgb, rec2_off)[0] == 2


def test_untouched_records_are_byte_identical():
    """Records with no intents come through the writer unchanged."""
    recs = [
        _make_record(1, "Untouched_A", [(100, 1), (101, 2)]),
        _make_record(2, "Patched", [(200, 1)]),
        _make_record(3, "Untouched_B", [(300, 3)]),
    ]
    pabgb, pabgh = _make_table(recs)
    out_pabgb, out_pabgh = apply_multichangeinfo(
        pabgb, pabgh, {2: [(0, "count", 99)]})
    new_entries = dict(parse_pabgh(out_pabgh))
    old_entries = dict(parse_pabgh(pabgh))
    # record 1 is before the patched record, same offset + same bytes.
    assert new_entries[1] == old_entries[1]
    assert out_pabgb[new_entries[1]:new_entries[1] + len(recs[0])] == recs[0]


def _apply_offset_changes(body: bytes, changes: list[dict]) -> bytes:
    """Reproduce the apply pipeline's absolute-offset replace with
    cumulative shift, to verify per-record changes reassemble the
    pabgb the writer intends."""
    work = bytearray(body)
    shift = 0
    for c in sorted(changes, key=lambda c: c["offset"]):
        off = c["offset"] + shift
        orig = bytes.fromhex(c["original"])
        patched = bytes.fromhex(c["patched"])
        assert work[off:off + len(orig)] == orig, "original mismatch"
        work[off:off + len(orig)] = patched
        shift += len(patched) - len(orig)
    return bytes(work)


def test_build_changes_in_place_patch_no_pabgh():
    """An in-place patch produces one per-record pabgb change and no
    pabgh change (no record grew, so offsets are unchanged). Replaying
    the change reproduces the writer's pabgb."""
    recs = [
        _make_record(1, "Arrow", []),
        _make_record(2, "Sword_Recipe", [(720001, 5), (720004, 3)]),
    ]
    pabgb, pabgh = _make_table(recs)
    intents = [("Sword_Recipe", 0,
                "fixed_material_data_list[1].count", 99)]
    pabgb_changes, pabgh_change = build_multichangeinfo_changes(
        pabgb, pabgh, intents)
    assert len(pabgb_changes) == 1
    assert pabgh_change is None, "in-place patch must not rebuild pabgh"
    rebuilt = _apply_offset_changes(pabgb, pabgb_changes)
    expected, _ = apply_multichangeinfo(pabgb, pabgh, {2: [(1, "count", 99)]})
    assert rebuilt == expected


def test_build_changes_extend_emits_pabgh_change():
    """An extending intent grows a record, so the builder emits both a
    per-record pabgb change and a whole-body pabgh change. The pabgh
    change carries the writer's rebuilt index; replaying the pabgb
    change reproduces the writer's grown pabgb."""
    recs = [
        _make_record(1, "First_Recipe", [(100, 1)]),
        _make_record(2, "Second_Recipe", [(200, 1)]),
    ]
    pabgb, pabgh = _make_table(recs)
    intents = [
        ("First_Recipe", 0, "fixed_material_data_list[3].item_info", 5),
        ("First_Recipe", 0, "fixed_material_data_list[3].count", 5),
    ]
    pabgb_changes, pabgh_change = build_multichangeinfo_changes(
        pabgb, pabgh, intents)
    assert len(pabgb_changes) == 1
    assert pabgh_change is not None
    assert pabgh_change["offset"] == 0
    new_pabgb, new_pabgh = apply_multichangeinfo(
        pabgb, pabgh, {1: [(3, "item_info", 5), (3, "count", 5)]})
    assert _apply_offset_changes(pabgb, pabgb_changes) == new_pabgb
    assert bytes.fromhex(pabgh_change["original"]) == pabgh
    assert bytes.fromhex(pabgh_change["patched"]) == new_pabgh


def test_build_changes_skips_unresolvable_intents():
    """Bad field paths, unknown entry names and non-integer values are
    dropped; only the well-formed intent yields a change."""
    recs = [_make_record(2, "Sword_Recipe", [(720001, 5)])]
    pabgb, pabgh = _make_table(recs)
    intents = [
        ("Sword_Recipe", 0, "fixed_material_data_list[0].count", 7),
        ("Ghost_Recipe", 0, "fixed_material_data_list[0].count", 7),
        ("Sword_Recipe", 0, "unrelated_field", 7),
        ("Sword_Recipe", 0, "fixed_material_data_list[0].count", "big"),
    ]
    pabgb_changes, _pabgh_change = build_multichangeinfo_changes(
        pabgb, pabgh, intents)
    assert len(pabgb_changes) == 1
    rebuilt = _apply_offset_changes(pabgb, pabgb_changes)
    expected, _ = apply_multichangeinfo(pabgb, pabgh, {2: [(0, "count", 7)]})
    assert rebuilt == expected


def test_build_changes_resolves_by_numeric_key_fallback():
    """When the entry name does not match a record, a non-zero numeric
    key still resolves the record."""
    recs = [_make_record(4242, "Real_Name", [(1, 1)])]
    pabgb, pabgh = _make_table(recs)
    # entry name is wrong but key 4242 is correct.
    intents = [("Wrong_Name", 4242,
                "fixed_material_data_list[0].item_info", 9)]
    pabgb_changes, _pabgh = build_multichangeinfo_changes(
        pabgb, pabgh, intents)
    assert len(pabgb_changes) == 1
    rebuilt = _apply_offset_changes(pabgb, pabgb_changes)
    expected, _ = apply_multichangeinfo(
        pabgb, pabgh, {4242: [(0, "item_info", 9)]})
    assert rebuilt == expected


def test_multichangeinfo_intents_pass_validation(tmp_path):
    """GitHub #125: fixed_material_data_list[N].item_info / .count are
    dotted+indexed paths the generic nested-struct walker rejects. The
    validator must early-accept them so they reach the writer."""
    import json
    from cdumm.engine.format3_handler import (
        parse_format3_mod_targets, validate_intents,
    )
    doc = {
        "format": 3,
        "format_minor": 1,
        "modinfo": {"title": "refinement probe", "version": "1.0"},
        "targets": [{
            "file": "multichangeinfo.pabgb",
            "intents": [
                {"entry": "Marni_Devotee_PlateArmor_Helm_4",
                 "field": "fixed_material_data_list[4].item_info",
                 "op": "set", "new": 1},
                {"entry": "Marni_Devotee_PlateArmor_Helm_4",
                 "field": "fixed_material_data_list[4].count",
                 "op": "set", "new": 200},
            ],
        }],
    }
    p = tmp_path / "refinement.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    target, intents = parse_format3_mod_targets(p)[0]
    assert target == "multichangeinfo.pabgb"
    v = validate_intents(target, intents)
    assert len(v.supported) == 2, (
        f"both multichangeinfo intents should validate, got "
        f"supported={len(v.supported)} skipped={v.skipped}")
    assert len(v.skipped) == 0
