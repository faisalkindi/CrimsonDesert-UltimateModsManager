"""multichangeinfo.pabgb writer for Format 3 mods (GitHub #125).

Refinement Cost Reforged (Nexus 1342) and similar mods ship Format 3.1
intents targeting multichangeinfo.pabgb with field paths of the form
``fixed_material_data_list[N].item_info`` and
``fixed_material_data_list[N].count``.

This module does NOT implement a full 25-field multichangeinfo parser.
It does the minimum to land those two fields safely:

  * Split the table into records using the .pabgh index.
  * Locate the _fixedMaterialDataList array inside a record.
  * Patch item_info / count of an existing element in place, or
    extend the array (append zeroed elements + bump the u16 count)
    when an intent targets an index past the current element count.
  * Reassemble the table and rebuild the .pabgh offsets.

Record framing (verified 2026-05-21 against vanilla 1.07.00):
  u32 key, u32 strlen, name[strlen], 0x00, then 25 fields.

_fixedMaterialDataList framing (verified):
  u16 element_count, then element_count * 30-byte elements.
  Element layout, on-disk order:
    +0  u32 item_info
    +4  u32 character_info
    +8  u32 gimmick_info
    +12 u16 enchant_level
    +14 u64 coupon_count
    +22 u64 count
  Array offset = 8 + strlen + 1 + 53 for ~94% of records; the rest
  have a variable-length field before it and are located by scan.

.pabgh framing (verified): u16 count, then count*(u32 key, u32 off).
Records are stored in ascending-offset order; pabgh order matches.
"""
from __future__ import annotations

import logging
import re
import struct

logger = logging.getLogger(__name__)

_FML_ELEM_SIZE = 30
_FML_ITEM_INFO_OFF = 0    # u32, within element
_FML_COUNT_OFF = 22       # u64, within element
_CONST_PREARRAY = 53      # bytes between the post-name null and the array
_MAX_PLAUSIBLE_COUNT = 256

# Format 3 intent field path: fixed_material_data_list[N].item_info|count
_FIELD_PATH_RE = re.compile(
    r"^fixed_material_data_list\[(\d+)\]\.(item_info|count)$")


def parse_pabgh(pabgh: bytes) -> list[tuple[int, int]]:
    """Return [(key, offset), ...] in pabgh order (== ascending offset)."""
    count = struct.unpack_from("<H", pabgh, 0)[0]
    out: list[tuple[int, int]] = []
    pos = 2
    for _ in range(count):
        key = struct.unpack_from("<I", pabgh, pos)[0]
        off = struct.unpack_from("<I", pabgh, pos + 4)[0]
        out.append((key, off))
        pos += 8
    return out


def build_pabgh(entries: list[tuple[int, int]]) -> bytes:
    """Inverse of parse_pabgh. entries = [(key, offset), ...]."""
    out = bytearray(struct.pack("<H", len(entries)))
    for key, off in entries:
        out += struct.pack("<II", key, off)
    return bytes(out)


def _record_strlen(rec: bytes) -> int:
    """u32 string length of the record's name field."""
    return struct.unpack_from("<I", rec, 4)[0]


def _array_candidate_ok(rec: bytes, array_off: int) -> int | None:
    """If a plausible _fixedMaterialDataList array sits at array_off,
    return its element count, else None.

    Plausible = u16 count below a sane ceiling and the whole array
    (2 + count*30 bytes) fits inside the record.
    """
    if array_off + 2 > len(rec):
        return None
    count = struct.unpack_from("<H", rec, array_off)[0]
    if count > _MAX_PLAUSIBLE_COUNT:
        return None
    if array_off + 2 + count * _FML_ELEM_SIZE > len(rec):
        return None
    return count


def locate_fixed_material_list(rec: bytes) -> tuple[int, int] | None:
    """Return (array_offset, element_count) for the record's
    _fixedMaterialDataList, or None if it cannot be located confidently.

    Uses ONLY the constant offset formula (8 + strlen + 1 + 53), which
    is the schema-derived position and is exact for the ~94% of
    records whose fields before _fixedMaterialDataList are all
    fixed-size. The remaining ~6% have a variable-length field that
    shifts the array; locating it there needs a real field walk.

    A forward scan was tried and rejected: a u16 that happens to equal
    a small int followed by 30 plausible bytes occurs coincidentally
    inside the earlier fixed fields, so a scan mislocates the array
    and would corrupt the record. Returning None for the 6% means the
    caller skips those records (and logs it) instead of patching the
    wrong bytes. Correct-but-partial beats silently-corrupt.
    """
    strlen = _record_strlen(rec)
    formula = 8 + strlen + 1 + _CONST_PREARRAY
    count = _array_candidate_ok(rec, formula)
    if count is not None:
        return formula, count
    return None


def _patch_element_field(
    rec: bytearray, elem_off: int, field: str, value: int
) -> bool:
    """Patch one element field in place. Returns True on success."""
    if field == "item_info":
        struct.pack_into("<I", rec, elem_off + _FML_ITEM_INFO_OFF,
                         value & 0xFFFFFFFF)
        return True
    if field == "count":
        struct.pack_into("<Q", rec, elem_off + _FML_COUNT_OFF,
                         value & 0xFFFFFFFFFFFFFFFF)
        return True
    logger.warning("multichangeinfo: unknown element field %r", field)
    return False


