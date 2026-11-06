"""statusinfo.pabgb ``stat_level_data`` writer (DIRECT SPEED stat mods).

The always-active stat presets on Nexus (DIRECT MOVEMENT SPEED, DIRECT
ATTACK SPEED, ...) ship as ``.cdmod`` packages whose ``semantic.json`` sets
``stat_level_data[0..15]`` on ``statusinfo.pabgb``. ``cdmod_handler`` turns
each operation into a Format 3 intent::

    {key: 1000010, field: "stat_level_data[1]", op: "set", new: 58000000}

This writer applies those element writes byte-exact.

Layout
------
::

    record = <u32 key><u32 name_len><name : name_len bytes><tail>

Only the four "rate" stats -- MoveSpeedRate, AttackSpeedRate, CriticalRate
and DHIT -- carry a 212-byte tail. The other 71 stats have an 84-byte tail
with no ``stat_level_data`` at all. Inside a 212-byte tail::

    tail[  0 :  68]   68-byte header
    tail[ 68 : 196]   stat_level_data : 16 * uint64  (128 bytes)
    tail[196 : 204]   zero terminator slot
    tail[204 : 212]   8-byte constant, byte-identical on all four records

Values are fixed point with **24 fractional bits**: the integer a mod asks
for is stored as ``value << 24``. Vanilla decodes to round numbers under
that rule -- DHIT runs 0, 2000, 4000 ... 25000 and AttackSpeedRate runs
0, 29000000, 55000000 ... 250000000 -- and nothing else about the field is
inferred.

How the offset and the scale were established
---------------------------------------------
Not by reading the ramp. Mod 2511 publishes, for the same game version
(1.13.0), both the JSON V3 field mod and a PAZ overlay carrying the
already-modded table, so the request and its result ship side by side.

Searching every offset that partitions the 212-byte tail into whole
uint64s, and keeping only those that contain every byte the overlay
actually changed, leaves exactly two candidates: 68 and 76. At 68 all 144
written elements satisfy ``raw == asked << 24``; at 76 none of them do.
Feeding the mod's own intents through that rule reproduces the overlay
**byte for byte, with zero residual differences, on all six presets**
(2x/3x/4x AtkSpd, each alone and paired with 2xMovSpd).

This is the third offset tried. 80 shipped first and was wrong -- it ran
the array off the end into the constant at 204, which is why that trailer
looked like data. 76 was the natural correction and is also wrong: it
partitions the tail just as exactly, so no boundary argument separates the
two. Only the overlay does.

Guardrails (the project's never-corrupt bar)
--------------------------------------------
* A record whose tail is not 212 bytes is refused, never written -- a
  regular stat has no ``stat_level_data`` and writing into its tail would
  corrupt it.
* An index outside 0..15 is refused rather than reaching the terminator or
  the trailer.
* A value that does not fit the field once shifted is refused rather than
  silently wrapping.

Every refusal is returned to the caller and surfaced to the user; nothing
is dropped in silence. The writes are length-preserving, so the table and
its companion ``.pabgh`` offsets stay byte-identical everywhere the mod did
not touch.
"""
from __future__ import annotations

import logging
import re
import struct

from cdumm.semantic.parser import parse_pabgh_index
from cdumm.semantic.record_header import is_name_first, name_first_refusal

logger = logging.getLogger(__name__)

_ENVELOPE = 8            # u32 key + u32 name_len
_RATE_TAIL_LEN = 212     # only rate stats carry stat_level_data
_SLD_TAIL_OFFSET = 68    # stat_level_data starts here inside the tail
_SLD_COUNT = 16          # 16 per-level elements; slot 16 is a terminator
_SLD_ELEM = 8            # each element is a uint64
_FRACTION_BITS = 24      # fixed point: stored value is (asked << 24)

_FIELD_RE = re.compile(r"^stat_level_data\[(\d+)\]$")


def _record_bounds(offsets: dict, starts: list[int], key: int, body_len: int):
    """Return (start, end) byte bounds of record ``key`` or None."""
    o = offsets.get(key)
    if o is None:
        return None
    idx = starts.index(o)
    end = starts[idx + 1] if idx + 1 < len(starts) else body_len
    return o, end


def _encode(value: object) -> bytes | None:
    """Little-endian uint64 for ``value`` in 24-bit fixed point.

    Returns None when the value cannot be represented exactly, so the
    caller refuses the intent instead of writing a wrapped or rounded
    number. ``bool`` is rejected explicitly: it is an ``int`` subclass, so
    ``True`` would otherwise be written as 1.
    """
    if type(value) is not int:
        return None
    raw = value << _FRACTION_BITS
    if not 0 <= raw < 2 ** 64:
        return None
    return struct.pack("<Q", raw)


def decode_element(raw: int) -> float:
    """The value a ``stat_level_data`` element holds, from its raw uint64.

    Exposed so tests and diagnostics read the field the way the game does
    rather than re-deriving the shift.
    """
    return raw / (1 << _FRACTION_BITS)


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
    # Game 2.04's name-first records are refused outright: this writer only
    # knows the key-first layout (#493).
    if is_name_first(vanilla_body, vanilla_header, "statusinfo"):
        why = name_first_refusal("statusinfo.pabgb")
        return [], [(i, why) for i in intents]
    dropped: list[tuple[object, str]] = []
    try:
        _, offsets = parse_pabgh_index(vanilla_header, "statusinfo")
    except Exception as e:  # noqa: BLE001 -- never crash the whole apply
        logger.error("statusinfo writer: header unreadable: %s", e)
        return [], [(i, f"statusinfo header unreadable: {e}") for i in intents]
    starts = sorted(offsets.values())
    body_len = len(vanilla_body)

    # Group the element writes per record key.
    by_key: dict[int, list[tuple[int, bytes, object]]] = {}
    for i in intents:
        field = getattr(i, "field", "") or ""
        m = _FIELD_RE.match(field)
        if m is None:
            dropped.append((i, f"field {field!r} is not stat_level_data[N]"))
            continue
        op = getattr(i, "op", "set") or "set"
        if op != "set":
            dropped.append((i, (f"op {op!r} not supported for stat_level_data "
                                f"(only 'set')")))
            continue
        raw_key = getattr(i, "key", None)
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            dropped.append((i, f"record key {raw_key!r} is not an integer"))
            continue
        idx = int(m.group(1))
        if not 0 <= idx < _SLD_COUNT:
            dropped.append((i, (f"stat_level_data index {idx} out of range "
                                f"0..{_SLD_COUNT - 1}")))
            continue
        new = getattr(i, "new", None)
        packed = _encode(new)
        if packed is None:
            dropped.append((i, (f"value {new!r} is not a whole number that "
                                f"fits a stat_level_data element "
                                f"(0..{(2 ** 64 - 1) >> _FRACTION_BITS})")))
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
                dropped.append((i, (f"record key {key} is not a rate stat "
                                    f"(tail {len(tail)}B, expected "
                                    f"{_RATE_TAIL_LEN}B) -- it has no "
                                    f"stat_level_data")))
            continue
        new_rec = bytearray(rec)
        blk = tail_start + _SLD_TAIL_OFFSET
        for idx, packed, _ in writes:
            at = blk + idx * _SLD_ELEM
            new_rec[at:at + _SLD_ELEM] = packed
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
