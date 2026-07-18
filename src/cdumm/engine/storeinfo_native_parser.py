"""CDUMM-native parser for storeinfo.pabgb stock record lists.

Clean-room RE for GitHub #183 (pinapana, IHateLacey/HernandPets):
Format 3 mods set the ``stock_data_list`` field (DMM's generic name
for StoreInfo's ``_exchangeItemInfoListForSell``) and CDUMM needs to
read and rewrite that list natively.

Entry body: u16 entry_id + u32 name_len + name + NUL, then fixed
scalar fields. The stock-list u32 ``count`` sits at a layout-dependent
offset into the payload, and the records follow it.

A stock record is::

    [fixed head][sub_data optional][effect_list carray]

Head fields, in every layout so far::

    @0  u16 lookup_a
    @2  u64 raw_a
    @10 u64 raw_b
    @18 u32 raw_c
    @22 u32 raw_d
    @26 u32 raw_e
    ... layout-dependent region (see LAYOUTS) ...
        u8  flag_a / flag_b / flag_c
        u8  is_restore_item          (CD 1.11+)
        u8  const == 1               (the tripwire)
        u32 value.payload.body
        71  opaque value-struct interior (``vgap``)

``sub_data`` uses the engine's optional encoding: u8 flag right after
the head; when 1, 13 more bytes follow (u8 flag + 3x u32 lookup).
``effect_list`` is a u32-count carray at the record end; its element
layout is NOT decoded, so a non-empty list is REFUSED rather than
guessed at.

THE LAYOUT MOVES, SO IT IS DETECTED, NOT ASSUMED
------------------------------------------------
This module used to hardcode one layout. It has now been broken by a
game patch twice:

  * CD 1.11 inserted ``is_restore_item`` (head 109 -> 110).
  * CD 1.12/1.13 inserted a u32 ``order_index_113`` at @30
    (head 110 -> 114), which shifted the flags and the const byte down
    four bytes.

Each time, every store mod stopped applying: the const tripwire caught
the misalignment and the writer refused the whole batch (GitHub #259,
donr484's "Shop Smart. Shop H-Mart" -- 10 of 14 stores dropped). The
tripwire did its job, but the fix was a hand-edit of the constants,
which means the next patch breaks it again.

So the layout is now DETECTED from the file: each candidate is trial-
parsed against the real table and the one that decodes the most entries
wins, with a byte-exact round-trip as the acceptance test. A new game
layout is a new entry in ``LAYOUTS``, and an unknown one degrades to a
clean refusal rather than a corrupt table.

Safety stance: storeinfo.pabgb has no content integrity check but a
corrupt body crashes the game on store open. Every unknown therefore
raises ``StoreinfoParseError`` instead of best-effort parsing, and
serialization is only possible for records this module understands.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class StoreinfoParseError(ValueError):
    """Raised when bytes do not match any known disc-0 layout."""


#: Bytes of the value-struct interior we carry verbatim. Constant across
#: every layout so far: the inserted fields all landed BEFORE it.
VGAP_SIZE = 71


@dataclass(frozen=True)
class StoreLayout:
    """One game build's stock-record shape.

    ``order_index_off`` is the record offset of the u32 added in CD 1.13,
    or ``None`` on builds that predate it. Everything else is derived, so
    a new build is one line here plus a fixture.
    """

    label: str
    count_payload_offset: int      # u32 stock count, relative to payload
    order_index_off: int | None    # u32 order_index_113, or None
    flags_off: int                 # u8 flag_a; flag_b/flag_c follow
    is_restore_off: int | None     # u8 is_restore_item (CD 1.11+), or None
    const_off: int                 # u8 const == 1 (the tripwire)

    @property
    def body_off(self) -> int:
        return self.const_off + 1

    @property
    def vgap_off(self) -> int:
        return self.body_off + 4

    @property
    def head_size(self) -> int:
        return self.vgap_off + VGAP_SIZE


#: Newest first -- detection prefers the current game, and an older build
#: only wins if it actually decodes better.
LAYOUTS: tuple[StoreLayout, ...] = (
    # CD 1.13: u32 order_index_113 at @30 pushed the flags + const down 4.
    # The mod that exposed this ("Shop Smart. Shop H-Mart", donr484) names
    # the field itself and sets it to 0xFFFFFFFF -- which is its value in
    # all 3661 vanilla records.
    StoreLayout("CD 1.13", 44, 30, 34, 37, 38),
    # CD 1.11: is_restore_item inserted at @33.
    StoreLayout("CD 1.11", 44, None, 30, 33, 34),
    # CD 1.10: the original RE (GitHub #183).
    StoreLayout("CD 1.10", 43, None, 30, None, 33),
)

#: The layout used when no detection has been run. CD 1.13 is what the
#: game currently ships; detection overrides it whenever a table is to
#: hand, and every write path detects.
DEFAULT_LAYOUT = LAYOUTS[0]

# Back-compat for callers that imported the old module constant.
LIST_COUNT_PAYLOAD_OFFSET = DEFAULT_LAYOUT.count_payload_offset


@dataclass
class StockRecord:
    """One disc-0 stock record. Field names follow the DMM-generic
    JSON names used by Format 3 mods (#183)."""

    lookup_a: int = 0
    raw_a: int = 0
    raw_b: int = 0
    raw_c: int = 0
    raw_d: int = 0
    raw_e: int = 0
    #: u32 @30, new in CD 1.13. 0xFFFFFFFF in every vanilla record, and
    #: what the mods supply as ``order_index_113``.
    order_index: int = 0xFFFFFFFF
    flag_a: int = 0
    flag_b: int = 0
    flag_c: int = 0
    is_restore_item: int = 0
    const33: int = 1                     # the tripwire byte (always 1)
    body: int = 0                        # value.payload.body
    vgap: bytes = b"\x00" * VGAP_SIZE    # opaque value interior
    sub_data: dict | None = None         # {flag, lookup_a, lookup_b, lookup_c}
    effect_list: list = field(default_factory=list)  # must stay empty


class _Reader:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def u8(self) -> int:
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        v = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def u64(self) -> int:
        v = struct.unpack_from("<Q", self.data, self.pos)[0]
        self.pos += 8
        return v

    def raw(self, n: int) -> bytes:
        v = self.data[self.pos:self.pos + n]
        if len(v) != n:
            raise StoreinfoParseError(
                f"unexpected EOF at {self.pos} (wanted {n} bytes)")
        self.pos += n
        return v


class _Writer:
    def __init__(self) -> None:
        self.out = bytearray()

    def u8(self, v: int) -> None:
        self.out.append(v & 0xFF)

    def u16(self, v: int) -> None:
        self.out += struct.pack("<H", v)

    def u32(self, v: int) -> None:
        self.out += struct.pack("<I", v)

    def u64(self, v: int) -> None:
        self.out += struct.pack("<Q", v)

    def raw(self, b: bytes) -> None:
        self.out += b


def read_stock_record(r: _Reader,
                      layout: StoreLayout = DEFAULT_LAYOUT) -> StockRecord:
    """Read one disc-0 stock record at the reader's position."""
    base = r.pos
    rec = StockRecord()
    rec.lookup_a = r.u16()
    rec.raw_a = r.u64()
    rec.raw_b = r.u64()
    rec.raw_c = r.u32()
    rec.raw_d = r.u32()
    rec.raw_e = r.u32()          # r.pos is now base + 30

    if layout.order_index_off is not None:
        rec.order_index = r.u32()

    rec.flag_a = r.u8()
    rec.flag_b = r.u8()
    rec.flag_c = r.u8()
    if layout.is_restore_off is not None:
        rec.is_restore_item = r.u8()

    rec.const33 = r.u8()
    if rec.const33 != 1:
        # This byte is 1 in every disc-0 record of every layout we know.
        # It is the cheapest tripwire against a one-byte drift -- exactly
        # the failure that hit in CD 1.11 (is_restore_item) and again in
        # CD 1.13 (order_index_113). If the record is misaligned this
        # stops being 1, and we refuse rather than rewrite a misread
        # record into a table the game will crash on.
        raise StoreinfoParseError(
            f"const byte at record offset {r.pos - 1 - base} is "
            f"{rec.const33} (expected 1) at byte {r.pos - 1}; record is "
            f"not the verified disc-0 shape for layout {layout.label!r} "
            f"or the layout has drifted again")
    rec.body = r.u32()
    rec.vgap = r.raw(VGAP_SIZE)

    sub_flag = r.u8()
    if sub_flag == 1:
        rec.sub_data = {
            "flag": r.u8(),
            "lookup_a": r.u32(),
            "lookup_b": r.u32(),
            "lookup_c": r.u32(),
        }
    elif sub_flag == 0:
        rec.sub_data = None
    else:
        raise StoreinfoParseError(
            f"sub_data optional flag is {sub_flag} at byte "
            f"{r.pos - 1}; record is not the verified disc-0 shape "
            f"(disc-variant value payload)")

    effect_count = r.u32()
    if effect_count != 0:
        raise StoreinfoParseError(
            f"effect_list has {effect_count} element(s); the element "
            f"layout is not decoded yet, refusing to parse rather "
            f"than guess")
    rec.effect_list = []
    return rec


def write_stock_record(w: _Writer, rec: StockRecord,
                       layout: StoreLayout = DEFAULT_LAYOUT) -> None:
    """Serialize one disc-0 stock record in ``layout``'s shape."""
    if rec.effect_list:
        raise StoreinfoParseError(
            "cannot serialize a non-empty effect_list (layout not "
            "decoded)")
    if len(rec.vgap) != VGAP_SIZE:
        raise StoreinfoParseError(
            f"vgap must be exactly {VGAP_SIZE} bytes, got {len(rec.vgap)}")
    w.u16(rec.lookup_a)
    w.u64(rec.raw_a)
    w.u64(rec.raw_b)
    w.u32(rec.raw_c)
    w.u32(rec.raw_d)
    w.u32(rec.raw_e)
    if layout.order_index_off is not None:
        w.u32(rec.order_index & 0xFFFFFFFF)
    w.u8(rec.flag_a)
    w.u8(rec.flag_b)
    w.u8(rec.flag_c)
    if layout.is_restore_off is not None:
        w.u8(rec.is_restore_item)
    w.u8(rec.const33)
    w.u32(rec.body)
    w.raw(rec.vgap)
    if rec.sub_data is None:
        w.u8(0)
    else:
        w.u8(1)
        w.u8(rec.sub_data["flag"])
        w.u32(rec.sub_data["lookup_a"])
        w.u32(rec.sub_data["lookup_b"])
        w.u32(rec.sub_data["lookup_c"])
    w.u32(0)  # effect_list count (always empty, enforced above)


def parse_stock_list(data: bytes, count_offset: int,
                     layout: StoreLayout = DEFAULT_LAYOUT
                     ) -> tuple[list[StockRecord], int, int]:
    """Parse the stock record list whose u32 count sits at
    ``count_offset`` in ``data``.

    Returns ``(records, list_start, list_end)`` where ``data[list_start:
    list_end]`` is exactly the count field plus all records -- the span
    :func:`serialize_stock_list` reproduces.
    """
    r = _Reader(data, count_offset)
    count = r.u32()
    if not (0 <= count < 10000):
        raise StoreinfoParseError(
            f"implausible stock record count {count} at offset "
            f"{count_offset}")
    records = [read_stock_record(r, layout) for _ in range(count)]
    return records, count_offset, r.pos


def serialize_stock_list(records: list[StockRecord],
                         layout: StoreLayout = DEFAULT_LAYOUT) -> bytes:
    """Serialize a full stock list (u32 count + records)."""
    w = _Writer()
    w.u32(len(records))
    for rec in records:
        write_stock_record(w, rec, layout)
    return bytes(w.out)


# ── layout detection ────────────────────────────────────────────────────

def _entry_payload(body: bytes, off: int) -> int:
    """Start of an entry's payload: past u16 id + u32 name_len + name + NUL."""
    name_len = struct.unpack_from("<I", body, off + 2)[0]
    return off + 6 + name_len + 1


def _score_layout(body: bytes, entry_offsets: list[int],
                  layout: StoreLayout) -> tuple[int, int]:
    """``(entries_decoded, records_decoded)`` for one candidate layout.

    An entry counts only if its whole stock list parses AND re-serializes
    to the identical bytes. Parsing alone is not enough: a wrong layout
    can consume a plausible-looking span and still be misreading it, and
    a misread record written back is a corrupt table. Byte-exactness is
    the only acceptance test that can't be fooled.
    """
    entries = 0
    records = 0
    for off in entry_offsets:
        try:
            payload = _entry_payload(body, off)
            recs, start, end = parse_stock_list(
                body, payload + layout.count_payload_offset, layout)
            if serialize_stock_list(recs, layout) != body[start:end]:
                continue
        except (StoreinfoParseError, struct.error, IndexError):
            continue
        entries += 1
        records += len(recs)
    return entries, records


def detect_storeinfo_layout(body: bytes,
                            entry_offsets: list[int]) -> StoreLayout:
    """Pick the layout that actually decodes this table.

    Trial-parses every candidate and keeps the one that byte-exactly
    round-trips the most stock records. Raises when none of them decode
    anything, which means the game changed in a way we don't model --
    and a clean refusal is the correct outcome there, because the
    alternative is writing a misread record into a table whose only
    integrity check is the game crashing on store open.
    """
    best: StoreLayout | None = None
    best_score = (0, 0)
    for cand in LAYOUTS:
        score = _score_layout(body, entry_offsets, cand)
        logger.debug("storeinfo layout %s: %d entries, %d records",
                     cand.label, score[0], score[1])
        if score > best_score:
            best, best_score = cand, score

    # Nothing decoded at all -> the shape is one we don't model. Refuse.
    #
    # Note the test is on ENTRIES, not records: a table whose stock lists
    # are all empty decodes correctly under every layout and yields zero
    # records, and that is a valid table, not an unknown one. Scoring it as
    # "unknown" would turn an empty store into a hard error.
    if best is None or best_score[0] == 0:
        raise StoreinfoParseError(
            "no known storeinfo layout decodes this table (tried: "
            + ", ".join(c.label for c in LAYOUTS)
            + "). The game's stock-record shape has changed again; "
              "refusing rather than rewriting records we can't read.")

    logger.info(
        "storeinfo: detected layout %s (%d/%d entries, %d records "
        "round-trip byte-exact)",
        best.label, best_score[0], len(entry_offsets), best_score[1])
    return best