def apply_record_intents(
    rec: bytes, intents: list[tuple[int, str, int]]
) -> bytes | None:
    """Apply fixed_material_data_list intents to one record.

    intents: list of (list_index, field, value). field is
    'item_info' or 'count'.

    Returns the new record bytes (possibly longer, if the array was
    extended), or None if the array could not be located.
    """
    located = locate_fixed_material_list(rec)
    if located is None:
        return None
    array_off, count = located
    work = bytearray(rec)

    max_index = max((i for i, _f, _v in intents), default=-1)
    if max_index >= count:
        # Extend: append (max_index + 1 - count) zeroed 30-byte
        # elements right after the existing array, bump the u16 count.
        new_count = max_index + 1
        insert_at = array_off + 2 + count * _FML_ELEM_SIZE
        pad = bytes(_FML_ELEM_SIZE * (new_count - count))
        work = work[:insert_at] + bytearray(pad) + work[insert_at:]
        struct.pack_into("<H", work, array_off, new_count)
        count = new_count
        logger.info(
            "multichangeinfo: extended _fixedMaterialDataList to %d "
            "elements (record grew %d bytes)",
            new_count, len(pad))

    for list_index, field, value in intents:
        if list_index < 0 or list_index >= count:
            logger.warning(
                "multichangeinfo: intent index %d out of range "
                "(count=%d), skipping", list_index, count)
            continue
        elem_off = array_off + 2 + list_index * _FML_ELEM_SIZE
        _patch_element_field(work, elem_off, field, value)

    return bytes(work)


def apply_multichangeinfo(
    pabgb: bytes,
    pabgh: bytes,
    intents_by_key: dict[int, list[tuple[int, str, int]]],
) -> tuple[bytes, bytes]:
    """Apply Format 3 intents to multichangeinfo.pabgb.

    intents_by_key maps a record key to a list of
    (list_index, field, value) tuples.

    Returns (new_pabgb, new_pabgh). When intents_by_key is empty the
    output is byte-identical to the input (round-trip floor).
    """
    entries = parse_pabgh(pabgh)
    # Records are stored in ascending-offset order; pabgh order matches.
    order = sorted(range(len(entries)), key=lambda i: entries[i][1])
    bounds: list[tuple[int, int, int]] = []  # (key, start, end)
    for rank, idx in enumerate(order):
        key, start = entries[idx]
        end = (entries[order[rank + 1]][1]
               if rank + 1 < len(order) else len(pabgb))
        bounds.append((key, start, end))

    out_records: list[tuple[int, bytes]] = []
    for key, start, end in bounds:
        rec = pabgb[start:end]
        rec_intents = intents_by_key.get(key)
        if rec_intents:
            new_rec = apply_record_intents(rec, rec_intents)
            if new_rec is None:
                logger.warning(
                    "multichangeinfo: could not locate "
                    "_fixedMaterialDataList for key=%d, leaving record "
                    "unmodified (%d intent(s) skipped)",
                    key, len(rec_intents))
                new_rec = rec
            rec = new_rec
        out_records.append((key, rec))

    # Reassemble in the same (ascending-offset) order, recompute
    # offsets, rebuild pabgh keeping the original pabgh key order.
    new_body = bytearray()
    key_to_off: dict[int, int] = {}
    for key, rec in out_records:
        key_to_off[key] = len(new_body)
        new_body += rec
    new_pabgh_entries = [(k, key_to_off[k]) for k, _o in entries]
    return bytes(new_body), build_pabgh(new_pabgh_entries)


def _record_header_len(body: bytes, offset: int) -> int:
    """Byte length of a record's u32-key + u32-strlen + name header."""
    strlen = struct.unpack_from("<I", body, offset + 4)[0]
    return 8 + strlen


def _record_name_at(body: bytes, offset: int) -> str:
    """Decode a record's name string. Empty on failure."""
    strlen = struct.unpack_from("<I", body, offset + 4)[0]
    if offset + 8 + strlen > len(body):
        return ""
    try:
        return body[offset + 8:offset + 8 + strlen].decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _record_bounds(pabgb: bytes, pabgh: bytes) -> dict[int, tuple[int, int]]:
    """Map record key -> (start, end) byte range in pabgb, derived from
    the pabgh index (records are stored in ascending-offset order)."""
    entries = parse_pabgh(pabgh)
    order = sorted(entries, key=lambda kv: kv[1])
    bounds: dict[int, tuple[int, int]] = {}
    for rank, (key, start) in enumerate(order):
        end = order[rank + 1][1] if rank + 1 < len(order) else len(pabgb)
        bounds[key] = (start, end)
    return bounds


