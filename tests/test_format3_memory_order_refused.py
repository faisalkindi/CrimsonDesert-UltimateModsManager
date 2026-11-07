"""Format 3 refuses to place a field from a schema still in memory order.

A table with no ``_ordered_fields`` in schemas/pabgb_type_overrides.json
keeps the base schema's field list: the field descriptors sorted by their
address in memory, minus every field the base schema gives no stream size.
That is not the order a record stores them. The schema walk still reached
every fixed-size field in that list, so a write landed on a guessed byte,
and the "original" bytes it was checked against were read from the same
guessed spot, so nothing downstream noticed and Apply reported success.

Measured on the committed 1.13 dropsetinfo fixture, against
``dropset_writer.parse_dropset_record``, an independent reader that has to
consume every byte of a record: six ``drop_roll_count`` writes all landed
22 bytes past the field, and 3 of the 6 records no longer parsed. On the
1.15 interactioninfo fixture ``auto_moving_stop_distance`` landed inside a
length-prefixed string.

The fix is one rule, ``parser.vouches_for``, used by every Format 3 path
that places or matches a field by the schema: the per-record writer
(``format3_apply._resolve_write_pos``, which now says which fields it
refused), the record-op writer
(``format3_handler.apply_intents_to_pabgb_bytes``), and the validator's
checks of clone patches and match selectors. A table whose order was
curated (``_ordered_fields``) keeps writing as before; only
``_verified_fields`` narrows it further.

The validator still reports a plain set on such a field as supported, as
it does today for a curated table's unverified fields: it can't tell a
field the schema walk places from one a dedicated writer places (all of
buffinfo's, through the clean-room parser), so the refusal is made, and
reported, where the walk happens.
"""
from __future__ import annotations

import dataclasses
import json
import struct
import zlib
from pathlib import Path

import pytest

from cdumm.engine.dropset_writer import _parse_drop_entry, parse_dropset_record
from cdumm.engine.format3_apply import (
    _payload_offset,
    _resolve_write_pos,
    expand_format3_into_aggregated,
)
from cdumm.engine.format3_handler import (
    Format3Intent,
    apply_intents_to_pabgb_bytes,
    validate_intents,
)
from cdumm.semantic.parser import (
    FieldSpec,
    TableSchema,
    get_schema,
    parse_pabgh_index,
)
from cdumm.storage.database import Database

_FX = Path(__file__).resolve().parent / "fixtures"


def _load(ver: str, stem: str) -> tuple[bytes, bytes]:
    body = zlib.decompress((_FX / ver / f"{stem}.pabgb.zlib").read_bytes())
    hdr = zlib.decompress((_FX / ver / f"{stem}.pabgh.zlib").read_bytes())
    return body, hdr


def _records(body: bytes, hdr: bytes, stem: str):
    """``[(key, start, end)]`` in body order."""
    _ks, offs = parse_pabgh_index(hdr, stem)
    srt = sorted(offs.items(), key=lambda kv: kv[1])
    return [(k, off, srt[i + 1][1] if i + 1 < len(srt) else len(body))
            for i, (k, off) in enumerate(srt)]


def _expand(tmp_path, target: str, intents: list[dict],
            body: bytes, hdr: bytes) -> tuple[list[dict], list[str]]:
    """Run one Format 3 mod through the real expansion, in memory."""
    mod = {"modinfo": {"title": "M", "version": "1", "author": "t",
                       "description": "t"},
           "format": 3, "target": target, "intents": intents}
    src = tmp_path / "m.json"
    src.write_text(json.dumps(mod), encoding="utf-8")
    db = Database(tmp_path / "t.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, "
        "json_source) VALUES (1, 'M', 'paz', 1, 1, ?)", (str(src),))
    db.connection.commit()
    aggregated: dict = {}
    warnings: list[str] = []
    expand_format3_into_aggregated(
        aggregated, {}, db, lambda _t: (body, hdr), warnings_out=warnings)
    db.close()
    return aggregated.get(target, []), warnings


