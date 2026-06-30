"""stringinfo.pabgb writer for Format 3 mods (GitHub #224).

Female Armor Module and similar character-creator supplement mods ship
Format 3 intents targeting stringinfo.pabgb with a variable-length
``_buffer`` string field, e.g.

  {"field": "buffer", "key": 2253925176,
   "new": "khione1_cd_phw_00_ub_00_0205_u", "op": "set"}

The string is located by numeric key, the new value replaces the
record's length-prefixed buffer, and the record changes length, so the
companion stringinfo.pabgh offsets must be rebuilt. Same two-file
contract as multichangeinfo / storeinfo.

Record framing (verified 2026-06-30 against vanilla build 23831243,
30,940 records round-trip byte-exact):
  +0  u32 _key
  +4  u8  _isBlocked
  +5  u32 _stringKey   (0 in every vanilla record)
  +9  u32 _buffer length
  +13 _buffer bytes    (UTF-8, no null terminator)
  record length = 13 + buffer_length

.pabgh framing (verified): u16 count, then count*(u32 key, u32 off).
Records are stored in ascending-offset order; pabgh order matches.
"""
from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

_HEADER_LEN = 9          # key(4) + isBlocked(1) + stringKey(4)
_BUFLEN_OFF = 9          # u32 buffer length sits here, then the bytes
_MAX_PLAUSIBLE_BUFLEN = 1 << 20  # 1 MiB sanity ceiling on a single string


class StringinfoWriteRefused(Exception):
    """Raised when the writer cannot safely produce a change set."""


def parse_pabgh(pabgh: bytes) -> list[tuple[int, int]]:
    """Return [(key, offset), ...] in pabgh order (== ascending offset)."""
    count = struct.unpack_from("<H", pabgh, 0)[0]
    out: list[tuple[int, int]] = []
    pos = 2
    for _ in range(count):
        if pos + 8 > len(pabgh):
            break
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


def _buffer_bytes_at(rec: bytes) -> bytes | None:
    """Return the raw _buffer bytes of a record, or None if the record
    is too short / its length prefix is implausible (foreign layout)."""
    if len(rec) < _HEADER_LEN + 4:
        return None
    blen = struct.unpack_from("<I", rec, _BUFLEN_OFF)[0]
    if blen > _MAX_PLAUSIBLE_BUFLEN:
        return None
    if _HEADER_LEN + 4 + blen != len(rec):
        # The record's declared buffer length must consume the rest of
        # the record exactly (buffer is the trailing field). If not, the
        # layout assumption is wrong for this file and we must not patch.
        return None
    return rec[_HEADER_LEN + 4:_HEADER_LEN + 4 + blen]


def _rebuild_record(rec: bytes, new_buffer: bytes) -> bytes:
    """Return the record with its _buffer replaced, header preserved."""
    return rec[:_HEADER_LEN] + struct.pack("<I", len(new_buffer)) + new_buffer


def apply_stringinfo(
    pabgb: bytes,
    pabgh: bytes,
    buffers_by_key: dict[int, bytes],
) -> tuple[bytes, bytes]:
    """Apply Format 3 _buffer replacements to stringinfo.pabgb.

    buffers_by_key maps a record key to the new UTF-8 buffer bytes.

    Returns (new_pabgb, new_pabgh). When buffers_by_key is empty the
    output is byte-identical to the input (round-trip floor).
    """
    entries = parse_pabgh(pabgh)
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
        if key in buffers_by_key:
            cur = _buffer_bytes_at(rec)
            if cur is None:
                logger.warning(
                    "stringinfo: record key=%d does not match the "
                    "length-prefixed buffer layout, leaving unmodified",
                    key)
            else:
                rec = _rebuild_record(rec, buffers_by_key[key])
        out_records.append((key, rec))

    new_body = bytearray()
    key_to_off: dict[int, int] = {}
    for key, rec in out_records:
        key_to_off[key] = len(new_body)
        new_body += rec
    new_pabgh_entries = [(k, key_to_off[k]) for k, _o in entries]
    return bytes(new_body), build_pabgh(new_pabgh_entries)


def _coerce_buffer(new_value: object) -> bytes | None:
    """Encode a Format 3 ``new`` value to _buffer bytes, or None if it
    is not a writable string."""
    if isinstance(new_value, str):
        return new_value.encode("utf-8")
    if isinstance(new_value, (bytes, bytearray)):
        return bytes(new_value)
    return None


