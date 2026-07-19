"""inventory.pabgb slot-count writer (DMM Mod Builder "max inventory" mods).

DMM Mod Builder presets set ``default_slot_count`` / ``max_slot_count`` /
``need_save_slot_count`` on named inventory records (CampWareHouse, WareHouse,
Character, the Housing_* storages, ...). ``cdmod_handler`` turns each into a
Format 3 intent that identifies the record by ``entry`` (the inventory name)::

    {entry: "CampWareHouse", field: "default_slot_count", op: "set", new: 1000}

``inventory.pabgb`` has no CDUMM PABGB schema, and its companion
``inventory.pabgh`` does NOT encode record byte offsets (parse_pabgh_index
returns nothing for it), so records can't be located the usual way. This
writer frames them by content instead, from the reverse-engineered layout:

  * Each record begins ``<u16 entry_id><u32 name_len><name utf-8><0x00>``.
  * Every record carries exactly one 5-byte marker ``28 80 02 00 00`` (the
    reader for its item-type lists), and the three slot counts sit as three
    consecutive little-endian u16 immediately before it::

        marker-6 : need_save_slot_count (u16)   (0 in every vanilla record)
        marker-4 : default_slot_count   (u16)
        marker-2 : max_slot_count       (u16)

Verified on the real 1.14 table (20 records): default <= max in all 20, the
values match the game's storage sizes, and a length-preserving u16 overwrite
changes exactly the two target bytes -- byte-exact by construction, and the
.pabgh needs no update because nothing moves.
"""
from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

_MARK = bytes.fromhex("2880020000")
_SLOT_FIELD_OFFSET = {          # bytes before the marker, and u16 width
    "need_save_slot_count": -6,
    "default_slot_count": -4,
    "max_slot_count": -2,
}
_U16_MAX = 0xFFFF


def _find_record_start(body: bytes, name: str) -> int | None:
    """Byte offset where the record named ``name`` begins, or None.

    Matches the ``<u32 name_len><name><0x00>`` envelope so a name that is a
    substring of another record's name (WareHouse in CampWareHouse) can't be
    mislocated.
    """
    nb = name.encode("utf-8")
    n = len(nb)
    i = 0
    while True:
        j = body.find(nb, i)
        if j < 0:
            return None
        if (j >= 6
                and struct.unpack_from("<I", body, j - 4)[0] == n
                and j + n < len(body)
                and body[j + n] == 0):
            return j - 6            # back up over <u16 eid><u32 name_len>
        i = j + 1


def build_inventory_changes(
    vanilla_body: bytes, vanilla_header: bytes, intents: list
) -> tuple[list[dict], list[tuple[object, str]]]:
    """Apply inventory slot-count set intents. Returns ``(changes, dropped)``
    where each change is ``{offset, original, patched}`` over the 6-byte slot
    block of one record; ``dropped`` is ``(intent, reason)`` for the rest. No
    .pabgh companion (length-preserving)."""
    dropped: list[tuple[object, str]] = []
    by_rec: dict[tuple[int, int], list[tuple[int, int]]] = {}

    for i in intents:
        field = getattr(i, "field", "") or ""
        off = _SLOT_FIELD_OFFSET.get(field)
        if off is None:
            dropped.append((i, f"field {field!r} is not an inventory slot "
                               f"field (default/max/need_save_slot_count)"))
            continue
        if (getattr(i, "op", "set") or "set") != "set":
            dropped.append((i, f"op {getattr(i, 'op', None)!r} not supported "
                               f"for inventory (only 'set')"))
            continue
        entry = getattr(i, "entry", "") or ""
        if not entry:
            dropped.append((i, "inventory intent has no entry (the inventory "
                               "record name, e.g. 'CampWareHouse')"))
            continue
        val = getattr(i, "new", None)
        if not isinstance(val, int) or isinstance(val, bool) \
                or not 0 <= val <= _U16_MAX:
            dropped.append((i, f"value {val!r} is out of the u16 range "
                               f"0..{_U16_MAX} for an inventory slot count"))
            continue
        rs = _find_record_start(vanilla_body, entry)
        if rs is None:
            dropped.append((i, f"no inventory record named {entry!r}"))
            continue
        m = vanilla_body.find(_MARK, rs)
        if m < 0:
            dropped.append((i, f"inventory record {entry!r}: slot marker "
                               f"not found"))
            continue
        by_rec.setdefault((rs, m), []).append((off, val))

    changes: list[dict] = []
    for (rs, m), writes in by_rec.items():
        blk_start = m - 6
        original = vanilla_body[blk_start:m]      # the 6-byte slot block
        new_blk = bytearray(original)
        for off, val in writes:
            struct.pack_into("<H", new_blk, off + 6, val)  # off in -6/-4/-2
        if bytes(new_blk) == original:
            continue                               # every write was a no-op
        changes.append({
            "offset": blk_start,
            "original": original.hex(),
            "patched": bytes(new_blk).hex(),
        })
    return changes, dropped
