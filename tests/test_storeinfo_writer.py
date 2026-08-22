"""GitHub #183 (pinapana): Format 3 ``stock_data_list`` writer.

End-to-end against the real inputs: the extracted current-build (CD
1.11) vanilla storeinfo pair and the reporter's HernandPets mod
(HernandPets_v1.1.json, updated for the 1.11 is_restore_item layout),
which sets store 3101's stock list to 42 records. On the 1.11 build 37
of those match a vanilla record by identity and 5 are new.

Safety contract pinned here:
* matched records take the mod's mapped per-slot fields (quantity,
  flags, restock/sort order), falling back to vanilla for any field
  the mod's JSON omits, but keep the vanilla value-struct interior
  (interior diffs in the mod JSON are stale-export noise from an older
  game version and must not overwrite current data),
* new records build from the mapped fields with the unmapped value
  interior zeroed,
* a new record carrying a non-zero unmapped field REFUSES the intent,
* the companion .pabgh offsets shift by exactly the list growth for
  every entry after the target, and byte ranges outside the patched
  span survive untouched.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.fixture_loaders import has_vanilla1161 as _have_vanilla1161
from tests.fixture_loaders import load_vanilla1161 as _load_vanilla1161

_BASE = Path(__file__).resolve().parents[1] / "issue_repro" / "183"
_BODY = _BASE / "vanilla" / "storeinfo.pabgb"
_HDR = _BASE / "vanilla" / "storeinfo.pabgh"
_MOD = _BASE / "HernandPets_v1.1.json"


def _fixture_layout():
    """The layout this snapshot was extracted under — CD 1.11.

    Pinned rather than defaulted (GitHub #351). ``parse_stock_list``
    defaults to the *newest* layout, so a verification step that omits it
    parses this 1.11 snapshot as whatever ships next: CD 1.11 puts the
    const byte at record offset 34 and CD 1.16 at 42, and the resulting
    "const byte at record offset 42 is 0" read as a live format
    regression when nothing had changed.

    ``build_storeinfo_changes`` itself detects the layout, so this is
    only needed where the test re-parses to check the writer's output.
    """
    from cdumm.engine.storeinfo_native_parser import LAYOUTS
    return {ly.label: ly for ly in LAYOUTS}["CD 1.11"]


def _have_fixtures() -> bool:
    return _BODY.exists() and _HDR.exists() and _MOD.exists()


@dataclass
class _Intent:
    entry: str
    key: int
    field: str
    op: str
    new: Any


def _mod_intent():
    data = json.loads(_MOD.read_text(encoding="utf-8-sig"))
    raw = data["targets"][0]["intents"][0]
    return _Intent(entry=raw.get("entry", ""), key=raw["key"],
                   field=raw["field"], op=raw.get("op", "set"),
                   new=raw["new"])


def _apply(body: bytes, changes: list[dict]) -> bytes:
    out = bytearray(body)
    # absolute-offset replaces, applied descending so offsets stay valid
    for c in sorted(changes, key=lambda c: c["offset"], reverse=True):
        start = c["offset"]
        orig = bytes.fromhex(c["original"])
        patched = bytes.fromhex(c["patched"])
        assert bytes(out[start:start + len(orig)]) == orig
        out[start:start + len(orig)] = patched
    return bytes(out)


@pytest.mark.skipif(not _have_fixtures(), reason="183 fixtures absent")
def test_hernandpets_applies_end_to_end():
    from cdumm.engine.storeinfo_native_parser import parse_stock_list
    from cdumm.engine.storeinfo_writer import _record_identity, build_storeinfo_changes
    from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index

    layout = _fixture_layout()
    list_count_off = layout.count_payload_offset

    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    intent = _mod_intent()
    assert len(intent.new) == 42

    pabgb_changes, pabgh_change = build_storeinfo_changes(
        body, header, [intent])
    assert len(pabgb_changes) == 1
    assert pabgh_change is not None, "list grew, offsets must shift"

    patched = _apply(body, pabgb_changes)
    new_header = bytes.fromhex(pabgh_change["patched"])
    growth = len(patched) - len(body)
    assert growth > 0

    # The patched entry parses back to exactly the mod's 42 records.
    ks, offs = parse_pabgh_index(new_header, "storeinfo")
    _, _, payload = _parse_entry_header(patched, offs[3101], ks)
    records, _s, _e = parse_stock_list(
        patched, payload + list_count_off, layout)
    assert len(records) == 42

    # Split the mod's records into matched-vanilla vs new by identity
    # (body), the same key the writer uses, instead of assuming a fixed
    # tail position.
    _, voffs = parse_pabgh_index(header, "storeinfo")
    _, _, vpayload = _parse_entry_header(body, voffs[3101], ks)
    vrecords, _vs, _ve = parse_stock_list(
        body, vpayload + list_count_off, layout)
    vbodies = {r.body for r in vrecords}
    new_js = [j for j in intent.new
              if _record_identity(j) not in vbodies]
    assert len(new_js) == 5, "37 of 42 match vanilla, 5 are new"

    # Every new record carries the mod's mapped values.
    by_body = {r.body: r for r in records}
    for j in new_js:
        r = by_body[_record_identity(j)]
        assert r.raw_a == j["raw_a"] and r.raw_b == j["raw_b"]
        assert r.lookup_a == j["lookup_a"]
        assert (r.sub_data is None) == (j["sub_data"] is None)

    # Matched records keep vanilla's value-struct interior verbatim
    # (the mod's ``value.*`` diffs must NOT have been written -- the
    # #183 protection this fixture exists for) even though their
    # mapped head fields now come from the mod's JSON.
    vby_body = {r.body: r for r in vrecords}
    for r in records:
        if r.body in vbodies:
            van = vby_body[r.body]
            assert r.vgap == van.vgap, r.body
            assert r.effect_list == van.effect_list, r.body

    # Every entry offset after store 3101 shifted by exactly +growth.
    for key, voff in voffs.items():
        if voff > voffs[3101]:
            assert offs[key] == voff + growth, key
        else:
            assert offs[key] == voff, key

    # Bytes outside the patched span are untouched.
    start = pabgb_changes[0]["offset"]
    end = start + len(bytes.fromhex(pabgb_changes[0]["original"]))
    assert patched[:start] == body[:start]
    assert patched[start + len(bytes.fromhex(pabgb_changes[0]['patched'])):] \
        == body[end:]


@pytest.mark.skipif(not _have_fixtures(), reason="183 fixtures absent")
def test_new_record_with_unmapped_field_refuses():
    from cdumm.engine.storeinfo_native_parser import parse_stock_list
    from cdumm.engine.storeinfo_writer import (
        StoreinfoWriteRefused,
        _record_identity,
        build_storeinfo_changes,
    )
    from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index
    layout = _fixture_layout()
    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    intent = _mod_intent()
    # Find an ADDED record (not matching any vanilla body) by identity,
    # rather than assuming a fixed index, and make it carry a non-zero
    # unmapped interior value.
    ks, offs = parse_pabgh_index(header, "storeinfo")
    _, _, pl = _parse_entry_header(body, offs[3101], ks)
    vrecs, _s, _e = parse_stock_list(
        body, pl + layout.count_payload_offset, layout)
    vbodies = {r.body for r in vrecs}
    bad = json.loads(json.dumps(intent.new))
    new_i = next(i for i, j in enumerate(bad)
                 if _record_identity(j) not in vbodies)
    bad[new_i]["value"]["raw_b"] = 12345
    intent.new = bad
    with pytest.raises(StoreinfoWriteRefused, match="raw_b"):
        build_storeinfo_changes(body, header, [intent])


@pytest.mark.skipif(not _have_fixtures(), reason="183 fixtures absent")
def test_unknown_store_key_yields_no_changes():
    from cdumm.engine.storeinfo_writer import build_storeinfo_changes
    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    intent = _mod_intent()
    intent.key = 999999
    # Also clear the entry name: the writer falls back to resolving a
    # missing key by entry name, and the real mod carries the valid name
    # "Store_Her_General", so an unknown key alone still resolves. A
    # genuinely unknown store has neither.
    intent.entry = "NoSuchStore_zzz"
    changes, hdr_change = build_storeinfo_changes(body, header, [intent])
    assert changes == [] and hdr_change is None


@pytest.mark.skipif(
    not _have_vanilla1161("storeinfo.pabgb"),
    reason="vanilla1161 storeinfo fixture absent")
def test_new_record_applies_on_cd1161():
    """GitHub #365 follow-up: adding a stock record works again.

    Before the raw_e_off/raw_g_off/raw_q_off fix, every ADD on this
    layout refused outright ("the CD 1.16.1 value interior has not been
    re-derived"). This targets the committed vanilla1161 fixture
    directly (not the gitignored issue_repro snapshot from #183, which
    predates this layout), adds one brand-new stock record with distinct
    non-zero raw_e/raw_g/raw_q, and checks it comes back at exactly
    those values -- i.e. they landed at vgap[37]/[53]/[55], not the
    stale pre-1.16.1 vgap[41]/[57]/[59].
    """
    from cdumm.engine.storeinfo_native_parser import LAYOUTS, locate_stock_list
    from cdumm.engine.storeinfo_writer import build_storeinfo_changes
    from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index

    layout = next(ly for ly in LAYOUTS if ly.label == "CD 1.16.1")
    body = _load_vanilla1161("storeinfo.pabgb")
    header = _load_vanilla1161("storeinfo.pabgh")

    ks, offs = parse_pabgh_index(header, "storeinfo")
    key = 1  # Store_Her_Equipment on this fixture
    sorted_offs = sorted(offs.values()) + [len(body)]
    entry_end = sorted_offs[sorted_offs.index(offs[key]) + 1]
    _, ename, payload = _parse_entry_header(body, offs[key], ks)
    vrecords, _s, _e = locate_stock_list(body, payload, entry_end, key, layout)

    new_body_id = max(r.body for r in vrecords) + 1
    new_record = {
        "lookup_a": vrecords[0].lookup_a,
        "raw_a": 0, "raw_b": 0, "raw_c": 0, "raw_d": 0, "raw_e": 0,
        "order_index_113": 0xFFFFFFFF,
        "low_price_threshold_count": 0xFFFFFFFF,
        "flag_a": 0, "flag_b": 0, "flag_c": 0, "is_restore_item": 0,
        "value": {
            "payload": {"body": new_body_id},
            "raw_e": 0xDEAD1E, "raw_g": 0xBEEF, "raw_q": new_body_id,
        },
    }
    intent = _Intent(entry=ename, key=key, field="stock_data_list", op="set",
                     new=[{"value": {"payload": {"body": r.body}}}
                          for r in vrecords] + [new_record])

    pabgb_changes, pabgh_change = build_storeinfo_changes(body, header, [intent])
    assert pabgb_changes, "the add must not be refused"

    patched = _apply(body, pabgb_changes)
    new_header = bytes.fromhex(pabgh_change["patched"]) if pabgh_change else header
    _, noffs = parse_pabgh_index(new_header, "storeinfo")
    nsorted_offs = sorted(noffs.values()) + [len(patched)]
    nentry_end = nsorted_offs[nsorted_offs.index(noffs[key]) + 1]
    _, _, npayload = _parse_entry_header(patched, noffs[key], ks)
    out_records, _s, _e = locate_stock_list(
        patched, npayload, nentry_end, key, layout)

    added = next(r for r in out_records if r.body == new_body_id)
    assert struct.unpack_from(
        "<I", added.vgap, layout.raw_e_off)[0] == 0xDEAD1E
    assert struct.unpack_from(
        "<H", added.vgap, layout.raw_g_off)[0] == 0xBEEF
    assert struct.unpack_from(
        "<I", added.vgap, layout.raw_q_off)[0] == new_body_id
    # And nowhere near the stale pre-1.16.1 indices.
    assert struct.unpack_from("<I", added.vgap, 41)[0] != 0xDEAD1E


@pytest.mark.skipif(
    not _have_vanilla1161("storeinfo.pabgb"),
    reason="vanilla1161 storeinfo fixture absent")
def test_matched_record_applies_the_mods_quantity_on_cd1161():
    """GitHub #365 residual: a Format 3 'set' on stock_data_list is
    meant to replace a slot's fields outright, including ones an item
    already had before the mod. Before this fix, any record whose item
    id matched an existing vanilla record discarded the mod's fields
    wholesale and kept the vanilla bytes verbatim, so bumping the stock
    of an item a store already carried (as opposed to adding a
    brand-new one) silently did nothing.
    """
    from cdumm.engine.storeinfo_native_parser import LAYOUTS, locate_stock_list
    from cdumm.engine.storeinfo_writer import build_storeinfo_changes
    from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index

    layout = next(ly for ly in LAYOUTS if ly.label == "CD 1.16.1")
    body = _load_vanilla1161("storeinfo.pabgb")
    header = _load_vanilla1161("storeinfo.pabgh")

    ks, offs = parse_pabgh_index(header, "storeinfo")
    key = 3101
    sorted_offs = sorted(offs.values()) + [len(body)]
    entry_end = sorted_offs[sorted_offs.index(offs[key]) + 1]
    _, ename, payload = _parse_entry_header(body, offs[key], ks)
    vrecords, _s, _e = locate_stock_list(body, payload, entry_end, key, layout)

    target = vrecords[0]
    assert target.raw_c == 1  # pinned: what the fix needs to change

    # Reproduce the whole list (a Format 3 'set' exports every record,
    # matched or new); bump only the target's quantity.
    new_records = []
    for r in vrecords:
        j = {"value": {"payload": {"body": r.body}}}
        if r.body == target.body:
            j["raw_c"] = 999
        new_records.append(j)

    intent = _Intent(entry=ename, key=key, field="stock_data_list",
                     op="set", new=new_records)
    pabgb_changes, pabgh_change = build_storeinfo_changes(body, header, [intent])
    assert pabgb_changes, "the quantity bump must not be a no-op"

    patched = _apply(body, pabgb_changes)
    new_header = bytes.fromhex(pabgh_change["patched"]) if pabgh_change else header
    _, noffs = parse_pabgh_index(new_header, "storeinfo")
    nsorted_offs = sorted(noffs.values()) + [len(patched)]
    nentry_end = nsorted_offs[nsorted_offs.index(noffs[key]) + 1]
    _, _, npayload = _parse_entry_header(patched, noffs[key], ks)
    out_records, _s, _e = locate_stock_list(
        patched, npayload, nentry_end, key, layout)

    out = next(r for r in out_records if r.body == target.body)
    assert out.raw_c == 999
    # Untouched fields, and the whole opaque interior, still match
    # vanilla -- only the field the mod actually set moved.
    assert out.vgap == target.vgap
    assert out.lookup_a == target.lookup_a
    assert out.flag_a == target.flag_a and out.flag_c == target.flag_c
    assert out.sub_data == target.sub_data

    # Every other slot in the store, none of which the mod touched,
    # is untouched too.
    out_by_body = {r.body: r for r in out_records}
    for r in vrecords:
        if r.body != target.body:
            assert out_by_body[r.body].raw_c == r.raw_c
            assert out_by_body[r.body].vgap == r.vgap
