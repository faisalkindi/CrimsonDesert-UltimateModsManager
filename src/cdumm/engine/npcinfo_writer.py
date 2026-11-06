"""CDUMM-native writer for npcinfo.pabgb dye lists.

GitHub #393 (delichandelarosse): donr484's "Dye Hard" (Nexus 3270)
unlocks every dye colour group at all ten world Dyers by setting two
list fields on each Dyer's NpcInfo record:

    dye_color_group_data_list  -> engine ``_dyeColorGroupDataList``
    dye_texture_set_data_list  -> engine ``_dyeTextureSetDataList``

CDUMM had no writer for npcinfo, so every intent was skipped.

LAYOUT (derived on CD 2.00.01, buildid 24934353, by exact tiling)
------------------------------------------------------------------
Entry: u32 key, u32 name_len, name, NUL, payload.

The two lists sit at the END of the payload, after a run of four
"condition" blobs that every Dyer (and 452 of the 542 NPCs) carries::

    [u32 tag][u8 0][u32 npc_key][u32 len][len chars]   x4

then::

    u32  pre                              (0 on every Dyer)
    u32  n_groups
    n_groups  x  { u32 dye_color_group_key ; u32 dye_target_key }
    u32  n_texsets
    n_texsets x  { u16 texture_set_lookup  ; u32 dye_target_key }
    4 trailing bytes                      (0 on every Dyer)

Evidence the element sizes are right, not merely plausible:

* Every one of the 452 four-blob NPCs tiles to exactly 4 trailing
  bytes with these sizes: 441 with (0,0), the 10 world Dyers with
  (1,1), and NHM_Unique_Oliver_649_npc (the Camp Dyer) with (10,10).
  The (10,10) entry is the decisive one -- ten 8-byte and ten 6-byte
  elements landing on the same 4-byte tail rules out any other split.
* Oliver's ten group keys are, in order, exactly the ten values Dye
  Hard writes to each world Dyer (3363967477, 3693560950, ...), and
  his texture lookups are 1..10, the same values the mod assigns to
  the ten Dyers. His ``dye_target_key`` values are 1000091..1000100
  (the "found this Dyer" unlock keys); the mod writes 0, which is how
  the Camp Dyer's rows become unconditional.
* Each world Dyer's single vanilla group key is the mod's value for
  that Dyer, and its texture lookup matches too (Theoric 1,
  Montpellier 2, ... Alteron 10, Cormar 9, Dverick 8).

A ``texture_set_lookup`` above 65535 cannot be represented and is
refused rather than truncated.

Safety stance mirrors the storeinfo writer: the lists are LOCATED by
walking the four tagged blobs and then checked to consume the payload
to exactly 4 trailing bytes. An entry that does not tile that way is
refused (``NpcinfoWriteRefused``), never best-effort patched -- a
malformed npcinfo record can crash the game at the NPC.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from cdumm.semantic.parser import _parse_entry_header, parse_pabgh_index
from cdumm.semantic.record_header import is_name_first, name_first_refusal

logger = logging.getLogger(__name__)

GROUP_FIELD = "dye_color_group_data_list"
TEXSET_FIELD = "dye_texture_set_data_list"
SUPPORTED_FIELDS = (
    GROUP_FIELD, "_dyeColorGroupDataList",
    TEXSET_FIELD, "_dyeTextureSetDataList",
)
_GROUP_ELEM = struct.Struct("<II")   # dye_color_group_key, dye_target_key
_TEXSET_ELEM = struct.Struct("<HI")  # texture_set_lookup, dye_target_key
_TAG_BLOBS = 4
_TRAILING = 4
_MAX_LIST = 64


class NpcinfoWriteRefused(ValueError):
    """Raised when an entry cannot be rewritten safely."""


@dataclass
class DyeLists:
    list_start: int          # absolute offset of n_groups
    list_end: int            # absolute offset of the 4 trailing bytes
    groups: list[tuple[int, int]]
    texsets: list[tuple[int, int]]


def _walk_tagged_blobs(body: bytes, payload: int, entry_end: int,
                       key: int) -> int | None:
    """Return the absolute offset just past the 4th tagged condition
    blob, or None if the entry does not carry exactly four."""
    pos = payload
    hits = 0
    last_end = None
    while pos < entry_end - 13:
        if body[pos + 4] == 0 and struct.unpack_from(
                "<I", body, pos + 5)[0] == key:
            slen = struct.unpack_from("<I", body, pos + 9)[0]
            if 0 < slen < 64 and pos + 13 + slen <= entry_end:
                hits += 1
                pos += 13 + slen
                last_end = pos
                continue
        pos += 1
    return last_end if hits == _TAG_BLOBS else None


def locate_dye_lists(body: bytes, payload: int, entry_end: int,
                     key: int) -> DyeLists:
    """Find and decode both dye lists of one npcinfo entry."""
    after = _walk_tagged_blobs(body, payload, entry_end, key)
    if after is None:
        raise NpcinfoWriteRefused(
            f"npc {key}: entry does not carry the four tagged condition "
            f"blobs the dye-list layout is anchored on; refusing")
    q = after + 4  # skip ``pre``
    try:
        n_groups = struct.unpack_from("<I", body, q)[0]
        list_start = q
        q += 4
        if n_groups > _MAX_LIST:
            raise NpcinfoWriteRefused(
                f"npc {key}: implausible group count {n_groups}")
        groups = [_GROUP_ELEM.unpack_from(body, q + i * _GROUP_ELEM.size)
                  for i in range(n_groups)]
        q += n_groups * _GROUP_ELEM.size
        n_texsets = struct.unpack_from("<I", body, q)[0]
        q += 4
        if n_texsets > _MAX_LIST:
            raise NpcinfoWriteRefused(
                f"npc {key}: implausible texture-set count {n_texsets}")
        texsets = [_TEXSET_ELEM.unpack_from(body, q + i * _TEXSET_ELEM.size)
                   for i in range(n_texsets)]
        q += n_texsets * _TEXSET_ELEM.size
    except struct.error as e:
        raise NpcinfoWriteRefused(
            f"npc {key}: dye lists overrun the entry ({e})") from e
    if entry_end - q != _TRAILING:
        raise NpcinfoWriteRefused(
            f"npc {key}: dye lists leave {entry_end - q} trailing byte(s), "
            f"expected {_TRAILING}; layout does not match, refusing")
    return DyeLists(list_start, q, groups, texsets)


def serialize_dye_lists(groups: list[tuple[int, int]],
                        texsets: list[tuple[int, int]]) -> bytes:
    out = bytearray()
    out += struct.pack("<I", len(groups))
    for k, t in groups:
        out += _GROUP_ELEM.pack(k, t)
    out += struct.pack("<I", len(texsets))
    for lk, t in texsets:
        out += _TEXSET_ELEM.pack(lk, t)
    return bytes(out)


def _u32(v, what: str, key: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 0xFFFFFFFF:
        raise NpcinfoWriteRefused(f"npc {key}: {what} must be a u32, got {v!r}")
    return v


def _groups_from_json(items, key: int) -> list[tuple[int, int]]:
    out = []
    for i, j in enumerate(items):
        if not isinstance(j, dict):
            raise NpcinfoWriteRefused(
                f"npc {key}: {GROUP_FIELD}[{i}] is not an object")
        gk = j.get("dye_color_group_key", j.get("_dyeColorGroupInfo"))
        tk = j.get("dye_target_key", j.get("_condition", 0))
        out.append((_u32(gk, f"{GROUP_FIELD}[{i}].dye_color_group_key", key),
                    _u32(tk, f"{GROUP_FIELD}[{i}].dye_target_key", key)))
    return out


def _texsets_from_json(items, key: int) -> list[tuple[int, int]]:
    out = []
    for i, j in enumerate(items):
        if not isinstance(j, dict):
            raise NpcinfoWriteRefused(
                f"npc {key}: {TEXSET_FIELD}[{i}] is not an object")
        lk = j.get("texture_set_lookup", j.get("_dyeTextureSetKey"))
        tk = j.get("dye_target_key", j.get("_condition", 0))
        lk = _u32(lk, f"{TEXSET_FIELD}[{i}].texture_set_lookup", key)
        if lk > 0xFFFF:
            raise NpcinfoWriteRefused(
                f"npc {key}: texture_set_lookup {lk} exceeds the u16 the "
                f"engine stores; refusing rather than truncating")
        out.append((lk, _u32(tk, f"{TEXSET_FIELD}[{i}].dye_target_key", key)))
    return out


def build_npcinfo_changes(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: list,
) -> tuple[list[dict], dict | None]:
    """Resolve Format 3 dye-list intents into v2 change dicts.

    Returns ``(pabgb_changes, pabgh_change)`` exactly like the storeinfo
    writer: absolute-offset replaces for the .pabgb plus a whole-body
    .pabgh rewrite when entry offsets shift.
    """
    # Game 2.04's name-first records are refused outright: this writer only
    # knows the key-first layout (#493).
    if is_name_first(vanilla_body, vanilla_header, "npcinfo"):
        raise NpcinfoWriteRefused(name_first_refusal("npcinfo.pabgb"))
    key_size, offsets = parse_pabgh_index(vanilla_header, "npcinfo")
    if not offsets:
        logger.warning("npcinfo writer: could not parse pabgh index")
        return [], None
    sorted_offs = sorted(offsets.values()) + [len(vanilla_body)]

    name_to_key: dict[str, int] = {}
    for k, off in offsets.items():
        _eid, ename, _payload = _parse_entry_header(vanilla_body, off, key_size)
        if ename:
            name_to_key.setdefault(ename, k)

    # key -> ordered ops: ("groups"|"texsets", "set", [elems]) or
    # ("groups"|"texsets", "append", elem). Applied in order on top of
    # the vanilla lists (#191: the Dye Addon appends 9 groups + 9
    # texture sets to each Dyer instead of setting the whole list).
    per_key: dict[int, list[tuple]] = {}
    for it in intents:
        field = (getattr(it, "field", "") or "").strip()
        if field not in SUPPORTED_FIELDS:
            logger.warning("npcinfo writer: unsupported field %r, skipping", field)
            continue
        op = (getattr(it, "op", "set") or "set")
        new = getattr(it, "new", None)
        key = getattr(it, "key", None)
        if op == "set" and not isinstance(new, list):
            logger.warning("npcinfo writer: malformed set (key=%r), skipping", key)
            continue
        if op == "array_append" and not isinstance(new, dict):
            logger.warning("npcinfo writer: array_append element on key=%r is "
                           "not an object, skipping", key)
            continue
        if op not in ("set", "array_append"):
            logger.warning("npcinfo writer: unsupported op %r, skipping", op)
            continue
        if not isinstance(key, int):
            logger.warning("npcinfo writer: malformed intent (key=%r), skipping", key)
            continue
        if key not in offsets:
            resolved = name_to_key.get(getattr(it, "entry", "") or "")
            if resolved is None:
                logger.warning("npcinfo writer: npc key %d / entry %r not in "
                               "table, skipping", key, getattr(it, "entry", ""))
                continue
            key = resolved
        slot = "groups" if field in (GROUP_FIELD, "_dyeColorGroupDataList") else "texsets"
        per_key.setdefault(key, []).append(
            (slot, "set" if op == "set" else "append", new))
    if not per_key:
        return [], None

    replacements: dict[int, tuple[int, int, bytes]] = {}
    for key, ops in per_key.items():
        off = offsets[key]
        entry_end = sorted_offs[sorted_offs.index(off) + 1]
        _, _, payload = _parse_entry_header(vanilla_body, off, key_size)
        van = locate_dye_lists(vanilla_body, payload, entry_end, key)
        groups = list(van.groups)
        texsets = list(van.texsets)
        for slot, kind, val in ops:
            conv = _groups_from_json if slot == "groups" else _texsets_from_json
            if kind == "set":
                new_list = conv(val, key)
            else:
                new_list = (groups if slot == "groups" else texsets) + conv([val], key)
            if slot == "groups":
                groups = new_list
            else:
                texsets = new_list
        if len(groups) > _MAX_LIST or len(texsets) > _MAX_LIST:
            raise NpcinfoWriteRefused(
                f"npc {key}: resulting dye list exceeds {_MAX_LIST} entries")
        blob = serialize_dye_lists(groups, texsets)
        replacements[key] = (van.list_start, van.list_end, blob)
        logger.info(
            "npcinfo writer: npc %d dye lists groups %d -> %d, texsets %d -> %d "
            "(%+d bytes)", key, len(van.groups), len(groups),
            len(van.texsets), len(texsets),
            len(blob) - (van.list_end - van.list_start))

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
            "label": f"npc {key}.dye lists",
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
            "label": "npcinfo.pabgh offset rebuild",
        }
    return pabgb_changes, pabgh_change