def _dropset_field_offsets(record: bytes) -> dict[str, tuple[int, int]]:
    """``{field: (offset_in_record, width)}`` as parse_dropset_record
    reads a 1.13 record; raises unless the read ends on the last byte."""
    pos = 4
    pos += 4 + struct.unpack_from("<I", record, pos)[0]      # name
    out = {"_isBlocked": (pos, 1), "_dropRollType": (pos + 1, 1),
           "_dropRollCount": (pos + 2, 4)}
    pos += 6
    pos += 4 + struct.unpack_from("<I", record, pos)[0]      # condition str
    out["_dropTagNameHash"] = (pos, 4)
    pos += 4
    n = struct.unpack_from("<I", record, pos)[0]
    pos += 4
    for _ in range(n):
        _drop, pos = _parse_drop_entry(record, pos)
    out["_neeSlotCount"] = (pos, 2)
    out["_needWeight"] = (pos + 2, 8)
    out["_totalDropRate"] = (pos + 10, 8)
    pos += 18
    pos += 4 + struct.unpack_from("<I", record, pos)[0]      # original str
    if pos != len(record):
        raise ValueError(f"read {pos} of {len(record)} bytes")
    return out


# ── the rule ────────────────────────────────────────────────────────────


def _schema(verified=None, memory_order=False) -> TableSchema:
    return TableSchema(
        "t", [FieldSpec("_a", 4, "direct_u32", "I"),
              FieldSpec("_b", 1, "direct_u8", "B")],
        verified_fields=verified, memory_order=memory_order)


def test_vouches_for_each_kind_of_schema():
    from cdumm.semantic.parser import vouches_for

    assert vouches_for(_schema(), "_a")                       # curated order
    assert not vouches_for(_schema(memory_order=True), "_a")  # base order
    gated = _schema(frozenset({"_a"}), memory_order=True)
    assert vouches_for(gated, "_a")                           # verified wins
    assert not vouches_for(gated, "_b")


def test_the_writer_refuses_a_memory_order_schema():
    body = struct.pack("<I", 7) + b"\x01"
    schema = _schema(memory_order=True)
    specs = {f.name: f for f in schema.fields}
    intent = Format3Intent(entry="", key=1, field="_b", op="set", new=1)
    assert _resolve_write_pos(
        intent, {}, specs, schema, body, 0, len(body)) is None
    curated = dataclasses.replace(schema, memory_order=False)
    assert _resolve_write_pos(
        intent, {}, specs, curated, body, 0, len(body)) == (4, 1, "B")


