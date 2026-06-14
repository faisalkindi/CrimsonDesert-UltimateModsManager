"""GitHub #183 (pinapana): Format 3 ``stock_data_list`` writer.

End-to-end against the real inputs: the extracted current-build (CD
1.11) vanilla storeinfo pair and the reporter's HernandPets mod
(HernandPets_v1.1.json, updated for the 1.11 is_restore_item layout),
which sets store 3101's stock list to 42 records. On the 1.11 build 37
of those match a vanilla record by identity and 5 are new.

Safety contract pinned here:
* matched records keep their vanilla bytes verbatim (interior diffs in
  the mod JSON are stale-export noise from an older game version and
  must not overwrite current data),
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

_BASE = Path(__file__).resolve().parents[1] / "issue_repro" / "183"
_BODY = _BASE / "vanilla" / "storeinfo.pabgb"
_HDR = _BASE / "vanilla" / "storeinfo.pabgh"
_MOD = _BASE / "HernandPets_v1.1.json"


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
    from cdumm.engine.storeinfo_writer import build_storeinfo_changes
    from cdumm.engine.storeinfo_native_parser import (
        LIST_COUNT_PAYLOAD_OFFSET, parse_stock_list)
    from cdumm.semantic.parser import parse_pabgh_index, _parse_entry_header

    from cdumm.engine.storeinfo_native_parser import serialize_stock_list
    from cdumm.engine.storeinfo_writer import _record_identity

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
        patched, payload + LIST_COUNT_PAYLOAD_OFFSET)
    assert len(records) == 42

    # Split the mod's records into matched-vanilla vs new by identity
    # (body), the same key the writer uses, instead of assuming a fixed
    # tail position.
    _, voffs = parse_pabgh_index(header, "storeinfo")
    _, _, vpayload = _parse_entry_header(body, voffs[3101], ks)
    vrecords, _vs, _ve = parse_stock_list(
        body, vpayload + LIST_COUNT_PAYLOAD_OFFSET)
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

    # Matched records keep vanilla bytes verbatim: each parsed record
    # whose body matches a vanilla record re-serializes to that vanilla
    # record's exact bytes (the mod's interior diffs must NOT have been
    # written).
    vby_body = {r.body: r for r in vrecords}
    for r in records:
        if r.body in vbodies:
            from cdumm.engine.storeinfo_native_parser import _Writer
            wa, wb = _Writer(), _Writer()
            from cdumm.engine.storeinfo_native_parser import (
                write_stock_record)
            write_stock_record(wa, r)
            write_stock_record(wb, vby_body[r.body])
            assert bytes(wa.out) == bytes(wb.out), r.body

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
    from cdumm.engine.storeinfo_writer import (
        StoreinfoWriteRefused, build_storeinfo_changes, _record_identity)
    from cdumm.engine.storeinfo_native_parser import (
        LIST_COUNT_PAYLOAD_OFFSET, parse_stock_list)
    from cdumm.semantic.parser import parse_pabgh_index, _parse_entry_header
    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    intent = _mod_intent()
    # Find an ADDED record (not matching any vanilla body) by identity,
    # rather than assuming a fixed index, and make it carry a non-zero
    # unmapped interior value.
    ks, offs = parse_pabgh_index(header, "storeinfo")
    _, _, pl = _parse_entry_header(body, offs[3101], ks)
    vrecs, _s, _e = parse_stock_list(body, pl + LIST_COUNT_PAYLOAD_OFFSET)
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
