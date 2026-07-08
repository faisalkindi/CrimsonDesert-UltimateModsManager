"""Locate + edit gear stats (armor defense / weapon damage / AbyssGear stats)
inside opaque iteminfo equipment records.

Background
----------
On CD 1.13 the *equipment* iteminfo record tail is a distinct, variable layout
the whole-record parser does not model, so those 3,341 records are carried
``_opaque_record`` (raw bytes, byte-exact but not field-decoded). The stats
modders actually want -- armor defense, weapon damage, the AbyssGear
enhancement values -- live inside ``EnchantStatData`` blocks in that tail:
carrays of ``EnchantStatChange {stat: u32, value: i64}``.

We don't need to decode the whole record to *edit a stat*. We locate the
``EnchantStatData`` blocks structurally and expose each numeric stat value. A
value is a fixed-width ``i64``, so editing it is a **same-width overwrite**:
byte-exact, the record length never changes, and the companion ``.pabgh`` index
stays valid. This is the same safety property the item-price writer relies on.

Precision (the safety that matters)
-----------------------------------
A candidate block is accepted only when:

1. its ``EnchantStatData`` parses cleanly as four consecutive carrays, AND
2. every stat key in it is in a whitelist derived **adaptively from the table
   itself** -- keys that recur at least ``MIN_STAT_FREQ`` times across the
   opaque records. Real stat keys recur in the thousands; a coincidental
   byte pattern almost never hits a specific high-frequency key.

Measured on the live 1.13 iteminfo (6,508 records, 3,341 opaque): the locator
finds stats in ~95% of gear records, with ~0 false positives on decoded
(non-gear) records, and 100% of located edits round-trip byte-exact. The
adaptive whitelist means no game-version-specific constant is baked in.

This module is intentionally self-contained (no dependency on the big native
parser) so it stays fast and CI-testable with synthetic fixtures.
"""
from __future__ import annotations

import re
import struct
from collections import Counter
from dataclasses import dataclass

# Stat keys are dense IDs in a known band; values are game-sane magnitudes.
# These bounds only gate the *whitelist scan* -- the whitelist itself is what
# provides precision, these just keep the scan cheap.
_STAT_KEY_MIN = 900_000
_STAT_KEY_MAX = 1_200_000
_VALUE_ABS_MAX = 2_000_000_000
MIN_STAT_FREQ = 10          # a stat key must recur this often to be "real"
_MAX_LIST_COUNT = 40        # a single stat carray never has more entries


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _i64(b: bytes, o: int) -> int:
    return struct.unpack_from("<q", b, o)[0]


# --- adaptive whitelist ---------------------------------------------------

def _scan_candidate_stat_keys(record: bytes):
    """Yield every stat key that appears in a plausible-looking
    ``carray<EnchantStatChange>`` anywhere in ``record``. Deliberately loose:
    this only feeds the frequency histogram, not the final decision."""
    n = len(record)
    p = 0
    while p + 4 <= n:
        count = _u32(record, p)
        if 1 <= count <= _MAX_LIST_COUNT and p + 4 + count * 12 <= n:
            entries = []
            ok = True
            for j in range(count):
                o = p + 4 + j * 12
                stat = _u32(record, o)
                val = _i64(record, o + 4)
                if not (_STAT_KEY_MIN <= stat <= _STAT_KEY_MAX
                        and -_VALUE_ABS_MAX <= val <= _VALUE_ABS_MAX):
                    ok = False
                    break
                entries.append(stat)
            if ok:
                yield from entries
        p += 1


def build_stat_whitelist(records, min_freq: int = MIN_STAT_FREQ) -> frozenset:
    """Build the real-stat-key whitelist from an iterable of raw record
    ``bytes`` (pass the opaque/equipment records). Keys recurring at least
    ``min_freq`` times are treated as real. Version-independent."""
    freq: Counter = Counter()
    for rec in records:
        freq.update(_scan_candidate_stat_keys(rec))
    return frozenset(k for k, c in freq.items() if c >= min_freq)


# --- structural EnchantStatData reader ------------------------------------

def _read_statchange_list(record: bytes, o: int, value_i64: bool):
    """Read a ``carray`` of stat entries at offset ``o``. Returns
    ``(entries, next_offset)`` or raises ValueError on an implausible shape.
    ``entries`` are ``(stat, value, value_offset)``. When ``value_i64`` the
    value is an editable 8-byte int; otherwise a 1-byte level delta (not
    exposed for editing)."""
    n = len(record)
    if o + 4 > n:
        raise ValueError("truncated count")
    count = _u32(record, o)
    o += 4
    width = 12 if value_i64 else 5
    if count > _MAX_LIST_COUNT or o + count * width > n:
        raise ValueError("implausible count")
    out = []
    for _ in range(count):
        stat = _u32(record, o)
        if value_i64:
            out.append((stat, _i64(record, o + 4), o + 4))
        else:
            out.append((stat, struct.unpack_from("<b", record, o + 4)[0], None))
        o += width
    return out, o