def test_a_curated_tables_unverified_field_is_reported_too(monkeypatch):
    """The same report covers an unverified field on a curated table. That
    was refused before this change as well, but with nothing said."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.semantic import parser as parser_mod

    schema = TableSchema(
        "synthgate", [FieldSpec("_alpha", 4, "direct_u32", "I"),
                      FieldSpec("_beta", 2, "direct_u16", "H")],
        verified_fields=frozenset({"_alpha"}))
    parser_mod._load_schemas()
    cache = dict(parser_mod._loaded_schemas or {})
    cache["synthgate"] = schema
    monkeypatch.setattr(parser_mod, "_loaded_schemas", cache)

    name = b"Rec"
    body = (struct.pack("<II", 7, len(name)) + name + b"\x00"
            + struct.pack("<IH", 1, 2))
    header = struct.pack("<H", 1) + struct.pack("<II", 7, 0)
    warnings: list[str] = []
    changes = _intents_to_v2_changes(
        "synthgate.pabgb", body, header,
        [Format3Intent(entry="Rec", key=7, field="_alpha", op="set", new=9),
         Format3Intent(entry="Rec", key=7, field="_beta", op="set", new=9)],
        warnings_out=warnings)
    assert [c["label"] for c in changes] == ["Rec._alpha"]
    assert warnings == [(
        "Format 3: 1 intent(s) on 'synthgate.pabgb' were refused, so "
        "nothing was written for _beta. It is not among synthgate's "
        "verified fields, so its offset is unproven.")]


@pytest.mark.parametrize("table,expected", [
    # no _ordered_fields: still the base schema's memory order
    ("dropsetinfo", True), ("interactioninfo", True), ("storeinfo", True),
    # _ordered_fields with no _verified_fields: curated, still writable
    ("fieldinfo", False), ("regioninfo", False), ("vehicleinfo", False),
    ("iteminfo", False), ("characterinfo", False),
    # _verified_fields: gated field by field, as before
    ("wantedinfo", False), ("stageinfo", False),
])
def test_the_loader_marks_tables_without_ordered_fields(table, expected):
    assert get_schema(table).memory_order is expected


# ── dropsetinfo, CD 1.13: the C2 case ──────────────────────────────────


def _first_parsing_dropsets(body, hdr, n=6):
    """The first ``n`` records parse_dropset_record reads, as the QC
    repro picks them. All 14,575 parse, so these are the first six."""
    out = []
    for k, off, end in _records(body, hdr, "dropsetinfo"):
        out.append((k, parse_dropset_record(body[off:end])))
        if len(out) == n:
            break
    return out


def test_drop_roll_count_is_refused_not_written(tmp_path):
    """Before the fix: 6 changes, all 22 bytes past _dropRollCount, and no
    warning. Now: nothing is written, and the warning names the field."""
    body, hdr = _load("vanilla113", "dropsetinfo")
    recs = _first_parsing_dropsets(body, hdr)
    intents = [{"entry": ds.name, "key": k, "field": "drop_roll_count",
                "op": "set", "new": ds.drop_roll_count + 1}
               for k, ds in recs]

    changes, warnings = _expand(
        tmp_path, "dropsetinfo.pabgb", intents, body, hdr)
    assert changes == []
    refused = [w for w in warnings if "were refused" in w]
    assert len(refused) == 1, warnings
    assert refused[0].startswith(
        "Format 3: 6 intent(s) on 'dropsetinfo.pabgb' were refused, so "
        "nothing was written for drop_roll_count. CDUMM's schema for "
        "dropsetinfo lists its fields in memory order"), refused[0]


def test_the_memory_order_walk_misses_every_dropsetinfo_field():
    """The evidence for the rule, pinned. With the gate lifted, the walk
    places each of dropsetinfo's seven scalar fields somewhere other than
    where parse_dropset_record reads it, on every record of the fixture.
    If a future schema change makes any of them land right, this fails,
    and the table can be given _ordered_fields instead."""
    body, hdr = _load("vanilla113", "dropsetinfo")
    ks, _offs = parse_pabgh_index(hdr, "dropsetinfo")
    schema = dataclasses.replace(get_schema("dropsetinfo"),
                                 memory_order=False)
    specs = {f.name: f for f in schema.fields}
    scalars = [f.name for f in schema.fields
               if f.struct_fmt and f.stream_size]
    assert len(scalars) == 7

    records = right = wrong = 0
    for k, off, end in _records(body, hdr, "dropsetinfo"):
        real = _dropset_field_offsets(body[off:end])
        records += 1
        po = _payload_offset(body, off, ks)
        for name in scalars:
            got = _resolve_write_pos(
                Format3Intent(entry="", key=k, field=name, op="set", new=1),
                {}, specs, schema, body, po, end)
            assert got is not None, (k, name)
            if (got[0] - off, got[1]) == real[name]:
                right += 1
            else:
                wrong += 1
    assert records == 14575
    assert (right, wrong) == (0, 7 * 14575)


def test_a_field_schema_entry_still_places_the_field(tmp_path, monkeypatch):
    """The refusal tells authors a field_schema entry can place the field.
    That route doesn't use the schema walk, so the gate leaves it alone,
    and with the offset parse_dropset_record reads (two bytes past the
    name: _isBlocked, _dropRollType) the value lands on _dropRollCount.

    rel_offset counts from the payload start, which on this table is the
    byte after _isBlocked whenever _isBlocked is 0 -- true of all 14,575
    records here, so 1 is right for every one of them."""
    root = tmp_path / "fsroot"
    (root / "field_schema").mkdir(parents=True)
    (root / "field_schema" / "dropsetinfo.json").write_text(json.dumps(
        {"drop_roll_count": {"rel_offset": 1, "type": "u32"}}),
        encoding="utf-8")
    monkeypatch.setenv("CDUMM_FIELD_SCHEMA_ROOT", str(root))

    from cdumm.engine.json_patch_handler import (
        _apply_byte_patches,
        _build_name_offsets_for_v2,
        _pabgh_key_size_hint,
    )

    body, hdr = _load("vanilla113", "dropsetinfo")
    recs = _first_parsing_dropsets(body, hdr)
    intents = [{"entry": ds.name, "key": k, "field": "drop_roll_count",
                "op": "set", "new": ds.drop_roll_count + 1}
               for k, ds in recs]
    changes, warnings = _expand(
        tmp_path, "dropsetinfo.pabgb", intents, body, hdr)
    assert len(changes) == len(intents), warnings

    gf = "gamedata/dropsetinfo.pabgb"
    name_offsets = _build_name_offsets_for_v2(
        gf, body, hdr, key_size=_pabgh_key_size_hint(gf, hdr))
    data = bytearray(body)
    applied, mismatched, _relocated = _apply_byte_patches(
        data, changes, vanilla_data=body, name_offsets=name_offsets)
    assert (applied, mismatched) == (len(intents), 0)

    bounds = {k: (off, end) for k, off, end in _records(body, hdr,
                                                        "dropsetinfo")}
    for k, ds in recs:
        off, end = bounds[k]
        after = parse_dropset_record(bytes(data[off:end]))
        assert after.drop_roll_count == ds.drop_roll_count + 1
        assert dataclasses.replace(after, drop_roll_count=0) == \
            dataclasses.replace(ds, drop_roll_count=0)


def test_the_record_op_writer_refuses_too():
    """A mod with a clone/delete op on the table sends its other set
    intents through apply_intents_to_pabgb_bytes, a second schema walk.
    Before the fix it wrote _dropRollCount at the memory-order position
    (exact engine names only reach it)."""
    body, hdr = _load("vanilla113", "dropsetinfo")
    (k, ds), = _first_parsing_dropsets(body, hdr, n=1)
    out = apply_intents_to_pabgb_bytes(
        "dropsetinfo", body, hdr,
        [Format3Intent(entry=ds.name, key=k, field="_dropRollCount",
                       op="set", new=ds.drop_roll_count + 1)])
    assert out == body


def test_clone_patches_and_match_selectors_are_refused():
    """Both read or write a field at the schema's position, so both are
    held to the same rule as a set."""
    v = validate_intents("dropsetinfo.pabgb", [
        Format3Intent(entry="", key=0, field="", op="clone_record",
                      new=None,
                      clone={"source_key": 9500001, "new_key": 9599999,
                             "patches": [{"field": "_dropRollCount",
                                          "new": 3}]}),
        Format3Intent(entry="", key=0, field="drops", op="set", new=[],
                      match={"_dropRollCount": 1}),
    ])
    assert v.supported == []
    reasons = [why for _i, why in v.skipped]
    assert "not a verified field" in reasons[0], reasons
    assert "not a verified field" in reasons[1], reasons


# ── interactioninfo, CD 1.15 ────────────────────────────────────────────


def test_auto_moving_stop_distance_is_refused(tmp_path):
    """The walk put this u8 20 bytes past the record's name, inside its
    length-prefixed decimal string (u32 length at +13, 16 digits at
    +17..+32, counted from the end of the name), on Gimmick_PickUp and
    Npc_Shop_120 alike."""
    body, hdr = _load("vanilla115", "interactioninfo")
    changes, warnings = _expand(
        tmp_path, "interactioninfo.pabgb",
        [{"entry": "Gimmick_PickUp", "key": 1000004,
          "field": "auto_moving_stop_distance", "op": "set", "new": 7}],
        body, hdr)
    assert changes == []
    assert any("were refused" in w and "auto_moving_stop_distance" in w
               for w in warnings), warnings


def test_a_curated_order_table_without_verified_fields_still_validates():
    """vehicleinfo has _ordered_fields and no _verified_fields; the fix
    leaves it writable, as it was."""
    v = validate_intents("vehicleinfo.pabgb", [Format3Intent(
        entry="Horse", key=16960, field="_canCallInSafeZone", op="set",
        new=1)])
    assert len(v.supported) == 1, v.skipped