def build_stringinfo_changes(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: list[tuple[str, int, str, object]],
) -> tuple[list[dict], dict | None]:
    """Resolve Format 3 stringinfo intents into v2 change dicts.

    ``intents`` is a list of (entry_name, key, field, new_value). Only
    the ``_buffer`` / ``buffer`` field is handled; the value must be a
    string. The record is located by numeric key (stringinfo has no
    name index).

    Returns ``(pabgb_changes, pabgh_change)`` with the same contract as
    build_multichangeinfo_changes:
      * pabgb_changes - one absolute-offset replace per modified record,
        covering the buffer-length + buffer bytes (the record header is
        preserved). The apply pipeline's cumulative shift handles
        records that change length.
      * pabgh_change - a single offset-0 whole-body replace for the
        companion stringinfo.pabgh, or None when no record changed
        length AND no offset moved.
    """
    v_bounds = _record_bounds(vanilla_body, vanilla_header)

    buffers_by_key: dict[int, bytes] = {}
    for entry_name, raw_key, field, new_value in intents:
        fname = (field or "").strip().lstrip("_").lower()
        if fname != "buffer":
            logger.warning(
                "stringinfo: intent field %r is not a buffer write, "
                "skipping", field)
            continue
        buf = _coerce_buffer(new_value)
        if buf is None:
            logger.warning(
                "stringinfo: intent on key=%r has non-string new value "
                "%r, skipping", raw_key, new_value)
            continue
        key = raw_key
        if key is None or key not in v_bounds:
            logger.warning(
                "stringinfo: intent entry %r (key=%r) not found in "
                "table, skipping", entry_name, raw_key)
            continue
        # Last intent wins if a mod sets the same key twice.
        buffers_by_key[key] = buf

    if not buffers_by_key:
        return [], None

    new_pabgb, new_pabgh = apply_stringinfo(
        vanilla_body, vanilla_header, buffers_by_key)

    # End-of-table growth guard, mirroring multichangeinfo: a record
    # that grows by more than the bytes following it cannot be delivered
    # as an absolute-offset replace. Drop those keys and re-run so the
    # pabgb changes and the pabgh stay mutually consistent.
    n_bounds = _record_bounds(new_pabgb, new_pabgh)
    unsafe: list[int] = []
    for key in buffers_by_key:
        v_start, v_end = v_bounds[key]
        n_start, n_end = n_bounds[key]
        growth = (n_end - n_start) - (v_end - v_start)
        if growth > 0 and growth > (len(vanilla_body) - v_end):
            unsafe.append(key)
    if unsafe:
        logger.error(
            "stringinfo: %d record(s) grow past the table tail and "
            "cannot be applied safely (keys=%s); dropping them",
            len(unsafe), unsafe)
        for key in unsafe:
            buffers_by_key.pop(key, None)
        if not buffers_by_key:
            return [], None
        new_pabgb, new_pabgh = apply_stringinfo(
            vanilla_body, vanilla_header, buffers_by_key)
        n_bounds = _record_bounds(new_pabgb, new_pabgh)

    pabgb_changes: list[dict] = []
    for key in buffers_by_key:
        v_start, v_end = v_bounds[key]
        n_start, n_end = n_bounds[key]
        v_rec = vanilla_body[v_start:v_end]
        n_rec = new_pabgb[n_start:n_end]
        if v_rec == n_rec:
            continue
        if v_rec[:_HEADER_LEN] != n_rec[:_HEADER_LEN]:
            logger.error(
                "stringinfo: record key=%d header changed during write "
                "(writer bug), skipping this record", key)
            continue
        pabgb_changes.append({
            "offset": v_start + _HEADER_LEN,
            "original": v_rec[_HEADER_LEN:].hex(),
            "patched": n_rec[_HEADER_LEN:].hex(),
            "label": f"stringinfo[{key}]._buffer",
        })

    pabgh_change: dict | None = None
    if new_pabgh != vanilla_header:
        pabgh_change = {
            "offset": 0,
            "original": vanilla_header.hex(),
            "patched": new_pabgh.hex(),
            "label": "stringinfo.pabgh offset rebuild",
        }
    return pabgb_changes, pabgh_change
