"""CDUMM-native parser for storeinfo.pabgb stock record lists.

Clean-room RE for GitHub #183 (pinapana, IHateLacey/HernandPets):
Format 3 mods set the ``stock_data_list`` field (DMM's generic name
for StoreInfo's ``_exchangeItemInfoListForSell``) and CDUMM needs to
read and rewrite that list natively.

Layout (verified by byte-exact round-trip of 192/293 entries of the
CD v1.10 vanilla storeinfo.pabgb, including all 37 records of entry
3101 "Store_Her_General"; the decode scripts live in
``issue_repro/183/``):

Layout note: this module tracks the current CD 1.11 build (game build
23693656). The original RE was against CD 1.10 (count at payload+43,
109-byte record); the 1.11 patch added one byte to the entry head
(count moved to payload+44) and one byte to each record
(is_restore_item @33, head 110). See GitHub #183.

- Entry body: u16 entry_id + u32 name_len + name + NUL, then fixed
  scalar fields. The stock-list u32 ``count`` is at payload+44 and
  records start at payload+48 in every entry.
- A disc-0 stock record is::

      [110-byte fixed head][sub_data optional][effect_list carray]

  Head fields: @0 u16 lookup_a, @2 u64 raw_a, @10 u64 raw_b,
  @18 u32 raw_c, @22 u32 raw_d, @26 u32 raw_e, @30/31/32 u8
  flag_a/b/c, @33 u8 is_restore_item (CD 1.11), @34 u8 const(=1),
  @35 u32 value.payload.body. The remaining @39-109 bytes are the
  value-struct interior; its fields (value.disc/lookup_*/raw_a..raw_g)
  are all zero or mod-edited in the only ground-truth entry, so they
  CANNOT be safely placed yet and are carried as an opaque ``_vgap``
  blob (round-trips exactly).
- ``sub_data`` uses the engine's optional encoding (same primitive
  as iteminfo_native_parser._read_optional): u8 flag at record
  offset 110; when 1, 13 more bytes follow (u8 flag + u32 lookup_a
  + u32 lookup_b + u32 lookup_c).
- ``effect_list`` is a u32-count carray at the record end. Its
  element layout is NOT yet decoded (13 vanilla entries have a
  non-empty list, e.g. key 1137 Store_Dem_4_Fishing); this parser
  REFUSES non-empty lists rather than guessing.

Safety stance: storeinfo.pabgb has no content integrity check but a
corrupt body crashes the game on store open. Every unknown therefore
raises ``StoreinfoParseError`` instead of best-effort parsing, and
serialization is only possible for records this module actually
understands.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


class StoreinfoParseError(ValueError):
    """Raised when bytes do not match the verified disc-0 layout."""


# Fixed head size of a disc-0 stock record. CD game build 23693656
# (the 2026-06-12 / 1.11 patch) inserted one u8 (is_restore_item) at
# record-relative offset 33, between flag_c and the const(=1) byte, so
# the head grew 109 -> 110 and the sub_data optional flag now sits at
# record-relative offset 110. Confirmed on the current build: across
# all 3555 disc-0 records the const byte (@34) is 1 and is_restore_item
# (@33) is a clean 0/1 flag (both values present). See GitHub #183.
_HEAD_SIZE = 110
# Opaque value-struct interior carried verbatim: bytes @39..109.
_VGAP_SIZE = _HEAD_SIZE - 39
# Offset of the stock-list u32 count relative to the entry payload
# start (after entry_id + name_len + name + NUL). The same 1.11 patch
# also added one byte to the entry head (lands in the skipped region
# before the count), moving the count from payload+43 to payload+44.
LIST_COUNT_PAYLOAD_OFFSET = 44


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
    flag_a: int = 0
    flag_b: int = 0
    flag_c: int = 0
    is_restore_item: int = 0           # u8 @33, added in CD 1.11 (#183)
    const33: int = 1
    body: int = 0                      # value.payload.body (u32 @34)
    vgap: bytes = b"\x00" * _VGAP_SIZE  # opaque value interior @38-108
    sub_data: dict | None = None       # {flag, lookup_a, lookup_b, lookup_c}
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


def read_stock_record(r: _Reader) -> StockRecord:
    """Read one disc-0 stock record at the reader's position."""
    rec = StockRecord()
    rec.lookup_a = r.u16()
    rec.raw_a = r.u64()
    rec.raw_b = r.u64()
    rec.raw_c = r.u32()
    rec.raw_d = r.u32()
    rec.raw_e = r.u32()
    rec.flag_a = r.u8()
    rec.flag_b = r.u8()
    rec.flag_c = r.u8()
    rec.is_restore_item = r.u8()       # @33, CD 1.11 (#183)
    rec.const33 = r.u8()
    if rec.const33 != 1:
        # The byte at record offset 34 is 1 in every disc-0 record on
        # the verified build (all 3555). It is the cheapest tripwire
        # against a one-byte layout drift (the exact failure mode behind
        # #183, which recurred when CD 1.11 inserted is_restore_item):
        # if the record is misaligned this byte stops being 1 and we
        # refuse rather than rewrite a misread record.
        raise StoreinfoParseError(
            f"const byte at record offset 34 is {rec.const33} (expected "
            f"1) at byte {r.pos - 1}; record is not the verified disc-0 "
            f"shape or the layout has drifted")
    rec.body = r.u32()
    rec.vgap = r.raw(_VGAP_SIZE)

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


def write_stock_record(w: _Writer, rec: StockRecord) -> None:
    """Serialize one disc-0 stock record."""
    if rec.effect_list:
        raise StoreinfoParseError(
            "cannot serialize a non-empty effect_list (layout not "
            "decoded)")
    if len(rec.vgap) != _VGAP_SIZE:
        raise StoreinfoParseError(
            f"vgap must be exactly {_VGAP_SIZE} bytes, got "
            f"{len(rec.vgap)}")
    w.u16(rec.lookup_a)
    w.u64(rec.raw_a)
    w.u64(rec.raw_b)
    w.u32(rec.raw_c)
    w.u32(rec.raw_d)
    w.u32(rec.raw_e)
    w.u8(rec.flag_a)
    w.u8(rec.flag_b)
    w.u8(rec.flag_c)
    w.u8(rec.is_restore_item)          # @33, CD 1.11 (#183)
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


def parse_stock_list(data: bytes, count_offset: int
                     ) -> tuple[list[StockRecord], int, int]:
    """Parse the stock record list whose u32 count sits at
    ``count_offset`` in ``data``.

    Returns ``(records, list_start, list_end)`` where the byte span
    ``data[list_start:list_end]`` is exactly the count field plus all
    records (what :func:`serialize_stock_list` reproduces).
    """
    r = _Reader(data, count_offset)
    count = r.u32()
    if not (0 <= count < 10000):
        raise StoreinfoParseError(
            f"implausible stock record count {count} at offset "
            f"{count_offset}")
    records = [read_stock_record(r) for _ in range(count)]
    return records, count_offset, r.pos


def serialize_stock_list(records: list[StockRecord]) -> bytes:
    """Serialize a full stock list (u32 count + records)."""
    w = _Writer()
    w.u32(len(records))
    for rec in records:
        write_stock_record(w, rec)
    return bytes(w.out)
