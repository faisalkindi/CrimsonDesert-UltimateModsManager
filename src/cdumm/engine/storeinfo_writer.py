"""Format 3 writer for storeinfo.pabgb ``stock_data_list`` (GitHub #183).

pinapana's HernandPets mod sets the full stock list of a store entry
(DMM generic field name ``stock_data_list`` = StoreInfo's
``_exchangeItemInfoListForSell``). This writer rebuilds the entry's
record list from the intent and rebuilds the companion .pabgh offsets,
mirroring the multichangeinfo writer's (pabgb_changes, pabgh_change)
contract.

Safety model (the value-struct interior is only partially mapped, see
storeinfo_native_parser):

* Records in the intent that MATCH a vanilla record (same
  ``value.payload.body``, which doubles as ``raw_q``) keep the vanilla
  bytes verbatim, reordered per the intent. Their pinned fields were
  validated identical across the whole ground-truth entry; interior
  diffs observed in real mods are stale-export noise from older game
  versions, which must NOT be written over current vanilla data.
* NEW records are built from the pinned fields plus sub_data, with the
  unmapped value interior zeroed. If a new record carries a NON-zero
  value in any unmapped interior field, the whole intent is refused —
  we cannot place the value, and a wrong placement corrupts the table
  (the game crashes on store open).
* Non-empty ``effect_list`` refuses (element layout not decoded).
"""
from __future__ import annotations

import logging
import struct

from cdumm.engine.storeinfo_native_parser import (
    LIST_COUNT_PAYLOAD_OFFSET,
    StockRecord,
    StoreinfoParseError,
    parse_stock_list,
    serialize_stock_list,
)
from cdumm.semantic.parser import parse_pabgh_index, _parse_entry_header

logger = logging.getLogger(__name__)

# JSON fields of the value struct whose binary position is NOT mapped
# yet. A new record carrying a non-zero value in any of these cannot
# be serialized faithfully.
_UNMAPPED_VALUE_FIELDS = (
    "disc", "lookup_a", "lookup_b", "lookup_c",
    "raw_a", "raw_b", "raw_c", "raw_d", "raw_f",
)
_UNMAPPED_RECORD_FIELDS = ("lookup_b", "lookup_c")


class StoreinfoWriteRefused(ValueError):
    """The intent cannot be applied without risking a corrupt table."""


def _record_identity(j: dict) -> int | None:
    """The stable identity of a stock record: the item id carried in
    value.payload.body (duplicated as value.raw_q)."""
    try:
        return int(j["value"]["payload"]["body"])
    except (KeyError, TypeError, ValueError):
        return None


def _check_new_record_buildable(j: dict, idx: int) -> None:
    v = j.get("value") or {}
    for f in _UNMAPPED_VALUE_FIELDS:
        if v.get(f):
            raise StoreinfoWriteRefused(
                f"new stock record [{idx}] sets value.{f}={v[f]!r}, "
                f"whose binary position is not mapped yet; refusing "
                f"rather than corrupting the table")
    for f in _UNMAPPED_RECORD_FIELDS:
        if j.get(f):
            raise StoreinfoWriteRefused(
                f"new stock record [{idx}] sets {f}={j[f]!r}, whose "
                f"binary position is not mapped yet; refusing")
    if j.get("effect_list"):
        raise StoreinfoWriteRefused(
            f"new stock record [{idx}] has a non-empty effect_list; "
            f"the element layout is not decoded yet")
    # value.raw_e and raw_g ARE mapped but currently only validated
    # against the defaults seen in every ground-truth record; anything
    # else would be a silent guess about semantics, so surface it.
    if v.get("raw_q") is not None and _record_identity(j) != int(v["raw_q"]):
        raise StoreinfoWriteRefused(
            f"new stock record [{idx}]: value.raw_q={v['raw_q']!r} "
            f"differs from value.payload.body; in every ground-truth "
            f"record they are the same value")


def _build_new_record(j: dict, idx: int) -> StockRecord:
    _check_new_record_buildable(j, idx)
    v = j.get("value") or {}
    rec = StockRecord(
        lookup_a=int(j.get("lookup_a") or 0),
        raw_a=int(j.get("raw_a") or 0),
        raw_b=int(j.get("raw_b") or 0),
        raw_c=int(j.get("raw_c") or 0),
        raw_d=int(j.get("raw_d") or 0),
        raw_e=int(j.get("raw_e") or 0),
        flag_a=int(j.get("flag_a") or 0),
        flag_b=int(j.get("flag_b") or 0),
        flag_c=int(j.get("flag_c") or 0),
        const33=1,
        body=int(_record_identity(j) or 0),
    )
    # Mapped interior fields: body u32@34 is in the typed prefix; the
    # vgap carries raw_e@79 (record-rel) = vgap[41], raw_g u16@95 =
    # vgap[57], raw_q u32@97 = vgap[59].
    vgap = bytearray(rec.vgap)
    struct.pack_into("<I", vgap, 79 - 38, int(v.get("raw_e") or 0))
    struct.pack_into("<H", vgap, 95 - 38, int(v.get("raw_g") or 0) & 0xFFFF)
    struct.pack_into("<I", vgap, 97 - 38, int(v.get("raw_q") or 0))
    rec.vgap = bytes(vgap)
    sd = j.get("sub_data")
    if sd is not None:
        rec.sub_data = {
            "flag": int(sd.get("flag") or 0),
            "lookup_a": int(sd.get("lookup_a") or 0) & 0xFFFFFFFF,
            "lookup_b": int(sd.get("lookup_b") or 0) & 0xFFFFFFFF,
            "lookup_c": int(sd.get("lookup_c") or 0) & 0xFFFFFFFF,
        }
    return rec