def build_multichangeinfo_changes(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: list[tuple[str, int, str, object]],
) -> tuple[list[dict], dict | None]:
    """Resolve Format 3 multichangeinfo intents into v2 change dicts.

    ``intents`` is a list of (entry_name, key, field_path, new_value):
      * entry_name  - the record's name (Format 3 mods locate by name).
      * key         - the numeric record key, or 0 when the mod omits it.
      * field_path  - 'fixed_material_data_list[N].item_info' / '.count'.
      * new_value   - the integer value to set.

    Returns ``(pabgb_changes, pabgh_change)``:
      * pabgb_changes - one v2 change dict per modified record. Each is
        an absolute-offset replace of the record's payload (the bytes
        after the key+strlen+name header). Absolute offsets are used,
        not entry-name anchors, so two records sharing a name cannot
        cross-resolve; the apply pipeline's cumulative-shift handles
        records that grow.
      * pabgh_change - a single offset-0 whole-body replace for the
        companion multichangeinfo.pabgh (its length never changes, only
        the per-record offsets), or None when no record grew.

    The pabgb changes and the pabgh change are produced from one
    ``apply_multichangeinfo`` pass, so they are always mutually
    consistent: the reassembled pabgb the apply pipeline builds from
    the per-record changes is byte-identical to the pabgb the pabgh
    offsets describe.
    """
    name_to_key: dict[str, int] = {}
    v_bounds = _record_bounds(vanilla_body, vanilla_header)
    for key, (start, _end) in v_bounds.items():
        name = _record_name_at(vanilla_body, start)
        if name:
            name_to_key.setdefault(name, key)

    intents_by_key: dict[int, list[tuple[int, str, int]]] = {}
    for entry_name, raw_key, field_path, new_value in intents:
        m = _FIELD_PATH_RE.match(field_path or "")
        if m is None:
            logger.warning(
                "multichangeinfo: intent field %r is not a "
                "fixed_material_data_list path, skipping", field_path)
            continue
        if isinstance(new_value, bool) or not isinstance(new_value, int):
            logger.warning(
                "multichangeinfo: intent %s on %r has non-integer "
                "new value %r, skipping",
                field_path, entry_name, new_value)
            continue
        key = name_to_key.get(entry_name)
        if key is None and raw_key:
            key = raw_key
        if key is None or key not in v_bounds:
            logger.warning(
                "multichangeinfo: intent entry %r (key=%r) not found "
                "in table, skipping", entry_name, raw_key)
            continue
        intents_by_key.setdefault(key, []).append(
            (int(m.group(1)), m.group(2), new_value))

    if not intents_by_key:
        return [], None

    new_pabgb, new_pabgh = apply_multichangeinfo(
        vanilla_body, vanilla_header, intents_by_key)

    # Safety guard for the rare end-of-file growth case. The apply
    # pipeline's replace path rejects a change whose patched bytes
    # would extend past the current buffer end, so a record that grows
    # by more than the total bytes that follow it (only possible for
    # the final record, or one near it) cannot be delivered as a
    # replace. Drop those keys and re-run so the pabgb changes and the
    # pabgh stay consistent with each other. In practice this never
    # fires - refinement recipes sit far from the table tail.
    n_bounds = _record_bounds(new_pabgb, new_pabgh)
    unsafe: list[int] = []
    for key in intents_by_key:
        v_start, v_end = v_bounds[key]
        n_start, n_end = n_bounds[key]
        growth = (n_end - n_start) - (v_end - v_start)
        if growth > 0 and growth > (len(vanilla_body) - v_end):
            unsafe.append(key)
    if unsafe:
        logger.error(
            "multichangeinfo: %d record(s) grow past the table tail "
            "and cannot be applied safely (keys=%s); dropping them and "
            "re-running so the pabgb/pabgh pair stays consistent",
            len(unsafe), unsafe)
        for key in unsafe:
            intents_by_key.pop(key, None)
        if not intents_by_key:
            return [], None
        new_pabgb, new_pabgh = apply_multichangeinfo(
            vanilla_body, vanilla_header, intents_by_key)
        n_bounds = _record_bounds(new_pabgb, new_pabgh)

    pabgb_changes: list[dict] = []
    for key in intents_by_key:
        v_start, v_end = v_bounds[key]
        n_start, n_end = n_bounds[key]
        v_rec = vanilla_body[v_start:v_end]
        n_rec = new_pabgb[n_start:n_end]
        if v_rec == n_rec:
            # Writer could not locate the array, or every intent was a
            # no-op. Either way there is nothing to patch for this key.
            continue
        header_len = _record_header_len(vanilla_body, v_start)
        if v_rec[:header_len] != n_rec[:header_len]:
            logger.error(
                "multichangeinfo: record key=%d header changed during "
                "write (writer bug), skipping this record", key)
            continue
        name = _record_name_at(vanilla_body, v_start)
        pabgb_changes.append({
            "offset": v_start + header_len,
            "original": v_rec[header_len:].hex(),
            "patched": n_rec[header_len:].hex(),
            "label": f"{name or key}.fixed_material_data_list",
        })

    pabgh_change: dict | None = None
    if new_pabgh != vanilla_header:
        pabgh_change = {
            "offset": 0,
            "original": vanilla_header.hex(),
            "patched": new_pabgh.hex(),
            "label": "multichangeinfo.pabgh offset rebuild",
        }
    return pabgb_changes, pabgh_change
