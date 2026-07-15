"""statusinfo.pabgb ``stat_level_data`` writer (DIRECT SPEED stat mods).

The always-active stat presets on Nexus (DIRECT MOVEMENT SPEED, DIRECT
ATTACK SPEED, ...) ship as ``.cdmod`` packages whose ``semantic.json`` sets
``stat_level_data[0..15]`` on ``statusinfo.pabgb``. ``cdmod_handler`` turns
each operation into a Format 3 intent::

    {key: 1000011, field: "stat_level_data[3]", op: "set", new: 2500000000}

This writer applies those element writes byte-exact.

Layout (reverse-engineered from the real 1.13 table and verified against
all four rate records -- MoveSpeedRate, AttackSpeedRate, CriticalRate,
DHIT)::

    record = <u32 key><u32 name_len><name : name_len bytes><tail>

Only the four "rate" stats carry a 212-byte tail; the other 71 stats have
an 84-byte tail with NO ``stat_level_data``. Inside a 212-byte tail::

    tail[0  : 80 ]   80-byte header
    tail[80 : 208]   stat_level_data : 16 * int64 (128 bytes)
    tail[208: 212]   4-byte trailer

Every rate record shows a clean non-decreasing per-level ramp in the low
32 bits of those 16 elements (e.g. MoveSpeedRate 78125, 156250, ... 976562),
which is what pins the array to tail offset 80. ``set stat_level_data[i] = V``
writes ``V`` as a little-endian int64 at ``tail_off + 80 + 8*i``. The write
is length-preserving, so the whole table (and its companion .pabgh offsets)
stays byte-identical everywhere the mod did not touch.

Guardrail (the project's never-corrupt bar): a ``stat_level_data`` intent
aimed at a record that is NOT a 212-byte rate record is refused, never
written -- a regular stat has no such array and writing into its tail would
corrupt it.
"""
from __future__ import annotations

import logging
import re
import struct

from cdumm.semantic.parser import parse_pabgh_index

logger = logging.getLogger(__name__)

_ENVELOPE = 8            # u32 key + u32 name_len
_RATE_TAIL_LEN = 212     # only rate stats carry stat_level_data
_SLD_TAIL_OFFSET = 80    # stat_level_data starts here inside the tail
_SLD_COUNT = 16          # 16 per-level elements
_SLD_ELEM = 8            # each element is an int64

_FIELD_RE = re.compile(r"^stat_level_data\[(\d+)\]$")


def _record_bounds(offsets: dict, starts: list[int], key: int, body_len: int):
    """Return (start, end) byte bounds of record ``key`` or None."""
    o = offsets.get(key)
    if o is None:
        return None
    idx = starts.index(o)
    end = starts[idx + 1] if idx + 1 < len(starts) else body_len
    return o, end


def _pack_i64(val: int) -> bytes | None:
    """Little-endian 8-byte encoding of ``val`` (the .cdmod sets the whole
    element to a plain integer). Accepts the full signed/unsigned 64-bit
    range; returns None if it does not fit."""
    if 0 <= val < 2 ** 64:
        return struct.pack("<Q", val)
    if -(2 ** 63) <= val < 0:
        return struct.pack("<q", val)
    return None


def build_statusinfo_changes(
    vanilla_body: bytes, vanilla_header: bytes, intents: list
) -> tuple[list[dict], list[tuple[object, str]]]:
    """Apply ``stat_level_data[i]`` set intents to statusinfo rate records.

    Returns ``(changes, dropped)`` where ``changes`` is a list of
    ``{offset, original, patched}`` byte-change dicts (offsets absolute in
    the .pabgb body, one per touched record) and ``dropped`` is a list of
    ``(intent, reason)`` for intents that could not be applied. No .pabgh
    companion is emitted: the writes are length-preserving.
    """
    dropped: list[tuple[object, str]] = []
    try:
        _, offsets = parse_pabgh_index(vanilla_header, "statusinfo")
    except Exception as e:  # noqa: BLE001 -- never crash the whole apply
        logger.error("statusinfo writer: header unreadable: %s", e)
        return [], [(i, f"statusinfo header unreadable: {e}") for i in intents]
    starts = sorted(offsets.values())
    body_len = len(vanilla_body)

    # Group the element writes per record key.
    by_key: dict[int, list[tuple[int, int, object]]] = {}
    for i in intents:
        field = getattr(i, "field", "") or ""
        m = _FIELD_RE.match(field)
        if m is None:
            dropped.append((i, f"field {field!r} is not stat_level_data[N]"))
            continue
        op = getattr(i, "op", "set") or "set"
        if op != "set":
            dropped.append((i, f"op {op!r} not supported for stat_level_data "
                               f"(only 'set')"))
            continue
        raw_key = getattr(i, "key", None)
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            dropped.append((i, f"record key {raw_key!r} is not an integer"))
            continue
        idx = int(m.group(1))
        if not 0 <= idx < _SLD_COUNT:
            dropped.append((i, f"stat_level_data index {idx} out of range "
                               f"0..{_SLD_COUNT - 1}"))
            continue
        packed = _pack_i64(getattr(i, "new", None)) \
            if isinstance(getattr(i, "new", None), int) else None
        if packed is None:
            dropped.append((i, f"value {getattr(i, 'new', None)!r} does not "
                               f"fit a 64-bit stat_level_data element"))
            continue
        by_key.setdefault(key, []).append((idx, packed, i))

    changes: list[dict] = []
    for key, writes in by_key.items():
        bounds = _record_bounds(offsets, starts, key, body_len)
        if bounds is None:
            for _, _, i in writes:
                dropped.append((i, f"statusinfo has no record with key {key}"))
            continue
        start, end = bounds
        rec = vanilla_body[start:end]
        if len(rec) < _ENVELOPE:
            for _, _, i in writes:
                dropped.append((i, f"record key {key} is truncated"))
            continue
        name_len = struct.unpack_from("<I", rec, 4)[0]
        tail_start = _ENVELOPE + name_len
        tail = rec[tail_start:]
        # GUARD: only 212-byte rate records carry stat_level_data. A regular
        # stat (84-byte tail) has no such array -- refuse, never write.
        if len(tail) != _RATE_TAIL_LEN:
            for _, _, i in writes:
                dropped.append((i, f"record key {key} is not a rate stat "
                                   f"(tail {len(tail)}B, expected "
                                   f"{_RATE_TAIL_LEN}B) -- it has no "
                                   f"stat_level_data"))
            continue
        new_rec = bytearray(rec)
        blk = tail_start + _SLD_TAIL_OFFSET
        for idx, packed, _ in writes:
            new_rec[blk + idx * _SLD_ELEM: blk + idx * _SLD_ELEM + _SLD_ELEM] = \
                packed
        if bytes(new_rec) == rec:
            continue  # every write matched the vanilla bytes (no-op)
        # Emit one change covering the whole 128-byte stat_level_data block:
        # original-anchored, so untouched elements are preserved verbatim.
        span = _SLD_COUNT * _SLD_ELEM
        changes.append({
            "offset": start + blk,
            "original": rec[blk: blk + span].hex(),
            "patched": bytes(new_rec)[blk: blk + span].hex(),
        })
    return changes, dropped
