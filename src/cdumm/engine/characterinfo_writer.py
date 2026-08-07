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
    # GitHub #192 (Yorivel): mesh / visual-swap mods set the appearance
    # hash and the model-path hash. Both are plain u32 name-hash slots
    # in the same action-chart block (block+12 / block+16), located by
    # the same parser walk as the four #150 u32 fields.
    "lookup_22": ("_appearanceName_stream_offset", 0, "<I", 4),
    "lookup_24": ("_characterPrefabPath_stream_offset", 0, "<I", 4),
    "skeleton_name": ("_skeletonName_offset", 0, "<I", 4),
    "lookup_25": ("_skeletonVariationName_offset", 0, "<I", 4),
    "flag_c": ("_flagC_offset", 0, "<B", 1),
    # GitHub #302: the two post-bool-block fields the Character Creator 7.6
    # mod also sets. The parser now WALKS to them (variable-length bool run
    # -> 900000 anchor -> count-driven list -> these two u32s) and only
    # publishes the offsets when its f32-2.0 gate confirms the position, so
    # a record whose layout can't be verified simply has no offset key here
    # and is refused by name below -- never written at a guessed position.
    "default_action_action_index": (
        "_defaultActionActionIndex_offset", 0, "<I", 4),
    # `f36` is the record's GENDER byte, at block+66. It is NOT the u32 the
    # parser publishes as `_f36_offset` (GitHub #302).
    #
    # That earlier mapping was added for this very mod and was never
    # confirmed in-game -- the mod has never once worked. Four independent
    # measurements say it is the wrong slot:
    #
    # 1. Field correspondence. Character Creator ships the same edit twice,
    #    as this Format 3 file and as a raw offset patch with hand-written
    #    labels. The two agree field for field on all five records
    #    (7/6/6/3/3). Every field matches by exact value except one; once
    #    the rest are paired off, the leftover is `f36` here and `_gender`
    #    there, both set to 2.
    # 2. Position. That patch puts `_gender` one byte at block+66. On the
    #    live table that byte still reads 01 on Kliff / Kliff_AI / Yann,
    #    exactly the "before" value the patch expects for its 01 -> 02 edit.
    # 3. Table-wide shape. Across all 7244 records block+66 holds only
    #    0, 1 or 2 (1357 / 4909 / 978) -- a clean enum on every record.
    #    `_f36_offset` is published on 142 records, i.e. 2% of the table.
    # 4. Intent. Read as gender the mod sets 1 -> 2 on every record it
    #    touches. Read as `_f36` it writes 2 over an existing 2 on Kliff_AI
    #    and PlayerAll, so two of the four edits would be no-ops -- not
    #    something a mod author writes on purpose.
    #
    # Block-relative also means it is placeable on every record, so records
    # the post-block walk cannot reach are no longer refused over this field.
    "f36": (_BLK, 66, "<B", 1),
    # `character_weight` is the SAME SLOT under a different DMM name, and
    # that is documented by the mod author rather than inferred. Character
    # Creator 7.5 shipped as raw offset patches with hand-written labels;
    # for the record 7.6 calls `character_weight` (Kliff_Clone), writing
    # the same value 1287066785, 7.5's label reads:
    #     offset=4503  "Kliff_Clone _defaultActionActionIndex -> ..."
    # The other three records carry the same value under the name
    # `default_action_action_index` in 7.6 and the identical
    # `_defaultActionActionIndex` label in 7.5, so the two names are one
    # field renamed between DMM versions. Mapping it here means the write
    # still only happens where the parser's gate has confirmed the
    # position; on a record it cannot locate, this is refused like any
    # other unlocatable field.
    "character_weight": ("_defaultActionActionIndex_offset", 0, "<I", 4),
    # DMM "no dragon / companion re-summon cooldown" mods set this to 1 on
    # mount/companion records (Riding_Dragon_1, Kliff, Damian, ...). It is a
    # u64 re-summon cooldown the parser already decodes (only summonable
    # entities have a nonzero value; ordinary NPCs read 0). Adding it here
    # enables both validation (_CHARACTERINFO_FIELDS is this same set) and the
    # in-place, length-preserving write.
    #
    # Byte delta 0: the parser publishes this offset immediately before its
    # own `<Q` read at the same position, so the write lands where the read
    # does. Verified in review against a measured run (change offset 64 vs
    # parser offset 64). It belongs in _FIELD_MAP rather than
    # _NEW_SCHEMA_MAP so legacy-vintage mods keep it -- SUPPORTED_FIELDS
    # derives from _FIELD_MAP and picks it up automatically.
    "call_mercenary_cool_time": ("_callMercenaryCoolTime_offset", 0, "<Q", 8),
    # The sibling slot the same mods set, and the reason "No CD Mount"
    # QoL mods only half-worked: cool_time applied, spawn_duration was
    # refused as an unsupported field name. The parser already decodes it
    # (`<Q` immediately after cool_time) and publishes its offset, so this
    # is the same length-preserving write as its sibling.
    #
    # Verified on live 1.15 rather than assumed from adjacency. Across all
    # 7105 records the offset is published for every one, and the slot is
    # nonzero on exactly 14 -- every one a summonable mount
    # (Riding_ATAG_*, Riding_Dragon_1, Riding_WarMachine_Unique_1), values
    # 600 and 9000, the same units and shape as cool_time's 300/600/3600/
    # 9000. Ordinary NPCs read 0, exactly as cool_time does. All 88
    # mercenary intents in the real QoL mod place inside their own record.
    "call_mercenary_spawn_duration": (
        "_callMercenarySpawnDuration_offset", 0, "<Q", 8),
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
# Pearl Abyss made variable-length in 1.13, so their offset drifts per
# record. They ARE mapped -- inherited from _FIELD_MAP above -- but only
# to a parser offset key the walk publishes when its f32-2.0 gate confirms
# the position, so a record whose layout can't be verified reports "could
# not locate" rather than being written to a guess. Measured on live 1.15:
# the walk reaches them on Damian/Kliff/Kliff_AI/PlayerAll and does not on
# the *_Clone records, whose partial writes GitHub #329 now abandons.
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
    refusals_out: list[str] | None = None,
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

    ALL-OR-NOTHING PER RECORD (GitHub #329). A character-swap mod copies
    one character's appearance, model, skeleton and action-chart index
    onto another as a set. If CDUMM can locate the record and models
    every field the mod names, but the parser cannot publish a write
    position for some of them, writing the rest leaves that record
    holding the source character's model driven by the target's own
    action data -- a combination neither vanilla nor the mod produces,
    and one the game does not survive. Such a record is abandoned whole:
    none of its fields are written and it stays vanilla, which is always
    self-consistent.

    This is deliberately scoped to fields CDUMM *models* but cannot
    *place on this record*. A field name CDUMM doesn't model at all is a
    property of the mod, identical on every record, so every record gets
    the same subset -- that is the behaviour #150/#192/#302 shipped and
    users confirmed in-game, and it is left alone.

    ``refusals_out``, when given, collects one human-readable line per
    abandoned record so a caller can surface it to the user; the reason
    is also logged at WARNING either way.
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
    # Entry name per emitted change, index-aligned with ``changes``. Used to
    # drop a partially-written record's changes below. Deriving the name from
    # the ``label`` instead would be wrong: field names contain dots
    # ('upper_chart.group_lookup'), so splitting a label can't recover the
    # entry name unambiguously.
    change_owner: list[str] = []
    # Per record: {field: parser offset key} CDUMM models, and the subset of
    # those field names it actually placed.
    modelled: dict[str, dict[str, str]] = {}
    placed: dict[str, set[str]] = {}
    for entry_name, raw_key, field, new_value in intents:
        spec = field_map.get(field)
        if spec is None:
            logger.warning(
                "characterinfo: field %r is not supported, skipping",
                field)
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
        # From here the record exists and CDUMM models this field, so the
        # record is expected to carry this write. Any drop past this point
        # unbalances the record.
        off_key, delta, fmt, width = spec
        modelled.setdefault(entry_name, {})[field] = off_key
        # The type check sits BELOW the registration above deliberately. A
        # bad value is still a field the mod asked this record to carry, so
        # dropping it must abandon the record like any other unwritable
        # field. Checking before registration let a non-integer escape the
        # all-or-nothing guard entirely: the other fields were written and
        # the record ended up half-modded, which is exactly the crash this
        # writer exists to prevent.
        if isinstance(new_value, bool) or not isinstance(new_value, int):
            logger.warning(
                "characterinfo: intent %s on %r has non-integer value "
                "%r, skipping", field, entry_name, new_value)
            continue
        base: int | None = rec.get(off_key)
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
        placed.setdefault(entry_name, set()).add(field)
        change_owner.append(entry_name)
        changes.append({
            "offset": abs_off,
            "original": original.hex(),
            "patched": patched.hex(),
            "label": f"{entry_name}.{field}",
        })

    # All-or-nothing per record (GitHub #329).
    #
    # Only a field the parser CAN publish somewhere in this table counts. A
    # field whose offset key the parser publishes for no record at all is a
    # table-wide gap, not per-record drift: every targeted record then gets
    # the same subset, so none is inconsistent relative to its siblings.
    # ``flag_c`` is the live example -- the 1.13 re-port stopped resolving
    # ``_flagC_offset`` entirely, and #150's mod has shipped and been
    # confirmed in-game writing the remaining four fields. Treating that as
    # a partial record would silently stop those mods working.
    #
    # ``character_weight`` is why this is keyed on the parser offset key
    # rather than the field name: it is the same slot as
    # ``default_action_action_index`` under a different DMM name, so asking
    # for one and placing the other still counts as the same field.
    wanted_keys = {k for f in modelled.values() for k in f.values()}
    publishable = {
        k for k in wanted_keys
        if any(r.get(k) is not None for r in parsed.values())
    }
    partial: dict[str, list[str]] = {}
    for name, fields in modelled.items():
        if not placed.get(name):
            continue
        missing_fields = sorted(
            f for f, off_key in fields.items()
            if f not in placed[name] and off_key in publishable
        )
        if missing_fields:
            partial[name] = missing_fields
    for name in sorted(partial):
        missing = ", ".join(partial[name])
        msg = (
            f"characterinfo: record {name!r} left unwritten -- CDUMM could "
            f"not locate {missing} on it, and writing only the other "
            f"{len(placed[name])} field(s) would give it another "
            f"character's appearance driven by its own action data, which "
            f"crashes the game. This record stays vanilla; the mod's other "
            f"records still apply."
        )
        logger.warning("%s", msg)
        if refusals_out is not None:
            refusals_out.append(msg)
    if partial:
        changes = [
            c for c, owner in zip(changes, change_owner)
            if owner not in partial
        ]
    return changes
