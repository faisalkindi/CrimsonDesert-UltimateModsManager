"""CDUMM-native writer for dyecolorgroupinfo.pabgb colour lists.

GitHub #191 (Damascinas): AerowynX's "Expanded Vendor Inventory Rebuilt
V3 Dye Addon" (Nexus 3290) appends 22 colours to each of the ten dye
colour groups via ``array_append`` on ``dye_color_data_list`` (engine
``_dyeColorDataList``). CDUMM had no writer for the table.

LAYOUT (CD 2.00.01, buildid 24994088, exact tiling on all 10 groups)
-------------------------------------------------------------------
Entry: u32 key, u32 name_len, name, NUL, payload.

The colour list is the FIRST thing in the payload (schema declares
``_dyeColorDataList`` first, and the bytes agree)::

    u32 count
    count x { u32 raw_color (RGBA bytes, little-endian) ; u32 texture_lookup }
    tail (33-37 bytes: blocked flag, icon path, group name string, string key)

Evidence: every vanilla group has exactly 109 colours; 109 x 8 + 4 lands
on a tail whose only variation is the length of the group's name
string. The mod's element ``{"raw_color": 4278255513, "texture_lookup":
1008648}`` is the same two u32s (the vanilla elements of the same group
carry texture_lookup 1008647..1008652 in step).

The tail is carried verbatim; only the list is rebuilt. An entry whose
count does not tile to the payload (count*8 + 4 > payload) is refused.
"""
from __future__ import annotations

import logging
import struct

from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index
from cdumm.semantic.record_header import is_name_first, name_first_refusal

logger = logging.getLogger(__name__)

FIELD = "dye_color_data_list"
SUPPORTED_FIELDS = (FIELD, "_dyeColorDataList")
_ELEM = struct.Struct("<II")   # raw_color, texture_lookup
_MAX_LIST = 4096
_MIN_TAIL = 8


class DyecolorgroupinfoWriteRefused(ValueError):
    """Raised when an entry cannot be rewritten safely."""


def locate_color_list(body: bytes, payload: int, entry_end: int,
                      key: int) -> tuple[int, int, list[tuple[int, int]]]:
    """Return ``(list_start, list_end, elements)`` for one entry."""
    try:
        n = struct.unpack_from("<I", body, payload)[0]
    except struct.error as e:
        raise DyecolorgroupinfoWriteRefused(
            f"dye group {key}: payload too short ({e})") from e
    end = payload + 4 + n * _ELEM.size
    if n > _MAX_LIST or end > entry_end - _MIN_TAIL:
        raise DyecolorgroupinfoWriteRefused(
            f"dye group {key}: colour count {n} does not tile the entry "
            f"(payload {entry_end - payload} bytes); refusing")
    elems = [_ELEM.unpack_from(body, payload + 4 + i * _ELEM.size)
             for i in range(n)]
    return payload, end, elems


def serialize_color_list(elems: list[tuple[int, int]]) -> bytes:
    out = bytearray(struct.pack("<I", len(elems)))
    for c, t in elems:
        out += _ELEM.pack(c, t)
    return bytes(out)