def build_storeinfo_changes(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: list,
) -> tuple[list[dict], dict | None]:
    """Resolve Format 3 stock_data_list intents into v2 change dicts.

    ``intents`` is a list of Format3Intent-like objects (attributes
    entry/key/field/op/new). Only ``op == 'set'`` with a list value on
    field ``stock_data_list`` is supported.

    Returns ``(pabgb_changes, pabgh_change)`` like the multichangeinfo
    writer: absolute-offset replaces for the .pabgb plus a whole-body
    .pabgh replace when entry offsets shift. Both are produced from
    the same rebuild so they stay mutually consistent.
    """
    key_size, offsets = parse_pabgh_index(vanilla_header, "storeinfo")
    if not offsets:
        logger.warning("storeinfo writer: could not parse pabgh index")
        return [], None
    sorted_offs = sorted(offsets.values()) + [len(vanilla_body)]

    # One rebuild per entry key; later intents on the same key win
    # (matching 'set' semantics).
    per_key: dict[int, list] = {}
    for it in intents:
        field = (getattr(it, "field", "") or "").strip()
        if field not in ("stock_data_list", "_exchangeItemInfoListForSell"):
            logger.warning(
                "storeinfo writer: unsupported field %r, skipping", field)
            continue
        if (getattr(it, "op", "set") or "set") != "set":
            logger.warning(
                "storeinfo writer: unsupported op %r, skipping",
                getattr(it, "op", None))
            continue
        new = getattr(it, "new", None)
        key = getattr(it, "key", None)
        if not isinstance(new, list) or not isinstance(key, int):
            logger.warning(
                "storeinfo writer: malformed intent (key=%r), skipping",
                key)
            continue
        if key not in offsets:
            logger.warning(
                "storeinfo writer: store key %d not in table, skipping",
                key)
            continue
        per_key[key] = new

    if not per_key:
        return [], None

    # Rebuild each targeted entry's list span.
    replacements: dict[int, tuple[int, int, bytes]] = {}
    for key, json_records in per_key.items():
        off = offsets[key]
        entry_end = sorted_offs[sorted_offs.index(off) + 1]
        _, _, payload = _parse_entry_header(vanilla_body, off, key_size)
        count_off = payload + LIST_COUNT_PAYLOAD_OFFSET
        try:
            van_records, list_start, list_end = parse_stock_list(
                vanilla_body, count_off)
        except (StoreinfoParseError, struct.error, IndexError) as e:
            raise StoreinfoWriteRefused(
                f"store entry {key}: vanilla stock list does not match "
                f"the verified layout ({e}); refusing to rewrite it")
        if list_end > entry_end:
            raise StoreinfoWriteRefused(
                f"store entry {key}: parsed list overruns the entry "
                f"boundary; refusing")
        by_body = {}
        for rec in van_records:
            by_body.setdefault(rec.body, rec)
        out_records: list[StockRecord] = []
        n_new = 0
        for idx, j in enumerate(json_records):
            ident = _record_identity(j)
            van = by_body.get(ident) if ident is not None else None
            if van is not None:
                out_records.append(van)
            else:
                out_records.append(_build_new_record(j, idx))
                n_new += 1
        new_list = serialize_stock_list(out_records)
        replacements[key] = (list_start, list_end, new_list)
        logger.info(
            "storeinfo writer: store %d stock list %d -> %d records "
            "(%d new, %+d bytes)",
            key, len(van_records), len(out_records), n_new,
            len(new_list) - (list_end - list_start))

    # Emit pabgb changes (absolute-offset replaces) and compute the
    # cumulative shift each replacement applies to later offsets.
    pabgb_changes: list[dict] = []
    deltas: list[tuple[int, int]] = []  # (vanilla_offset, size_delta)
    for key in sorted(replacements, key=lambda k: replacements[k][0]):
        start, end, blob = replacements[key]
        pabgb_changes.append({
            "offset": start,
            "original": vanilla_body[start:end].hex(),
            "patched": blob.hex(),
            "label": f"store {key}.stock_data_list",
        })
        deltas.append((offsets[key], len(blob) - (end - start)))

    # Rebuild the pabgh: every entry whose offset lies after a grown
    # entry shifts by the accumulated delta.
    def shifted(off: int) -> int:
        s = off
        for at, d in deltas:
            if off > at:
                s += d
        return s

    new_header = bytearray(vanilla_header)
    count = struct.unpack_from("<H", vanilla_header, 0)[0]
    pos = 2
    changed = False
    for _ in range(count):
        ekey = int.from_bytes(
            vanilla_header[pos:pos + key_size], "little")
        eoff = struct.unpack_from("<I", vanilla_header, pos + key_size)[0]
        noff = shifted(eoff)
        if noff != eoff:
            struct.pack_into("<I", new_header, pos + key_size, noff)
            changed = True
        pos += key_size + 4
    pabgh_change = None
    if changed:
        pabgh_change = {
            "offset": 0,
            "original": vanilla_header.hex(),
            "patched": bytes(new_header).hex(),
            "label": "storeinfo.pabgh offset rebuild",
        }
    return pabgb_changes, pabgh_change
