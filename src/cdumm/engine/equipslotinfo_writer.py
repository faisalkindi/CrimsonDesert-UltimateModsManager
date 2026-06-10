"""Format 3 writer for equipslotinfo.pabgb ``entries[N].etl_hashes``
(GitHub #190, Character Creator's Female Rapier and Shield Module).

Layout (clean-room RE against the CD 1.10 vanilla file; every one of
the 14 entries round-trips byte-exact under this model, see
tests/test_equipslotinfo_writer.py):

entry payload (after u32 entry_id + u32 name_len + name + NUL):
    u16  unk                  (0..4 in vanilla; category/page index)
    u32  record_count
    record * record_count
    u32  footer_count         (0 in 13 of 14 entries; 5 in entry 701)
    footer_item * footer_count  (opaque 20 bytes each)
    u32  const 0xb954d87c     (entry terminator)

record:
    u32  etl_count
    u32 * etl_count           (the ``etl_hashes`` the mods set)
    66B  fixed block          (opaque; carries an 8-byte hash pair at
                               +26 that is constant in most records)

The DMM field path ``entries[N].etl_hashes`` addresses record N of the
store entry selected by the intent ``key``. Mods grow or shrink the
hash list of EXISTING records; the fixed 66-byte block is preserved
verbatim from vanilla, so nothing unmapped is ever synthesized.
"""
from __future__ import annotations

import logging
import re
import struct

from cdumm.semantic.parser import parse_pabgh_index, _parse_entry_header

logger = logging.getLogger(__name__)

_FIXED_BLOCK = 66
_FOOTER_ITEM = 20
_TERMINATOR = 0xB954D87C

_FIELD_RE = re.compile(r"^entries\[(\d+)\]\.etl_hashes$")


class EquipslotWriteRefused(ValueError):
    """The intent cannot be applied without risking a corrupt table."""


def parse_entry_records(body: bytes, payload: int, entry_end: int
                        ) -> tuple[int, list[tuple[int, list[int], bytes]],
                                   bytes]:
    """Parse one entry's payload.

    Returns ``(unk, records, footer)`` where each record is
    ``(etl_count, hashes, fixed_block)`` and ``footer`` is the raw
    bytes from footer_count through the terminator inclusive.
    Raises :class:`EquipslotWriteRefused` when the bytes do not match
    the verified model.
    """
    u16 = lambda p: struct.unpack_from("<H", body, p)[0]
    u32 = lambda p: struct.unpack_from("<I", body, p)[0]
    unk = u16(payload)
    count = u32(payload + 2)
    if not (0 <= count < 1000):
        raise EquipslotWriteRefused(
            f"implausible record count {count}")
    p = payload + 6
    records = []
    for i in range(count):
        if p + 4 > entry_end:
            raise EquipslotWriteRefused(f"record {i} overruns entry")
        c = u32(p)
        if c > 64:
            raise EquipslotWriteRefused(
                f"record {i}: implausible etl count {c} at {p}")
        if p + 4 + 4 * c + _FIXED_BLOCK > entry_end:
            raise EquipslotWriteRefused(f"record {i} overruns entry")
        hashes = [u32(p + 4 + 4 * j) for j in range(c)]
        fixed = body[p + 4 + 4 * c:p + 4 + 4 * c + _FIXED_BLOCK]
        records.append((c, hashes, fixed))
        p += 4 + 4 * c + _FIXED_BLOCK
    if p + 4 > entry_end:
        raise EquipslotWriteRefused("footer count overruns entry")
    fcount = u32(p)
    fend = p + 4 + fcount * _FOOTER_ITEM + 4
    if fend != entry_end:
        raise EquipslotWriteRefused(
            f"footer does not close the entry (footer_count={fcount}, "
            f"computed end {fend} vs entry end {entry_end})")
    if u32(fend - 4) != _TERMINATOR:
        raise EquipslotWriteRefused(
            f"entry terminator mismatch at {fend - 4}")
    footer = body[p:entry_end]
    return unk, records, footer


def serialize_entry_payload(unk: int,
                            records: list[tuple[int, list[int], bytes]],
                            footer: bytes) -> bytes:
    out = bytearray()
    out += struct.pack("<H", unk)
    out += struct.pack("<I", len(records))
    for _c, hashes, fixed in records:
        if len(fixed) != _FIXED_BLOCK:
            raise EquipslotWriteRefused(
                f"fixed block must be {_FIXED_BLOCK} bytes")
        out += struct.pack("<I", len(hashes))
        for h in hashes:
            out += struct.pack("<I", h & 0xFFFFFFFF)
        out += fixed
    out += footer
    return bytes(out)