def _u32(v, what: str, key: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0xFFFFFFFF:
        raise DyecolorgroupinfoWriteRefused(
            f"dye group {key}: {what} must be a u32, got {v!r}")
    return v


def _elems_from_json(items, key: int) -> list[tuple[int, int]]:
    out = []
    for i, j in enumerate(items):
        if not isinstance(j, dict):
            raise DyecolorgroupinfoWriteRefused(
                f"dye group {key}: {FIELD}[{i}] is not an object")
        c = j.get("raw_color", j.get("_color"))
        t = j.get("texture_lookup", j.get("_condition"))
        out.append((_u32(c, f"{FIELD}[{i}].raw_color", key),
                    _u32(t, f"{FIELD}[{i}].texture_lookup", key)))
    return out


def build_dyecolorgroupinfo_changes(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: list,
) -> tuple[list[dict], dict | None]:
    """Resolve Format 3 colour-list intents (set / array_append) into v2
    change dicts plus a .pabgh rewrite when entries grow."""
    # Game 2.04's name-first records are refused outright: this writer only
    # knows the key-first layout (#493).
    if is_name_first(vanilla_body, vanilla_header, "dyecolorgroupinfo"):
        raise DyecolorgroupinfoWriteRefused(
            name_first_refusal("dyecolorgroupinfo.pabgb"))
    key_size, offsets = parse_pabgh_index(vanilla_header, "dyecolorgroupinfo")
    if not offsets:
        logger.warning("dyecolorgroupinfo writer: could not parse pabgh index")
        return [], None
    sorted_offs = sorted(offsets.values()) + [len(vanilla_body)]

    name_to_key: dict[str, int] = {}
    for k, off in offsets.items():
        _eid, ename, _payload = _parse_entry_header(vanilla_body, off, key_size)
        if ename:
            name_to_key.setdefault(ename, k)

    per_key: dict[int, list[tuple]] = {}
    for it in intents:
        field = (getattr(it, "field", "") or "").strip()
        if field not in SUPPORTED_FIELDS:
            logger.warning("dyecolorgroupinfo writer: unsupported field %r, "
                           "skipping", field)
            continue
        op = (getattr(it, "op", "set") or "set")
        new = getattr(it, "new", None)
        key = getattr(it, "key", None)
        if op == "set" and not isinstance(new, list):
            logger.warning("dyecolorgroupinfo writer: malformed set (key=%r)", key)
            continue
        if op == "array_append" and not isinstance(new, dict):
            logger.warning("dyecolorgroupinfo writer: array_append element on "
                           "key=%r is not an object", key)
            continue
        if op not in ("set", "array_append"):
            logger.warning("dyecolorgroupinfo writer: unsupported op %r", op)
            continue
        if not isinstance(key, int):
            continue
        if key not in offsets:
            resolved = name_to_key.get(getattr(it, "entry", "") or "")
            if resolved is None:
                logger.warning("dyecolorgroupinfo writer: key %r / entry %r not "
                               "in table, skipping", key, getattr(it, "entry", ""))
                continue
            key = resolved
        per_key.setdefault(key, []).append((op, new))
    if not per_key:
        return [], None

    replacements: dict[int, tuple[int, int, bytes]] = {}
    for key, ops in per_key.items():
        off = offsets[key]
        entry_end = sorted_offs[sorted_offs.index(off) + 1]
        _, _, payload = _parse_entry_header(vanilla_body, off, key_size)
        start, end, elems = locate_color_list(vanilla_body, payload, entry_end, key)
        cur = list(elems)
        for op, val in ops:
            if op == "set":
                cur = _elems_from_json(val, key)
            else:
                cur = cur + _elems_from_json([val], key)
        if len(cur) > _MAX_LIST:
            raise DyecolorgroupinfoWriteRefused(
                f"dye group {key}: resulting colour list has {len(cur)} "
                f"entries, refusing")
        blob = serialize_color_list(cur)
        replacements[key] = (start, end, blob)
        logger.info("dyecolorgroupinfo writer: group %d colours %d -> %d (%+d bytes)",
                    key, len(elems), len(cur), len(blob) - (end - start))

    pabgb_changes: list[dict] = []
    deltas: list[tuple[int, int]] = []
    for key in sorted(replacements, key=lambda k: replacements[k][0]):
        start, end, blob = replacements[key]
        if vanilla_body[start:end] == blob:
            continue
        pabgb_changes.append({
            "offset": start,
            "original": vanilla_body[start:end].hex(),
            "patched": blob.hex(),
            "label": f"dye group {key}.{FIELD}",
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
            "label": "dyecolorgroupinfo.pabgh offset rebuild",
        }
    return pabgb_changes, pabgh_change
