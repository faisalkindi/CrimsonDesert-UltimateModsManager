"""CDUMM-native parser for storeinfo.pabgb stock record lists.

Clean-room RE for GitHub #183 (pinapana, IHateLacey/HernandPets):
Format 3 mods set the ``stock_data_list`` field (DMM's generic name
for StoreInfo's ``_exchangeItemInfoListForSell``) and CDUMM needs to
read and rewrite that list natively.

Entry body: u16 entry_id + u32 name_len + name + NUL, then fixed
scalar fields, then the stock list -- a u32 ``count`` followed by that
many records.

THE LIST IS FOUND, NOT COMPUTED
-------------------------------
The count does NOT sit at a fixed offset into the payload. Some entries
carry an extra ~48-byte block before it, so the same build holds counts
at payload+44 AND payload+92 (also +96 and +112). Assuming one constant
made CDUMM read 71 stocked stores as EMPTY on CD 1.13 -- a *successful*
parse of the wrong u32, so no error, no warning, 1,613 stock records
simply invisible.

So the list is located by an anchor instead: ``StockData._storeInfo``
is a u16 reference to the owning store, it is the first field of the
first record, and it therefore equals the entry's own key. Scanning for
that -- plus a plausible count and a byte-exact round-trip of the whole
span -- pins the list with no offset constant at all. Measured on the
real tables, exactly one offset per entry satisfies it; never two.

A stock record is::

    [fixed head][sub_data optional][sub_gap][effect_list carray]

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
``sub_gap`` is a second opaque block, empty until CD b25116796 added
8 bytes there; like the value interior it is carried verbatim, because
it is zero in every record the game ships and so offers nothing to
name fields from. ``effect_list`` is a u32-count carray at the record
end -- the engine's ``StockData._orderCountDataList``, whose element
is ``StockOrderCountData``. Its 12-byte size was derived by exact tiling
(see ``ORDER_ELEM_SIZE``); the interior is carried verbatim rather than
decoded, which is enough to round-trip and keeps us from guessing at
two fields we have no ground truth for.

THE LAYOUT MOVES, SO IT IS DETECTED, NOT ASSUMED
------------------------------------------------
This module used to hardcode one layout. Game patches have since moved
the shape five times:

  * CD 1.11 inserted ``is_restore_item`` (head 109 -> 110).
  * CD 1.12/1.13 inserted a u32 ``order_index_113`` at @30
    (head 110 -> 114), which shifted the flags and the const byte down
    four bytes.
  * CD 1.16 inserted a u32 ``_lowPriceThresholdCount`` after ``raw_c``.
  * CD 1.16.1 took four bytes back OUT of the opaque interior, 71 -> 67,
    without moving anything ahead of it.
  * CD b25116796 added eight bytes AFTER ``sub_data``, ahead of the
    ``effect_list`` count -- again with nothing ahead of it moving.

The first three broke every store mod outright: the const tripwire
caught the misalignment and the writer refused the batch (GitHub #259,
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


#: Bytes of the value-struct interior we carry verbatim.
#:
#: This was constant from CD 1.10 through CD 1.16 because every field the
#: game inserted landed BEFORE it. The 15 August 2026 patch broke that: it
#: took four bytes out of the interior itself, 71 -> 67, leaving every
#: field ahead of it in place (GitHub #365). So it is per-layout now, and
#: this name remains only as the pre-1.16.1 default for the dataclass and
#: for callers that never had a layout to hand.
VGAP_SIZE = 71

#: Size of one ``StockOrderCountData`` element (``_orderCountDataList``).
#:
#: Derived by exact tiling rather than assumed: for every entry whose
#: first record carries a non-empty list, walk the whole list assuming a
#: fixed element size and require every subsequent record to land on a
#: valid boundary. Exactly one size in 1..128 satisfies that -- 12 --
#: and it satisfies it for all of them. A size that "nearly" works fails
#: outright, so this is unique-or-nothing rather than best-fit.
ORDER_ELEM_SIZE = 12

#: Smallest payload offset at which a stock list has ever been seen. Used
#: only to bound the search; the list is LOCATED, not computed (see the
#: module docstring), so this is a floor and not an answer.
MIN_LIST_OFFSET = 0


@dataclass(frozen=True)
class StoreLayout:
    """One game build's stock-record shape.

    ``order_index_off`` is the record offset of the u32 added in CD 1.13,
    or ``None`` on builds that predate it. Everything else is derived, so
    a new build is one line here plus a fixture.

    Note the offsets are descriptive, not operative: the reader consumes
    fields sequentially, so what actually selects a shape is which
    of ``order_index_off`` / ``is_restore_off`` / ``low_price_threshold``
    are set, not the numbers.
    """

    label: str
    #: The payload offset the stock count sits at for entries with no
    #: optional block -- the commonest case, not the rule. Nothing reads
    #: it to find a list any more (``locate_stock_list`` does that); it
    #: survives as documentation and for building synthetic entries.
    count_payload_offset: int
    order_index_off: int | None    # u32 order_index_113, or None
    flags_off: int                 # u8 flag_a; flag_b/flag_c follow
    is_restore_off: int | None     # u8 is_restore_item (CD 1.11+), or None
    const_off: int                 # u8 const == 1 (the tripwire)
    #: CD 1.16 inserted a u32 after ``raw_c`` -- ``_lowPriceThresholdCount``
    #: in the binary's own field names.
    low_price_threshold: bool = False
    #: Bytes of the opaque value interior. 71 up to CD 1.16; the 15 Aug
    #: 2026 patch cut it to 67 without moving anything ahead of it, which
    #: is why ``const_off`` is unchanged on that build (GitHub #365).
    vgap_size: int = VGAP_SIZE
    #: vgap-relative offsets of the three fields the writer maps INSIDE
    #: the opaque interior for a NEW record: raw_e (u32), raw_g (u16),
    #: raw_q (u32). Constant at 41/57/59 from CD 1.10 through CD 1.16.
    #:
    #: On CD 1.16.1 these move to 37/53/55. Naive first-divergence against
    #: CD 1.16 lands at index 37 for 4,749 of the 6,376 shared records and
    #: at 53 for the other 1,627 -- which is what first made this look
    #: like two different removal points needing two different mappings.
    #: It isn't: for literally all 6,376, re-derived by SHIFT (not first
    #: difference) -- keep vgap[:37], drop 4, keep the rest -- reproduces
    #: the CD 1.16 interior byte-exact. The 53-only-look records are not a
    #: second layout; every one of them has vgap[37:57] == 0 in both
    #: builds, so shifting at 53 instead is *also* consistent for them by
    #: coincidence of zero-padding, never because shifting at 37 is wrong.
    #: No record in the shared table ever contradicts 37. See
    #: ``test_vgap_shift_is_uniformly_at_37`` for the check across the
    #: committed fixtures.
    raw_e_off: int = 41
    raw_g_off: int = 57
    raw_q_off: int = 59
    #: Whether ``raw_e_off`` / ``raw_g_off`` / ``raw_q_off`` are known to
    #: be correct for this build, i.e. safe to write a NEW record with.
    #: Reading is unaffected by this flag either way: the interior is
    #: always carried through verbatim for records that already exist.
    vgap_map_verified: bool = True
    #: Bytes of a second opaque block, between ``sub_data`` and the
    #: ``effect_list`` count. 0 up to CD 2.00.01; the 4 September 2026
    #: patch added 8 -- see the CD b25116796 layout. Carried verbatim
    #: like ``vgap`` rather than decoded: it is zero in all 6,376
    #: records of the shipped table, so there is no ground truth to
    #: name fields from and nothing to lose by not naming them.
    sub_gap_size: int = 0

    @property
    def body_off(self) -> int:
        return self.const_off + 1

    @property
    def vgap_off(self) -> int:
        return self.body_off + 4

    @property
    def head_size(self) -> int:
        return self.vgap_off + self.vgap_size


#: Newest first -- detection prefers the current game, and an older build
#: only wins if it actually decodes better.
LAYOUTS: tuple[StoreLayout, ...] = (
    # CD buildid 25116796 (4 September 2026 patch -- the same one that
    # renamed the tables to .staticinfobody, see cdumm.archive.table_ext):
    # 8 zero bytes appeared between ``sub_data`` and the ``effect_list``
    # count. Named by buildid because this build ships no version string
    # worth citing -- bin64/CrimsonDesert.exe still reports 1.0.0.2760 --
    # and a buildid is machine-checkable rather than guessed (the same
    # reasoning as the vanilla_b24773079 fixture).
    #
    # Everything AHEAD of the insertion is untouched, which is why this
    # one did not trip the const tripwire either: it still reads 1 at
    # offset 42 and the 67-byte interior is unchanged. Detection did not
    # degrade cleanly, it degraded into CD 1.16 decoding 17 of 436
    # entries by coincidence, and the writer then refused 16 of the 17
    # stores in donr484's "Shop Smart. Shop H-Mart".
    #
    # Derived against the committed CD 2.00.01 table (buildid 24994088),
    # which carries the same 436 entries: every entry grew by exactly
    # 1 + 8 x record_count bytes, no exceptions. The +1 is a byte in the
    # entry's own fixed part, which nothing here reads -- the list is
    # LOCATED, not computed -- and the +8 is per record. For all 6,376
    # shared records the 8 bytes sit at the ``effect_list`` count, are
    # always zero, and the record either side of them is reproduced
    # byte-exact by the shift. The 99 records that differ past it differ
    # in raw_a or sub_data, i.e. in shipped values, not in shape. See
    # tests/test_storeinfo_layout_b25116796.py.
    #
    # The 67-byte interior is byte-identical to CD 1.16.1 across those
    # 6,376 records, so raw_e / raw_g / raw_q keep 37 / 53 / 55 and
    # ADDING a stock record stays safe on this build.
    StoreLayout("CD b25116796", 45, 34, 38, 41, 42,
                low_price_threshold=True, vgap_size=67,
                raw_e_off=37, raw_g_off=53, raw_q_off=55,
                vgap_map_verified=True, sub_gap_size=8),
    # CD 1.16.1 (15 Aug 2026 patch): the first change that did NOT insert
    # or remove a field ahead of the const tripwire. Everything up to and
    # including the const byte is identical to CD 1.16 -- record 0 of a
    # failing store still reads 01 at offset 42 -- and the four bytes came
    # out of the opaque interior instead, 71 -> 67.
    #
    # That is why the const tripwire did not catch it as a shift: it was
    # never misaligned at the const. The failure surfaced one record later,
    # when the next record started four bytes early and ITS const read 0.
    #
    # Measured on buildid fingerprint 7b103b0202b606fd (exe 2026-08-15):
    # every one of the 397 entries that decode on 1.16 shrank by exactly
    # 4 x record_count bytes, with zero exceptions, and this layout reads
    # 398 located + 39 provably empty = 437/437, 6378 records, 0 not-found
    # and 0 mis-round-tripped.
    #
    # The interior's removed 4 bytes are at vgap offset 37 for every one
    # of those records (see raw_e_off's docstring and
    # test_vgap_shift_is_uniformly_at_37) -- so raw_e/raw_g/raw_q move to
    # 37/53/55 and adding a stock record is safe again.
    StoreLayout("CD 1.16.1", 44, 34, 38, 41, 42,
                low_price_threshold=True, vgap_size=67,
                raw_e_off=37, raw_g_off=53, raw_q_off=55,
                vgap_map_verified=True),
    # CD 1.16: a u32 (_lowPriceThresholdCount) inserted after raw_c pushed
    # everything below it down another four bytes. Under the CD 1.13 shape
    # the live 1.16 table decodes ZERO entries; under this one, 397 of 432
    # (the other 35 are provably empty -- see locate_stock_list).
    StoreLayout("CD 1.16", 44, 34, 38, 41, 42, low_price_threshold=True),
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

#: The layout used when no detection has been run -- the newest one,
#: i.e. what the game currently ships. Detection overrides it whenever a
#: table is to hand, and every write path detects, so this only matters
#: to callers that build records from nothing.
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
    #: u32 after raw_c, new in CD 1.16 (``_lowPriceThresholdCount``).
    low_price_threshold_count: int = 0
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
    #: Opaque block between ``sub_data`` and the ``effect_list`` count,
    #: sized by ``StoreLayout.sub_gap_size`` (empty before CD b25116796).
    sub_gap: bytes = b""
    #: ``_orderCountDataList``: ORDER_ELEM_SIZE-byte blobs, carried
    #: verbatim. Named ``effect_list`` because that is the key Format 3
    #: store mods already use.
    effect_list: list[bytes] = field(default_factory=list)


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
    if layout.low_price_threshold:
        rec.low_price_threshold_count = r.u32()
    rec.raw_d = r.u32()
    rec.raw_e = r.u32()          # r.pos is now base + 30 (+4 on CD 1.16)

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
    rec.vgap = r.raw(layout.vgap_size)

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

    if layout.sub_gap_size:
        rec.sub_gap = r.raw(layout.sub_gap_size)

    effect_count = r.u32()
    if effect_count > 4096:
        # Not a length -- we are misaligned. Bail rather than allocate.
        raise StoreinfoParseError(
            f"effect_list count {effect_count} at byte {r.pos - 4} is "
            f"implausible; record is misaligned")
    rec.effect_list = [r.raw(ORDER_ELEM_SIZE) for _ in range(effect_count)]
    return rec


def write_stock_record(w: _Writer, rec: StockRecord,
                       layout: StoreLayout = DEFAULT_LAYOUT) -> None:
    """Serialize one disc-0 stock record in ``layout``'s shape."""
    for i, el in enumerate(rec.effect_list):
        if not isinstance(el, (bytes, bytearray)) or len(el) != ORDER_ELEM_SIZE:
            raise StoreinfoParseError(
                f"effect_list[{i}] must be exactly {ORDER_ELEM_SIZE} "
                f"opaque bytes, got {el!r}")
    if len(rec.vgap) != layout.vgap_size and not any(rec.vgap):
        # An all-zero interior of the wrong length is the dataclass
        # placeholder on a record built from nothing, not data: it carries
        # no information to lose, so size it to the layout rather than
        # refuse. A non-zero interior of the wrong length IS data from
        # another build and still refuses below. (GitHub #365, where the
        # interior stopped being one fixed size across builds.)
        rec.vgap = b"\x00" * layout.vgap_size
    if len(rec.vgap) != layout.vgap_size:
        raise StoreinfoParseError(
            f"vgap must be exactly {layout.vgap_size} bytes, got "
            f"{len(rec.vgap)}")
    if len(rec.sub_gap) != layout.sub_gap_size and not any(rec.sub_gap):
        # Same reasoning as the vgap placeholder above: an all-zero block
        # of the wrong length is a default, not data, so size it to the
        # layout. That is also what lets a record read on one build
        # serialize on another -- the point of keeping the block opaque.
        rec.sub_gap = b"\x00" * layout.sub_gap_size
    if len(rec.sub_gap) != layout.sub_gap_size:
        raise StoreinfoParseError(
            f"sub_gap must be exactly {layout.sub_gap_size} bytes, got "
            f"{len(rec.sub_gap)}")
    w.u16(rec.lookup_a)
    w.u64(rec.raw_a)
    w.u64(rec.raw_b)
    w.u32(rec.raw_c)
    if layout.low_price_threshold:
        w.u32(rec.low_price_threshold_count)
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
    w.raw(rec.sub_gap)
    w.u32(len(rec.effect_list))
    for el in rec.effect_list:
        w.raw(bytes(el))


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


