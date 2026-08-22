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
  ``value.payload.body``, which doubles as ``raw_q``) take the mod's
  per-slot fields (quantity, flags, restock/sort order -- everything
  the head maps), falling back to the vanilla value for any field the
  mod's JSON omits (GitHub #365 residual: a matched record used to keep
  100% vanilla bytes, so a mod bumping the stock of an item a store
  already carried had no effect). The value-struct interior (``vgap``)
  and ``effect_list`` stay vanilla unconditionally: interior diffs
  observed in real mods are stale-export noise from older game
  versions.
* NEW records are built from the pinned fields plus sub_data, with the
  unmapped value interior zeroed. If a new record carries a NON-zero
  value in any unmapped interior field, the whole intent is refused,
  we cannot place the value, and a wrong placement corrupts the table
  (the game crashes on store open).
* A new record's non-empty ``effect_list`` refuses (element layout not
  decoded); a matched record's carries over from vanilla verbatim.
"""
from __future__ import annotations

import logging
import struct

from cdumm.engine.storeinfo_native_parser import (
    StockRecord,
    StoreinfoParseError,
    StoreLayout,
    StoreListNotFound,
    detect_storeinfo_layout,
    locate_stock_list,
    serialize_stock_list,
)
from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index

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
    if v.get("raw_q") is not None:
        try:
            raw_q = int(v["raw_q"])
        except (TypeError, ValueError):
            raise StoreinfoWriteRefused(
                f"new stock record [{idx}]: value.raw_q={v['raw_q']!r} "
                f"is not an integer")
        if _record_identity(j) != raw_q:
            raise StoreinfoWriteRefused(
                f"new stock record [{idx}]: value.raw_q={v['raw_q']!r} "
                f"differs from value.payload.body; in every "
                f"ground-truth record they are the same value")


def _build_matched_record(j: dict, van: StockRecord) -> StockRecord:
    """A mod record whose item id matches an existing vanilla stock
    entry at this store.

    Applies the mod's per-slot fields (quantity, flags, restock/sort
    order) -- they are exactly as mapped as they are for a brand-new
    record, and a Format 3 'set' intent means them as an authoritative
    replacement, not noise. A field the mod's JSON omits falls back to
    the vanilla value instead of a hardcoded default, since a vanilla
    record to fall back to actually exists here (unlike the new-record
    case).

    Keeps vanilla's value-struct interior (``vgap``) and ``effect_list``
    verbatim rather than trusting the mod's ``value.*`` -- this is the
    #183 protection, unchanged: interior diffs seen in real mods are
    stale-export noise from an older layout, and the interior encodes
    the ITEM's own definition (price/type), which does not vary by
    which store sells it.
    """
    def field(name: str) -> int:
        v = j.get(name)
        return int(v) if v is not None else getattr(van, name)

    order_index = j.get("order_index_113", j.get("order_index"))
    order_index = (int(order_index) & 0xFFFFFFFF if order_index is not None
                   else van.order_index)
    low_thr = j.get("low_price_threshold_count")
    low_thr = (int(low_thr) & 0xFFFFFFFF if low_thr is not None
               else van.low_price_threshold_count)

    return StockRecord(
        lookup_a=field("lookup_a"),
        raw_a=field("raw_a"),
        raw_b=field("raw_b"),
        raw_c=field("raw_c"),
        low_price_threshold_count=low_thr,
        raw_d=field("raw_d"),
        raw_e=field("raw_e"),
        order_index=order_index,
        flag_a=field("flag_a"),
        flag_b=field("flag_b"),
        flag_c=field("flag_c"),
        is_restore_item=field("is_restore_item"),
        const33=van.const33,
        body=van.body,
        vgap=van.vgap,
        sub_data=van.sub_data,
        effect_list=van.effect_list,
    )


def _build_new_record(j: dict, idx: int,
                      layout: StoreLayout) -> StockRecord:
    _check_new_record_buildable(j, idx)
    # The three fields mapped below live INSIDE the opaque interior at
    # fixed indices. On a build whose interior CDUMM has not re-derived
    # them for, writing there lands in the wrong bytes, so refuse the
    # record rather than write a plausible-looking wrong one.
    #
    # Editing an EXISTING record is unaffected: nothing else touches the
    # interior, it is carried through verbatim. (GitHub #365.)
    if not layout.vgap_map_verified:
        raise StoreinfoWriteRefused(
            f"new stock record [{idx}]: the {layout.label} value "
            f"interior has not been re-derived, so raw_e / raw_g / raw_q "
            f"would be written at indices measured on an older build. "
            f"Editing existing stock records still works on this game "
            f"version; only ADDING one is refused")
    v = j.get("value") or {}
    rec = StockRecord(
        lookup_a=int(j.get("lookup_a") or 0),
        raw_a=int(j.get("raw_a") or 0),
        raw_b=int(j.get("raw_b") or 0),
        raw_c=int(j.get("raw_c") or 0),
        raw_d=int(j.get("raw_d") or 0),
        raw_e=int(j.get("raw_e") or 0),
        # CD 1.13's u32 @30. Mods that know about it ship it as
        # `order_index_113` (donr484's Shop Smart names it exactly that);
        # 0xFFFFFFFF is its value in all 3661 vanilla records, so that is
        # the default for a mod written before the field existed. Ignored
        # entirely on pre-1.13 layouts, which have no such field.
        order_index=int(
            j.get("order_index_113",
                  j.get("order_index", 0xFFFFFFFF)) or 0) & 0xFFFFFFFF,
        # CD 1.16's u32, same reasoning as order_index above. 0xFFFFFFFF
        # is the unset value, holding on 5,654 of the 6,376 vanilla 1.16
        # records; the other 22 distinct values are real thresholds (150,
        # 20, 50, 30, ...). Zero occurs EXACTLY ZERO times, so taking the
        # dataclass default of 0 would write every new record out of
        # distribution -- a value the game never ships for this field.
        # Ignored on layouts that have no such field.
        low_price_threshold_count=int(
            j.get("low_price_threshold_count", 0xFFFFFFFF) or 0
        ) & 0xFFFFFFFF,
        flag_a=int(j.get("flag_a") or 0),
        flag_b=int(j.get("flag_b") or 0),
        flag_c=int(j.get("flag_c") or 0),
        is_restore_item=int(j.get("is_restore_item") or 0),
        const33=1,
        body=int(_record_identity(j) or 0),
    )
    # Mapped interior fields live inside the opaque value-struct blob
    # (vgap), at the layout's raw_e_off / raw_g_off / raw_q_off. These
    # stayed at 41/57/59 across the CD 1.11 layout shift (is_restore_item
    # was inserted before the vgap, so the blob's contents and internal
    # offsets did not move, only the vgap's record-relative start did,
    # 38 -> 39) but moved to 37/53/55 on CD 1.16.1 (GitHub #365) -- see
    # StoreLayout.raw_e_off for how that was derived.
    #
    # Sized from the layout, not from the StockRecord dataclass default
    # (71, the pre-1.16.1 VGAP_SIZE): that default only ever matched by
    # coincidence, because every layout before 1.16.1 also happened to be
    # 71 bytes. On a shorter interior it silently built an oversized
    # vgap that write_stock_record then refused outright.
    vgap = bytearray(layout.vgap_size)
    struct.pack_into("<I", vgap, layout.raw_e_off, int(v.get("raw_e") or 0))
    struct.pack_into("<H", vgap, layout.raw_g_off,
                     int(v.get("raw_g") or 0) & 0xFFFF)
    struct.pack_into("<I", vgap, layout.raw_q_off, int(v.get("raw_q") or 0))
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

    # Format 3 dialect contract: "lookup by entry name first, key as
    # fallback". Key-omitted intents arrive with the sentinel key=0;
    # resolve through the entry name parsed from the table when the
    # numeric key misses (mirrors the multichangeinfo writer).
    name_to_key: dict[str, int] = {}
    for k, off in offsets.items():
        _eid, ename, _payload = _parse_entry_header(
            vanilla_body, off, key_size)
        if ename:
            name_to_key.setdefault(ename, k)

    # One rebuild per entry key; later intents on the same key win
    # (matching 'set' semantics).
    per_key: dict[int, list] = {}
    name_resolved = 0
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
            entry_name = getattr(it, "entry", "") or ""
            resolved = name_to_key.get(entry_name)
            if resolved is not None:
                logger.debug(
                    "storeinfo writer: intent key %r missed, resolved "
                    "by entry name %r (key=%d)",
                    key, entry_name, resolved)
                key = resolved
                name_resolved += 1
            else:
                logger.warning(
                    "storeinfo writer: store key %d / entry %r not in "
                    "table, skipping", key, entry_name)
                continue
        per_key[key] = new

    if name_resolved:
        logger.info(
            "storeinfo writer: %d intent(s) resolved by entry name "
            "(key missing or not in table)", name_resolved)
    if not per_key:
        return [], None

    # Detect the record shape from the table in front of us rather than
    # assuming one. The layout has moved twice -- CD 1.11 inserted
    # is_restore_item, CD 1.13 inserted order_index_113 -- and each time
    # every store mod silently stopped applying until the constants were
    # hand-edited. Detection makes the next shift a fixture, not an outage.
    #
    # Done here, after we know there is work: a mod that targets no store
    # in this table should skip cleanly, not trip layout detection.
    #
    # A table in no shape we know raises, and is re-raised as a refusal so
    # the caller treats it like any other per-mod skip. Refusing is the
    # right outcome: storeinfo has no integrity check, and a misread record
    # written back crashes the game on store open.
    try:
        layout: StoreLayout = detect_storeinfo_layout(
            vanilla_body, sorted(offsets.values()))
    except StoreinfoParseError as e:
        raise StoreinfoWriteRefused(f"storeinfo: {e}") from e

    # Rebuild each targeted entry's list span.
    replacements: dict[int, tuple[int, int, bytes]] = {}
    for key, json_records in per_key.items():
        off = offsets[key]
        entry_end = sorted_offs[sorted_offs.index(off) + 1]
        _, _, payload = _parse_entry_header(vanilla_body, off, key_size)
        # Locate the list rather than computing its offset. A single
        # constant was wrong for 71 of the 1.13 table's stocked stores:
        # it read a different u32, found 0, and reported "empty" -- so a
        # mod targeting one of them would have spliced a new list into
        # the middle of live fields.
        try:
            van_records, list_start, list_end = locate_stock_list(
                vanilla_body, payload, entry_end, key, layout)
        except StoreListNotFound as e:
            raise StoreinfoWriteRefused(
                f"store entry {key}: {e}" + (
                    "; adding stock to a store that has none is not "
                    "supported (the empty list's position cannot be "
                    "pinned)" if e.provably_empty else ""))
        except (StoreinfoParseError, struct.error, IndexError) as e:
            raise StoreinfoWriteRefused(
                f"store entry {key}: vanilla stock list does not match "
                f"the detected {layout.label} layout ({e}); refusing to "
                f"rewrite it")
        # Round-trip identity (audit 2026-06-11, the iteminfo/skill
        # writers gate on an explicit identity serialize): no separate
        # pre-flight is needed here because parse_stock_list and
        # serialize_stock_list are structurally inverse. Every field
        # the reader consumes (typed head, opaque vgap blob, optional
        # sub_data, the always-zero effect_list count) is written back
        # verbatim by write_stock_record, and any byte pattern outside
        # the verified disc-0 shape raises StoreinfoParseError above
        # (caught as a refusal) instead of parsing lossily. A matched
        # record whose mod JSON reproduces vanilla's head fields
        # therefore round-trips byte-exact too (see
        # _build_matched_record for the fields that always stay
        # vanilla); tests/test_storeinfo_writer.py pins this on the
        # real table.
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
                out_records.append(_build_matched_record(j, van))
            else:
                out_records.append(
                    _build_new_record(j, idx, layout))
                n_new += 1
        new_list = serialize_stock_list(out_records, layout)
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
        if vanilla_body[start:end] == blob:
            continue  # no-op set (mod matches vanilla exactly)
        pabgb_changes.append({
            "offset": start,
            "original": vanilla_body[start:end].hex(),
            "patched": blob.hex(),
            "label": f"store {key}.stock_data_list",
        })
        deltas.append((offsets[key], len(blob) - (end - start)))

    if not pabgb_changes:
        return [], None

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
