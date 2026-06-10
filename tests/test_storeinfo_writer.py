"""GitHub #183 (pinapana): Format 3 ``stock_data_list`` writer.

End-to-end against the real inputs: the extracted CD 1.10 vanilla
storeinfo pair and the reporter's HernandPets mod (IHateLacey.json),
which sets store 3101's stock list to 41 records (the 37 vanilla ones
plus 4 added pets).

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
_MOD = _BASE / "IHateLacey.json"


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
    data = json.loads(_MOD.read_text(encoding="utf-8"))
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

    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    intent = _mod_intent()
    assert len(intent.new) == 41

    pabgb_changes, pabgh_change = build_storeinfo_changes(
        body, header, [intent])
    assert len(pabgb_changes) == 1
    assert pabgh_change is not None, "list grew, offsets must shift"

    patched = _apply(body, pabgb_changes)
    new_header = bytes.fromhex(pabgh_change["patched"])
    growth = len(patched) - len(body)
    assert growth > 0

    # The patched entry parses back to exactly the mod's 41 records.
    ks, offs = parse_pabgh_index(new_header, "storeinfo")
    _, _, payload = _parse_entry_header(patched, offs[3101], ks)
    records, _s, _e = parse_stock_list(
        patched, payload + LIST_COUNT_PAYLOAD_OFFSET)
    assert len(records) == 41

    # The 4 new pets carry the mod's values in the mapped fields.
    new_bodies = [r["value"]["payload"]["body"] for r in intent.new[37:]]
    tail = records[37:]
    assert [r.body for r in tail] == new_bodies
    for r, j in zip(tail, intent.new[37:]):
        assert r.raw_a == j["raw_a"] and r.raw_b == j["raw_b"]
        assert r.lookup_a == j["lookup_a"]
        assert (r.sub_data is None) == (j["sub_data"] is None)

    # Matched records keep vanilla bytes verbatim: the first 37 parsed
    # records re-serialize to the same bytes as vanilla's list payload
    # (the mod's interior diffs must NOT have been written).
    from cdumm.engine.storeinfo_native_parser import serialize_stock_list
    _, voffs = parse_pabgh_index(header, "storeinfo")
    _, _, vpayload = _parse_entry_header(body, voffs[3101], ks)
    vrecords, vs, ve = parse_stock_list(
        body, vpayload + LIST_COUNT_PAYLOAD_OFFSET)
    assert serialize_stock_list(records[:37])[4:] == \
        serialize_stock_list(vrecords)[4:]

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
        StoreinfoWriteRefused, build_storeinfo_changes)
    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    intent = _mod_intent()
    # Make one ADDED record (not matching any vanilla body) carry a
    # non-zero unmapped interior value.
    bad = json.loads(json.dumps(intent.new))
    bad[37]["value"]["raw_b"] = 12345
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
    changes, hdr_change = build_storeinfo_changes(body, header, [intent])
    assert changes == [] and hdr_change is None