class StoreListNotFound(StoreinfoParseError):
    """No stock list could be located in an entry.

    ``provably_empty`` is True when the entry is too short to hold even
    one record at any offset -- i.e. the store really has no stock, and
    the failure to locate a list is the correct answer rather than a
    gap in our understanding.
    """

    def __init__(self, msg: str, *, provably_empty: bool = False) -> None:
        super().__init__(msg)
        self.provably_empty = provably_empty


def _min_list_bytes(layout: StoreLayout) -> int:
    """Smallest span a one-record list can occupy: count + record."""
    return 4 + (2 + 8 + 8 + 4 + (4 if layout.low_price_threshold else 0)
                + 4 + 4 + (4 if layout.order_index_off is not None else 0)
                + 3 + (1 if layout.is_restore_off is not None else 0)
                + 1 + 4 + layout.vgap_size + 1 + layout.sub_gap_size + 4)


def locate_stock_list(body: bytes, payload: int, entry_end: int, key: int,
                      layout: StoreLayout
                      ) -> tuple[list[StockRecord], int, int]:
    """Find and parse an entry's stock list. No offset constant.

    Scans the entry for a span that is simultaneously

      * a plausible non-empty u32 count,
      * whose first record's ``lookup_a`` is the entry's own key
        (``StockData._storeInfo``, a u16 back-reference to the owning
        store -- the anchor),
      * whose records all parse in ``layout``'s shape, and
      * which re-serializes to the identical bytes.

    Returns ``(records, list_start, list_end)``.

    Raises :class:`StoreListNotFound`. When the entry is too short to
    hold one record at any offset the store is *provably* empty and the
    exception says so, which lets callers tell "this store has no stock"
    apart from "we could not read this store".

    Why a scan and not a constant: some entries carry an extra block
    before the list, so one build holds counts at four different
    offsets. Measured over the real CD 1.13 and CD 1.16 tables, exactly
    one offset per entry passes all four conditions -- never two -- so
    the scan is deterministic, not a best-guess.
    """
    room = entry_end - payload
    if room < _min_list_bytes(layout):
        raise StoreListNotFound(
            f"store {key}: entry holds {room} bytes after its header, too "
            f"few for even one {layout.label} record; the store has no "
            f"stock list", provably_empty=True)

    found: list[tuple[list[StockRecord], int, int]] = []
    limit = entry_end - _min_list_bytes(layout)
    for off in range(payload + MIN_LIST_OFFSET, limit + 1):
        # Cheap rejections first: a count and the anchor are 6 bytes.
        count = struct.unpack_from("<I", body, off)[0]
        if not (0 < count < 10000):
            continue
        if struct.unpack_from("<H", body, off + 4)[0] != key:
            continue
        try:
            recs, start, end = parse_stock_list(body, off, layout)
        except (StoreinfoParseError, struct.error, IndexError):
            continue
        if end > entry_end:
            continue
        try:
            if serialize_stock_list(recs, layout) != body[start:end]:
                continue
        except (StoreinfoParseError, struct.error):
            continue
        found.append((recs, start, end))

    if not found:
        raise StoreListNotFound(
            f"store {key}: no span in this entry is a byte-exact "
            f"{layout.label} stock list anchored on the store key; "
            f"refusing rather than writing to a guessed offset")
    if len(found) > 1:
        # Never observed on a real table. If a build ever makes it
        # possible, refusing is the only safe answer.
        raise StoreListNotFound(
            f"store {key}: {len(found)} distinct spans each parse as a "
            f"byte-exact stock list ({[f[1] - payload for f in found]}); "
            f"ambiguous, refusing")
    return found[0]


