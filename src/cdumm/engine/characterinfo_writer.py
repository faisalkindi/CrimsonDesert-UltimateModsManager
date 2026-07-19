"""characterinfo.pabgb field writer for Format 3 mods (GitHub #150).

Female Animations (and similar character-swap mods) ship Format 3
intents targeting characterinfo.pabgb with five fields:

  upper_chart.group_lookup   u32  the upper action-chart package hash
  lower_chart.group_lookup   u32  the lower action-chart package hash
  skeleton_name              u32  the skeleton package hash
  lookup_25                  u32  the skeleton-variation package hash
  flag_c                     u8   a 0/1/2 enum in the post-block run

CDUMM's characterinfo PABGB schema is a positional, name-less
decompiled structure, so the generic Format 3 writer cannot resolve a
write position from a field name. All five fields sit at fixed
offsets inside (or just past) the action-chart / skeleton block, and
that block is located per record by the characterinfo parser walk.

The field-to-slot mapping was verified against vanilla 1.07.00, not
guessed: the Damian record holds the exact four u32 hashes the mod
copies onto Kliff, one per slot, and the flag_c slot holds only 0/1/2
across all 7027 records with Damian holding 2 (the value the mod
sets). See GitHub #150.

Every field is a fixed-size primitive, so each intent becomes one
absolute-offset replace; no record ever changes size and the
companion .pabgh never needs rebuilding.
"""
from __future__ import annotations

import logging
import struct

from cdumm.archive.format_parsers.characterinfo_full_parser import (
    parse_entry,
    parse_pabgh_index,
)

logger = logging.getLogger(__name__)

# The action-chart block start; hash-block fields are written at a fixed byte
# delta from it. Located per record by the parser walk.
_BLK = "_upperActionChartPackageGroupName_offset"

# Mod field name -> (parse_entry offset key, byte delta, struct format, width).
# The absolute write offset is ``record[offset_key] + delta``.
#
# LEGACY DMM naming (GitHub #150 / #192), kept byte-for-byte to preserve
# behaviour for mods already in the wild.
_FIELD_MAP: dict[str, tuple[str, int, str, int]] = {
    "upper_chart.group_lookup": (_BLK, 0, "<I", 4),
    "lower_chart.group_lookup":
        ("_lowerActionChartPackageGroupName_offset", 0, "<I", 4),
    "lookup_22": ("_appearanceName_stream_offset", 0, "<I", 4),
    "lookup_24": ("_characterPrefabPath_stream_offset", 0, "<I", 4),
    "skeleton_name": ("_skeletonName_offset", 0, "<I", 4),
    "lookup_25": ("_skeletonVariationName_offset", 0, "<I", 4),
    "flag_c": ("_flagC_offset", 0, "<B", 1),
}

# CURRENT DMM Mod Builder naming (Character Creator / Female Animations 7.6,
# GitHub #302). DMM renamed the action-chart slots between versions, so the
# same field names resolve to DIFFERENT block offsets than the legacy set.
# The mod copies the Damian record onto Kliff, and Damian holds each target
# value at these exact block deltas, verified against the live 1.13/1.14
# table: appearance_name=+0, character_prefab_path=+4, skeleton_name=+8,
# lookup_24=+20, lookup_25=+24. block+16 is a table-wide constant type-tag
# (3938836851 across all 7105 records), so the legacy lookup_24->+16 mapping
# wrote to a constant; the current schema routes lookup_24 to its real slot
# (+20). The three post-block fields the 7.6 mod also sets
# (default_action_action_index, character_weight, f36) sit in the stretch
# Pearl Abyss made variable-length in 1.13; their offset drifts per record
# and is deliberately NOT mapped -- they report "could not locate" rather
# than being written to a guess.
_NEW_SCHEMA_MAP: dict[str, tuple[str, int, str, int]] = {
    **_FIELD_MAP,
    "appearance_name": (_BLK, 0, "<I", 4),
    "character_prefab_path": (_BLK, 4, "<I", 4),
    "skeleton_name": (_BLK, 8, "<I", 4),
    "lookup_24": (_BLK, 20, "<I", 4),
    "lookup_25": (_BLK, 24, "<I", 4),
}