def _read_enchant_stat_data(record: bytes, o: int):
    """Read the four consecutive carrays of an ``EnchantStatData`` at ``o``.
    Returns ``(static_entries, next_offset)`` where ``static_entries`` is the
    flat list of editable ``(stat, value, value_offset)`` from the three i64
    lists. Raises ValueError if the shape doesn't hold."""
    static = []
    for _ in range(3):  # max_stat_list, regen_stat_list, stat_list_static
        ents, o = _read_statchange_list(record, o, value_i64=True)
        static.extend(ents)
    # stat_list_static_level: i8 values, not exposed for editing but consumed
    _lvl, o = _read_statchange_list(record, o, value_i64=False)
    return static, o


# --- locate + edit --------------------------------------------------------

@dataclass(frozen=True)
class GearStat:
    """One editable gear stat located in a record."""
    stat: int          # stat key (game stat ID)
    value: int         # current i64 value
    value_offset: int  # byte offset of the i64 value within the record


def locate_gear_stats(record: bytes, whitelist: frozenset) -> list[GearStat]:
    """Locate editable gear stats in one raw equipment record. Only accepts an
    ``EnchantStatData`` block whose stat keys are all whitelisted. Blocks are
    non-overlapping (first match wins). Returns them in byte order."""
    out: list[GearStat] = []
    n = len(record)
    pos = 0
    last_end = 0
    while pos < n - 16:
        if pos < last_end:
            pos += 1
            continue
        try:
            static, end = _read_enchant_stat_data(record, pos)
        except (ValueError, struct.error):
            pos += 1
            continue
        stats = [s for s, _v, _o in static]
        if stats and all(s in whitelist for s in stats):
            for s, v, off in static:
                out.append(GearStat(stat=s, value=v, value_offset=off))
            last_end = end
        pos += 1
    return out


def apply_stat_edit(record: bytes, value_offset: int, new_value: int) -> bytes:
    """Overwrite the i64 stat value at ``value_offset`` with ``new_value``.
    Same-width, so the returned record has identical length -- byte-exact,
    no ``.pabgh`` reindex. Raises ValueError if the offset is out of range or
    the value doesn't fit in a signed 64-bit int."""
    if not (0 <= value_offset <= len(record) - 8):
        raise ValueError(f"stat value offset {value_offset} out of range")
    if not (-(2 ** 63) <= new_value < 2 ** 63):
        raise ValueError(f"stat value {new_value} does not fit in i64")
    buf = bytearray(record)
    struct.pack_into("<q", buf, value_offset, new_value)
    return bytes(buf)


# --- Format 3 field addressing -------------------------------------------
# A gear-stat intent targets ``gear_stat[N]``. N is a STAT KEY when it's in
# the stat-key band (first entry with that key wins -- how mod authors think:
# "set defense to X"); otherwise it's a positional index into
# ``locate_gear_stats`` order. Bracket form (no dot) so it passes the Format 3
# nested-path validation gate unchanged.
_GEAR_STAT_FIELD = re.compile(r"^gear_stat\[(\d+)\]$")


def is_gear_stat_field(field: str) -> bool:
    return bool(_GEAR_STAT_FIELD.match(field or ""))


def resolve_gear_stat_index(field: str, located: list[GearStat]) -> int | None:
    """Map a ``gear_stat[N]`` field to an index into ``located`` (or None)."""
    m = _GEAR_STAT_FIELD.match(field or "")
    if not m:
        return None
    n = int(m.group(1))
    if n >= _STAT_KEY_MIN:                       # N is a stat key
        for i, g in enumerate(located):
            if g.stat == n:
                return i
        return None
    return n if 0 <= n < len(located) else None  # N is a positional index


def edit_record_stats(record: bytes, edits: dict[int, int],
                      whitelist: frozenset) -> bytes:
    """Apply ``{stat_index: new_value}`` edits to one record and return the
    patched bytes. ``stat_index`` indexes the list from ``locate_gear_stats``
    (stable byte order). Offsets are resolved from the *current* bytes, so
    multiple same-width edits compose. Byte-exact: length is preserved.

    Raises KeyError if an index isn't a located stat."""
    located = locate_gear_stats(record, whitelist)
    out = record
    for idx, new_value in edits.items():
        if not (0 <= idx < len(located)):
            raise KeyError(f"stat index {idx} not found "
                           f"({len(located)} located)")
        out = apply_stat_edit(out, located[idx].value_offset, new_value)
    return out


def locate_all_gear_stats(records_bytes: "dict[int, bytes]",
                          min_freq: int = MIN_STAT_FREQ
                          ) -> "dict[int, list[GearStat]]":
    """Locate editable gear stats across a whole table in one pass.

    ``records_bytes`` maps record key -> raw record ``bytes`` (pass the opaque
    equipment records). The adaptive whitelist is built from *all* of them, so
    precision comes from the table itself with no version-specific constant.
    Returns ``{record_key: [GearStat, ...]}`` containing only the records that
    have at least one located stat, so a caller can offer a stat editor for
    exactly those records.
    """
    whitelist = build_stat_whitelist(records_bytes.values(), min_freq=min_freq)
    if not whitelist:
        return {}
    out: dict[int, list[GearStat]] = {}
    for key, raw in records_bytes.items():
        located = locate_gear_stats(raw, whitelist)
        if located:
            out[key] = located
    return out