def _is_provably_empty(body: bytes, entry_offsets: list[int],
                       off: int) -> bool:
    """True when this entry cannot hold one record under ANY layout."""
    ordered = sorted(entry_offsets)
    i = ordered.index(off)
    end = ordered[i + 1] if i + 1 < len(ordered) else len(body)
    try:
        room = end - _entry_payload(body, off)
    except (struct.error, IndexError):
        return False
    return room < min(_min_list_bytes(c) for c in LAYOUTS)


def _score_layout(body: bytes, entry_offsets: list[int],
                  layout: StoreLayout) -> tuple[int, int]:
    """``(entries_decoded, records_decoded)`` for one candidate layout.

    An entry counts only if its whole stock list parses AND re-serializes
    to the identical bytes. Parsing alone is not enough: a wrong layout
    can consume a plausible-looking span and still be misreading it, and
    a misread record written back is a corrupt table. Byte-exactness is
    the only acceptance test that can't be fooled.

    Provably-empty entries are not counted either way -- they decode
    identically under every layout, so they carry no signal.
    """
    entries = 0
    records = 0
    ordered = sorted(entry_offsets)
    for i, off in enumerate(ordered):
        end = ordered[i + 1] if i + 1 < len(ordered) else len(body)
        try:
            key = struct.unpack_from("<H", body, off)[0]
            recs, _s, _e = locate_stock_list(
                body, _entry_payload(body, off), end, key, layout)
        except (StoreinfoParseError, struct.error, IndexError):
            continue
        entries += 1
        records += len(recs)
    return entries, records