def build_equipslotinfo_changes(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: list,
) -> tuple[list[dict], dict | None]:
    """Resolve ``entries[N].etl_hashes`` intents into v2 change dicts.

    Same contract as the multichangeinfo / storeinfo writers:
    ``(pabgb_changes, pabgh_change)``, mutually consistent. Entries can
    grow when a mod appends hashes, so the companion .pabgh offsets
    are rebuilt in the same pass.
    """
    key_size, offsets = parse_pabgh_index(vanilla_header, "equipslotinfo")
    if not offsets:
        logger.warning("equipslotinfo writer: pabgh parse failed")
        return [], None
    sorted_offs = sorted(offsets.values()) + [len(vanilla_body)]

    # Collect per (entry key) -> {record_index: new_hashes}.
    per_key: dict[int, dict[int, list[int]]] = {}
    for it in intents:
        field = (getattr(it, "field", "") or "").strip()
        m = _FIELD_RE.match(field)
        if m is None:
            logger.warning(
                "equipslotinfo writer: unsupported field %r, skipping",
                field)
            continue
        if (getattr(it, "op", "set") or "set") != "set":
            logger.warning(
                "equipslotinfo writer: unsupported op %r, skipping",
                getattr(it, "op", None))
            continue
        new = getattr(it, "new", None)
        key = getattr(it, "key", None)
        if (not isinstance(new, list)
                or not all(isinstance(v, int) for v in new)
                or not isinstance(key, int)):
            logger.warning(
                "equipslotinfo writer: malformed intent (key=%r), "
                "skipping", key)
            continue
        if key not in offsets:
            logger.warning(
                "equipslotinfo writer: entry key %d not in table, "
                "skipping", key)
            continue
        per_key.setdefault(key, {})[int(m.group(1))] = new

    if not per_key:
        return [], None

    replacements: dict[int, tuple[int, int, bytes]] = {}
    for key, idx_map in per_key.items():
        off = offsets[key]
        entry_end = sorted_offs[sorted_offs.index(off) + 1]
        _, _, payload = _parse_entry_header(vanilla_body, off, key_size)
        unk, records, footer = parse_entry_records(
            vanilla_body, payload, entry_end)
        for idx, hashes in idx_map.items():
            if not (0 <= idx < len(records)):
                raise EquipslotWriteRefused(
                    f"entry {key}: record index {idx} out of range "
                    f"(entry has {len(records)} records)")
            c, _old, fixed = records[idx]
            records[idx] = (len(hashes), list(hashes), fixed)
        new_payload = serialize_entry_payload(unk, records, footer)
        replacements[key] = (payload, entry_end, new_payload)
        logger.info(
            "equipslotinfo writer: entry %d, %d record(s) updated, "
            "%+d bytes", key, len(idx_map),
            len(new_payload) - (entry_end - payload))

    pabgb_changes: list[dict] = []
    deltas: list[tuple[int, int]] = []
    for key in sorted(replacements, key=lambda k: replacements[k][0]):
        start, end, blob = replacements[key]
        if vanilla_body[start:end] == blob:
            continue  # no-op set
        pabgb_changes.append({
            "offset": start,
            "original": vanilla_body[start:end].hex(),
            "patched": blob.hex(),
            "label": f"equipslot entry {key}.etl_hashes",
        })
        deltas.append((offsets[key], len(blob) - (end - start)))

    if not pabgb_changes:
        return [], None

    def shifted(off: int) -> int:
        s = off
        for at, d in deltas:
            if off > at:
                s += d
        return s

    new_header = bytearray(vanilla_header)
    count = struct.unpack_from("<H", vanilla_header, 0)[0]
    pos = 2
    changed = False
    for _ in range(count):
        eoff = struct.unpack_from("<I", vanilla_header, pos + key_size)[0]
        noff = shifted(eoff)
        if noff != eoff:
            struct.pack_into("<I", new_header, pos + key_size, noff)
            changed = True
        pos += key_size + 4
    pabgh_change = None
    if changed:
        pabgh_change = {
            "offset": 0,
            "original": vanilla_header.hex(),
            "patched": bytes(new_header).hex(),
            "label": "equipslotinfo.pabgh offset rebuild",
        }
    return pabgb_changes, pabgh_change