# A characterinfo mod that uses either semantic name was exported by the
# current DMM Mod Builder, so the new block layout applies to the whole mod.
# Legacy mods never use these names and keep the old offsets.
_NEW_SCHEMA_MARKERS = frozenset({"appearance_name", "character_prefab_path"})

SUPPORTED_FIELDS = frozenset(_FIELD_MAP) | _NEW_SCHEMA_MARKERS


def build_characterinfo_changes(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: list[tuple[str, int, str, object]],
) -> list[dict]:
    """Resolve Format 3 characterinfo intents into v2 change dicts.

    ``intents`` is a list of (entry_name, key, field, new_value):
      * entry_name - the record's name (Format 3 mods locate by name).
      * key        - the numeric record key, or 0 when the mod omits it.
      * field      - one of SUPPORTED_FIELDS.
      * new_value  - the integer value to set.

    Returns one absolute-offset replace change per resolved intent.
    Intents whose field is unsupported, whose record cannot be found or
    parsed, or whose value does not fit the field width are dropped
    with a logged warning, never raising.
    """
    idx = parse_pabgh_index(vanilla_header)  # {key: record offset}
    order = sorted(idx.items(), key=lambda kv: kv[1])

    parsed: dict[int, dict] = {}
    name_to_key: dict[str, int] = {}
    for rank, (key, start) in enumerate(order):
        end = (order[rank + 1][1]
               if rank + 1 < len(order) else len(vanilla_body))
        rec = parse_entry(vanilla_body, start, end)
        if rec is None:
            continue
        parsed[key] = rec
        name = rec.get("name")
        if name:
            name_to_key.setdefault(name, key)

    # A mod that uses the current DMM semantic names resolves the shared
    # action-chart slots at different block offsets than legacy mods, so the
    # whole mod is interpreted under one schema or the other.
    fields_present = {field for _n, _k, field, _v in intents}
    field_map = (_NEW_SCHEMA_MAP
                 if _NEW_SCHEMA_MARKERS & fields_present else _FIELD_MAP)

    changes: list[dict] = []
    for entry_name, raw_key, field, new_value in intents:
        spec = field_map.get(field)
        if spec is None:
            logger.warning(
                "characterinfo: field %r is not supported, skipping",
                field)
            continue
        if isinstance(new_value, bool) or not isinstance(new_value, int):
            logger.warning(
                "characterinfo: intent %s on %r has non-integer value "
                "%r, skipping", field, entry_name, new_value)
            continue
        key = name_to_key.get(entry_name)
        if key is None and raw_key:
            key = raw_key
        rec = parsed.get(key) if key is not None else None
        if rec is None:
            logger.warning(
                "characterinfo: entry %r (key=%r) not found or not "
                "parsable, skipping intent on %s",
                entry_name, raw_key, field)
            continue
        off_key, delta, fmt, width = spec
        base = rec.get(off_key)
        if base is None:
            logger.warning(
                "characterinfo: could not locate field %r for entry "
                "%r (record parsed only partially), skipping",
                field, entry_name)
            continue
        abs_off = base + delta
        if abs_off + width > len(vanilla_body):
            continue
        try:
            patched = struct.pack(fmt, new_value)
        except struct.error:
            logger.warning(
                "characterinfo: value %r is out of range for field "
                "%r (%d-byte), skipping", new_value, field, width)
            continue
        original = bytes(vanilla_body[abs_off:abs_off + width])
        changes.append({
            "offset": abs_off,
            "original": original.hex(),
            "patched": patched.hex(),
            "label": f"{entry_name}.{field}",
        })
    return changes