def _unreadable_entries(body: bytes, entry_offsets: list[int],
                        layout: StoreLayout) -> int:
    """Entries that neither decode nor are *provably empty* under ``layout``.

    The count of decoded entries alone stopped separating layouts on the
    CD 1.16.1 table (GitHub #365): CD 1.13 byte-exactly decodes 320 of its
    437 entries against CD 1.16.1's 398, a margin of 1.24x where every
    earlier build gave 100x or more. Ranking on that alone is one bad
    build away from picking a layout that reads two thirds of a table and
    silently abandons the rest.

    An entry that is neither decodable nor provably empty is a hole in the
    explanation, and the right layout has none. On that same table CD 1.13
    leaves 78 and CD 1.16.1 leaves 0, which is decisive where 320 vs 398
    is not.
    """
    holes = 0
    ordered = sorted(entry_offsets)
    for i, off in enumerate(ordered):
        end = ordered[i + 1] if i + 1 < len(ordered) else len(body)
        try:
            key = struct.unpack_from("<H", body, off)[0]
            locate_stock_list(
                body, _entry_payload(body, off), end, key, layout)
        except StoreListNotFound as exc:
            if not exc.provably_empty:
                holes += 1
        except (StoreinfoParseError, struct.error, IndexError):
            holes += 1
    return holes


