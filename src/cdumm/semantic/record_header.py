"""The two fields every .pabgb record opens with: its key and its name.

Up to game 2.03 a record starts key first::

    [key : key_size bytes][u32 name_len][name : name_len bytes] ...

Game 2.04 writes the same two fields name first::

    [u32 name_len][name : name_len bytes][key : key_size bytes] ...

Nothing after them moved, and no record changed size. The byte that
followed the name before (skipped as a NUL by some readers, read as
``_isBlocked`` by the tables that model it) now follows the key.

Measured on Steam build 26448710 (game 2.04): all 137 game-data tables,
313,496 of 313,496 records, are name first, and all 139,325 records of
the 29 committed pre-2.04 fixture tables are key first. The oracle is
each table's own .pabgh index: the key read out of the body must equal
the index's key for that record's offset. Swapping the two fields back,
and changing nothing else, makes storeinfo (440 of 440 stores, 0 stock
lists mis-round-tripped) and statusgroupinfo (9 of 9 records) read again.

The order is decided once per table, never per record. A record can fit
both readings: formationinfo's record 1 on that build is named ``1``
and has key 1, so read key first it is a key of 1 followed by a
305-byte name. No table mixes the two orders, so the order that fits
every record is the table's order, and that settles such a record.
"""
from __future__ import annotations

import struct

KEY_FIRST = "key_first"
NAME_FIRST = "name_first"

#: Same bound ``parser._parse_entry_header`` and
#: ``format3_apply._payload_offset`` put on a name.
MAX_NAME_LEN = 500


def _key_width(key_size: int) -> int:
    return key_size if key_size > 0 else 4


def header_end(data: bytes, offset: int, key_size: int,
               order: str) -> int | None:
    """Offset of the first byte after the key and name, or None.

    None when the two fields don't fit inside ``data`` under ``order``,
    or the name is longer than ``MAX_NAME_LEN``.
    """
    ks = _key_width(key_size)
    if offset < 0:
        return None
    if order == NAME_FIRST:
        if offset + 4 > len(data):
            return None
        nlen = struct.unpack_from("<I", data, offset)[0]
    else:
        if offset + ks + 4 > len(data):
            return None
        nlen = struct.unpack_from("<I", data, offset + ks)[0]
    end = offset + 4 + nlen + ks
    if nlen > MAX_NAME_LEN or end > len(data):
        return None
    return end


def read_header(data: bytes, offset: int, key_size: int,
                order: str) -> tuple[int, bytes, int] | None:
    """``(key, name_bytes, end)`` of the record at ``offset``, or None."""
    end = header_end(data, offset, key_size, order)
    if end is None:
        return None
    ks = _key_width(key_size)
    if order == NAME_FIRST:
        nlen = struct.unpack_from("<I", data, offset)[0]
        return (int.from_bytes(data[end - ks:end], "little"),
                bytes(data[offset + 4:offset + 4 + nlen]), end)
    return (int.from_bytes(data[offset:offset + ks], "little"),
            bytes(data[offset + ks + 4:end]), end)


def _fits(data: bytes, offset: int, key_size: int, order: str,
          index_key: int) -> bool:
    got = read_header(data, offset, key_size, order)
    return got is not None and got[0] == index_key


def table_header_order(body: bytes, offsets: dict[int, int],
                       key_size: int) -> str | None:
    """The order every record of a table fits, checked against its index.

    ``offsets`` is ``parse_pabgh_index``'s ``{key: offset}``. Key first
    wins when both fit every record, since that is how every pre-2.04
    table reads and an empty table has nothing to decide on. Returns None
    when neither order fits every record; callers then keep the key-first
    reading they always had.
    """
    if all(_fits(body, off, key_size, KEY_FIRST, key)
           for key, off in offsets.items()):
        return KEY_FIRST
    if all(_fits(body, off, key_size, NAME_FIRST, key)
           for key, off in offsets.items()):
        return NAME_FIRST
    return None
