"""GitHub #190 part 2: equipslotinfo.pabgb ``entries[N].etl_hashes``.

Character Creator's Female Rapier and Shield Module sets the etl hash
lists of two records in equipslot entry 1 (3 -> 4 hashes and 5 -> 7
hashes). CDUMM rejected the mod as schema-less. The writer rewrites
the targeted records' hash lists, preserves every other byte
verbatim, and rebuilds the companion .pabgh offsets because the entry
grows.

Trust anchor: the record model (u32 etl_count + hashes + 66B fixed
block per record, u16 unk + u32 count entry head, 20B-item footer +
0xb954d87c terminator) must round-trip every entry of the extracted
CD 1.10 vanilla file byte-identically.
"""
from __future__ import annotations

import json
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_BASE = Path(__file__).resolve().parents[1] / "issue_repro" / "190"
_BODY = _BASE / "vanilla" / "equipslotinfo.pabgb"
_HDR = _BASE / "vanilla" / "equipslotinfo.pabgh"
_MODZIP = _BASE / "mod837_file10646.zip"
_MODJSON = "CharacterCreator/Female Rapier and Shield Module.json"


def _have_fixtures() -> bool:
    return _BODY.exists() and _HDR.exists() and _MODZIP.exists()


@dataclass
class _Intent:
    entry: str
    key: int
    field: str
    op: str
    new: Any


def _mod_intents():
    with zipfile.ZipFile(_MODZIP) as z:
        data = json.loads(z.read(_MODJSON))
    return [
        _Intent(entry=r.get("entry", ""), key=r["key"], field=r["field"],
                op=r.get("op", "set"), new=r["new"])
        for r in data["targets"][0]["intents"]
    ]


def _entries(body, header):
    from cdumm.semantic.parser import parse_pabgh_index, _parse_entry_header
    ks, offs = parse_pabgh_index(header, "equipslotinfo")
    spans = sorted(offs.values()) + [len(body)]
    out = {}
    for key, off in offs.items():
        _, _, payload = _parse_entry_header(body, off, ks)
        end = spans[spans.index(off) + 1]
        out[key] = (off, payload, end)
    return out


@pytest.mark.skipif(not _have_fixtures(), reason="190 fixtures absent")
def test_every_vanilla_entry_round_trips_byte_exact():
    from cdumm.engine.equipslotinfo_writer import (
        parse_entry_records, serialize_entry_payload)
    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    ents = _entries(body, header)
    assert len(ents) == 14
    for key, (_off, payload, end) in ents.items():
        unk, records, footer = parse_entry_records(body, payload, end)
        assert serialize_entry_payload(unk, records, footer) == \
            body[payload:end], f"entry {key} mis-round-tripped"


@pytest.mark.skipif(not _have_fixtures(), reason="190 fixtures absent")
def test_female_rapier_module_applies_end_to_end():
    from cdumm.engine.equipslotinfo_writer import (
        build_equipslotinfo_changes, parse_entry_records)
    from cdumm.semantic.parser import parse_pabgh_index, _parse_entry_header

    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    intents = _mod_intents()
    assert len(intents) == 2

    pabgb_changes, pabgh_change = build_equipslotinfo_changes(
        body, header, intents)
    assert len(pabgb_changes) == 1  # both records live in entry 1
    assert pabgh_change is not None  # entry grew -> offsets shift

    # Apply the replace and re-parse the patched entry.
    c = pabgb_changes[0]
    start = c["offset"]
    orig = bytes.fromhex(c["original"])
    patched_blob = bytes.fromhex(c["patched"])
    assert body[start:start + len(orig)] == orig
    patched = body[:start] + patched_blob + body[start + len(orig):]
    growth = len(patched) - len(body)
    assert growth == (len(intents[0].new) - 3 + len(intents[1].new) - 5) * 4

    new_header = bytes.fromhex(pabgh_change["patched"])
    ks, offs = parse_pabgh_index(new_header, "equipslotinfo")
    _, _, payload = _parse_entry_header(patched, offs[1], ks)
    spans = sorted(offs.values()) + [len(patched)]
    end = spans[spans.index(offs[1]) + 1]
    _unk, records, _footer = parse_entry_records(patched, payload, end)

    assert records[0][1] == [v & 0xFFFFFFFF for v in intents[0].new]
    assert records[1][1] == [v & 0xFFFFFFFF for v in intents[1].new]
    # untouched records keep their hash lists
    vk, voffs = parse_pabgh_index(header, "equipslotinfo")
    _, _, vpayload = _parse_entry_header(body, voffs[1], vk)
    vspans = sorted(voffs.values()) + [len(body)]
    vend = vspans[vspans.index(voffs[1]) + 1]
    _vu, vrecords, _vf = parse_entry_records(body, vpayload, vend)
    assert [r[1] for r in records[2:]] == [r[1] for r in vrecords[2:]]
    # fixed blocks preserved verbatim for ALL records
    assert [r[2] for r in records] == [r[2] for r in vrecords]

    # pabgh: every entry after entry 1 shifts by exactly +growth
    for key, voff in voffs.items():
        expect = voff + growth if voff > voffs[1] else voff
        assert offs[key] == expect, key


@pytest.mark.skipif(not _have_fixtures(), reason="190 fixtures absent")
def test_out_of_range_record_index_refuses():
    from cdumm.engine.equipslotinfo_writer import (
        EquipslotWriteRefused, build_equipslotinfo_changes)
    body = _BODY.read_bytes()
    header = _HDR.read_bytes()
    bad = _Intent(entry="", key=1, field="entries[99].etl_hashes",
                  op="set", new=[1, 2, 3])
    with pytest.raises(EquipslotWriteRefused, match="out of range"):
        build_equipslotinfo_changes(body, header, [bad])


@pytest.mark.skipif(not _have_fixtures(), reason="190 fixtures absent")
def test_validator_accepts_the_indexed_field_path():
    from cdumm.engine.format3_handler import Format3Intent, validate_intents
    intents = [
        Format3Intent(entry=i.entry, key=i.key, field=i.field,
                      op=i.op, new=i.new)
        for i in _mod_intents()
    ]
    res = validate_intents("equipslotinfo.pabgb", intents)
    assert len(res.supported) == 2, [r for _i, r in res.skipped]