def detect_storeinfo_layout(body: bytes,
                            entry_offsets: list[int]) -> StoreLayout:
    """Pick the layout that actually decodes this table.

    Trial-parses every candidate and keeps the one that leaves the
    fewest entries unexplained, then the one that byte-exactly
    round-trips the most stock records. Raises when none of them decode
    anything, and equally when the best of them explains less of the
    table than it leaves unexplained: both mean the game changed in a
    way we don't model, and a clean refusal is the correct outcome
    there, because the alternative is writing a misread record into a
    table whose only integrity check is the game crashing on store open.
    """
    best: StoreLayout | None = None
    best_rank: tuple[int, int, int] | None = None
    for cand in LAYOUTS:
        entries, records = _score_layout(body, entry_offsets, cand)
        if not entries:
            logger.debug("storeinfo layout %s: decodes nothing", cand.label)
            continue
        # Fewest unexplained entries first, then the old
        # (entries, records) ordering. A layout that reads more records
        # while abandoning entries the other one reads has not understood
        # the table better -- see `_unreadable_entries`.
        holes = _unreadable_entries(body, entry_offsets, cand)
        rank = (-holes, entries, records)
        logger.debug(
            "storeinfo layout %s: %d entries, %d records, %d unreadable",
            cand.label, entries, records, holes)
        if best_rank is None or rank > best_rank:
            best, best_rank = cand, rank

    # Nothing decoded at all -> either every store is empty (fine, and
    # unknowable by construction) or the shape is one we don't model.
    #
    # Telling those apart is what `provably empty` buys: an entry with
    # too few bytes to hold one record cannot be hiding a stock list from
    # us under any layout, so a table made only of those is a valid
    # degenerate table rather than an unreadable one. Without that
    # distinction an all-empty table would be a hard error.
    if best is None or best_rank is None:
        if entry_offsets and all(
                _is_provably_empty(body, entry_offsets, off)
                for off in entry_offsets):
            logger.info(
                "storeinfo: every entry is too small to hold a stock "
                "record; treating the table as empty (layout %s)",
                DEFAULT_LAYOUT.label)
            return DEFAULT_LAYOUT
        raise StoreinfoParseError(
            "no known storeinfo layout decodes this table (tried: "
            + ", ".join(c.label for c in LAYOUTS)
            + "). The game's stock-record shape has changed again; "
              "refusing rather than rewriting records we can't read.")

    # A winner that explains less of the table than it leaves unexplained
    # has not recognised this build; it has landed on the least-bad of a
    # set of wrong answers. Returning it is worse than refusing, because
    # the refusal then arrives per store, from the writer, phrased as if
    # a mod were at fault -- which is exactly how the 4 Sep 2026 patch
    # surfaced: "CD 1.16" won with 17 entries read and 380 unexplained,
    # and the user saw 16 stores dropped with no mention of the game
    # having changed. Every layout that IS the build's own leaves zero
    # unexplained entries (asserted for all five committed tables), so
    # this threshold is far below anything a correct layout produces.
    holes, entries = -best_rank[0], best_rank[1]
    if holes > entries:
        raise StoreinfoParseError(
            f"no known storeinfo layout explains this table: the best "
            f"candidate ({best.label}) reads {entries} of "
            f"{len(entry_offsets)} entries and leaves {holes} it can "
            f"neither decode nor prove empty. The game's stock-record "
            f"shape has changed again; refusing rather than rewriting "
            f"records we can't read (tried: "
            + ", ".join(c.label for c in LAYOUTS) + ")")

    logger.info(
        "storeinfo: detected layout %s (%d/%d entries, %d records "
        "round-trip byte-exact, %d unreadable)",
        best.label, best_rank[1], len(entry_offsets), best_rank[2],
        -best_rank[0])
    return best
