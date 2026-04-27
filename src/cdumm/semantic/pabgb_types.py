"""PABGB binary type walker — consumes bytes for any PABGB field type.

Extends the schema-driven parser to handle the variable-length and
nested types that NattKh's `pabgb_complete_schema.json` leaves as
`stream=?`/`type=?`. Built by porting type definitions from Potter420's
`crimson-rs/src/item_info/structs.rs` and `item.rs` (MIT licensed,
credited in CRIMSON_DESERT_MODDING_BIBLE.md section 22).

The key entry point is :func:`consume_bytes`, which returns the number
of bytes a typed field occupies starting at a given offset, or None
when the bytes can't be safely walked (truncated, unknown sub-struct,
unsupported tagged variant value).

Type descriptor grammar (string-based, mirrors crimson-rs Rust syntax):

  Primitives        u8 i8 u16 i16 u32 i32 u64 i64 f32 f64
  CString           u32 length + UTF-8 bytes
  LocalizableString u8 category + u64 index + u32 default_len + bytes
  CArray<T>         u32 count + count * T
  COptional<T>      u8 flag + (T if flag != 0)
  [T;N]             N * T (fixed-length array)
  Substruct names   resolved via SUBSTRUCT_DEFS table below
  Tagged variants   resolved via TAGGED_VARIANT_DEFS table below

All `*Key` types from crimson-rs (ItemKey, CharacterKey, etc.) are u32
on the wire. `LocStringInfoKey` is also a u32 key. `InventoryKey` is u16.

Schema entries that use these new types are in
``schemas/pabgb_type_overrides.json``, which the schema loader merges
with NattKh's base schema at load time.
"""
from __future__ import annotations

import struct
from typing import Optional


# ── Primitive widths ────────────────────────────────────────────────────────

_PRIMITIVE_WIDTH: dict[str, int] = {
    "u8": 1, "i8": 1,
    "u16": 2, "i16": 2,
    "u32": 4, "i32": 4,
    "u64": 8, "i64": 8,
    "f32": 4, "f64": 8,
}


# ── Substruct definitions (ported from crimson-rs/src/item_info/structs.rs) ─
#
# Each entry: substruct_name -> list of (field_name, type_descriptor) tuples
# in the deserialization order from the Rust py_binary_struct! macro.
# Field names match crimson-rs naming (snake_case); descriptors use the
# grammar above.
#
# All *Key Rust types map to u32 except where noted (per crimson-rs/keys.rs):
#   InventoryKey = u16
#   Everything else (ItemKey, CharacterKey, ConditionKey, GimmickInfoKey,
#   StringInfoKey, LocStringInfoKey, KnowledgeKey, EffectKey, BuffKey,
#   SkillKey, ReserveSlotKey, MaterialMatchKey, MultiChangeKey, EquipTypeKey,
#   CategoryKey, StatusKey, ItemUseKey, ItemGroupKey, CraftToolKey,
#   GameAdviceInfoKey, MissionKey, TribeInfoKey, CharacterGroupKey) = u32

SUBSTRUCT_DEFS: dict[str, list[tuple[str, str]]] = {
    # Simple fixed-size sub-structs
    "OccupiedEquipSlotData": [
        ("equip_slot_name_key", "u32"),
        ("equip_slot_name_index_list", "CArray<u8>"),
    ],
    "ItemIconData": [
        ("icon_path", "u32"),
        ("check_exist_sealed_data", "u8"),
        ("gimmick_state_list", "CArray<u32>"),
    ],
    "PassiveSkillLevel": [
        ("skill", "u32"),
        ("level", "u32"),
    ],
    "ReserveSlotTargetData": [
        ("reserve_slot_info", "u32"),
        ("condition_info", "u32"),
    ],
    "SocketMaterialItem": [
        ("item", "u32"),
        ("value", "u64"),
    ],
    "EnchantStatChange": [
        ("stat", "u32"),
        ("change_mb", "i64"),
    ],
    "EnchantLevelChange": [
        ("stat", "u32"),
        ("change_mb", "i8"),
    ],
    "EnchantStatData": [
        ("max_stat_list", "CArray<EnchantStatChange>"),
        ("regen_stat_list", "CArray<EnchantStatChange>"),
        ("stat_list_static", "CArray<EnchantStatChange>"),
        ("stat_list_static_level", "CArray<EnchantLevelChange>"),
    ],
    "PriceFloor": [
        ("price", "u64"),
        ("sym_no", "u32"),
        ("item_info_wrapper", "u32"),
    ],
    "ItemPriceInfo": [
        ("key", "u32"),
        ("price", "PriceFloor"),
    ],
    "EquipmentBuff": [
        ("buff", "u32"),
        ("level", "u32"),
    ],
    "EnchantData": [
        ("level", "u16"),
        ("enchant_stat_data", "EnchantStatData"),
        ("buy_price_list", "CArray<ItemPriceInfo>"),
        ("equip_buffs", "CArray<EquipmentBuff>"),
    ],
    "GimmickVisualPrefabData": [
        ("tag_name_hash", "u32"),
        ("scale", "[f32;3]"),
        ("prefab_names", "CArray<u32>"),
        ("animation_path_list", "CArray<u32>"),
        ("use_gimmick_prefab", "u8"),
    ],
    "GameEventExecuteData": [
        ("game_event_type", "u8"),
        ("player_condition", "u32"),
        ("target_condition", "u32"),
        ("event_condition", "u32"),
    ],
    "InventoryChangeData": [
        ("game_event_execute_data", "GameEventExecuteData"),
        ("to_inventory_info", "u16"),  # InventoryKey = u16
    ],
    "PageData": [
        ("left_page_texture_path", "CString"),
        ("right_page_texture_path", "CString"),
        ("left_page_related_knowledge_info", "u32"),
        ("right_page_related_knowledge_info", "u32"),
    ],
    "InspectData": [
        ("item_info", "u32"),
        ("gimmick_info", "u32"),
        ("character_info", "u32"),
        ("spawn_reason_hash", "u32"),
        ("socket_name", "CString"),
        ("speak_character_info", "u32"),
        ("inspect_target_tag", "u32"),
        ("reward_own_knowledge", "u8"),
        ("reward_knowledge_info", "u32"),
        ("item_desc", "LocalizableString"),
        ("board_key", "u32"),
        ("inspect_action_type", "u8"),
        ("gimmick_state_name_hash", "u32"),
        ("target_page_index", "u32"),
        ("is_left_page", "u8"),
        ("target_page_related_knowledge_info", "u32"),
        ("enable_read_after_reward", "u8"),
        ("refer_to_left_page_inspect_data", "u8"),
        ("inspect_effect_info_key", "u32"),
        ("inspect_complete_effect_info_key", "u32"),
    ],
    "InspectAction": [
        ("action_name_hash", "u32"),
        ("catch_tag_name_hash", "u32"),
        ("catcher_socket_name", "CString"),
        ("catch_target_socket_name", "CString"),
    ],
    "ItemInfoSharpnessData": [
        ("max_sharpness", "u16"),
        ("craft_tool_info", "u16"),  # CraftToolKey = u16 (keys.rs)
        ("stat_data", "EnchantStatData"),
    ],
    "ItemBundleData": [
        ("count_mb", "u64"),
        ("key", "u32"),
    ],
    "UnitData": [
        ("ui_component", "CString"),
        ("minimum", "u32"),
        ("icon_path", "u32"),
        ("item_name", "LocalizableString"),
        ("item_desc", "LocalizableString"),
    ],
    "MoneyUnitEntry": [
        ("key", "u32"),
        ("value", "UnitData"),
    ],
    "MoneyTypeDefine": [
        ("price_floor_value", "u64"),
        ("unit_data_list_map", "CArray<MoneyUnitEntry>"),
    ],
    "PrefabData": [
        ("prefab_names", "CArray<u32>"),
        ("equip_slot_list", "CArray<u16>"),
        ("tribe_gender_list", "CArray<u32>"),
        ("is_craft_material", "u8"),
    ],
    "DockingChildData": [
        ("gimmick_info_key", "u32"),
        ("character_key", "u32"),
        ("item_key", "u32"),
        ("attach_parent_socket_name", "CString"),
        ("attach_child_socket_name", "CString"),
        ("docking_tag_name_hash", "[u32;4]"),
        ("docking_equip_slot_no", "u16"),
        ("spawn_distance_level", "u32"),
        ("is_item_equip_docking_gimmick", "u8"),
        ("send_damage_to_parent", "u8"),
        ("is_body_part", "u8"),
        ("docking_type", "u8"),
        ("is_summoner_team", "u8"),
        ("is_player_only", "u8"),
        ("is_npc_only", "u32"),  # ConditionKey = u32
        ("is_sync_break_parent", "u8"),
        ("hit_part", "u8"),
        ("detected_by_npc", "u8"),
        ("is_bag_docking", "u8"),
        ("enable_collision", "u8"),
        ("disable_collision_with_other_gimmick", "u8"),
        ("docking_slot_key", "CString"),
        ("inherit_summoner", "u8"),
        ("summon_tag_name_hash", "[u32;4]"),
    ],
    "PatternParamString": [
        ("flag", "u8"),
        ("unk_flag_2", "u8"),
        ("unk_value", "[u32;2]"),
        ("param_string", "CString"),
    ],
    "PatternDescriptionData": [
        ("pattern_description_info", "u32"),
        ("param_string_list", "CArray<PatternParamString>"),
    ],
    "RepairData": [
        ("resource_item_info", "u32"),
        ("repair_value", "u16"),
        ("repair_style", "u8"),
        ("resource_item_count", "u64"),
    ],
    "DropDefaultData": [
        ("drop_enchant_level", "u16"),
        ("socket_item_list", "CArray<u32>"),
        ("add_socket_material_item_list", "CArray<SocketMaterialItem>"),
        ("default_sub_item", "SubItem"),
        ("socket_valid_count", "u8"),
        ("use_socket", "u8"),
    ],
    # ── StageInfo helpers (ported from NattKh stageinfo_parser.py) ────────────
    # Each one mirrors a sub_*_NNNN reader. Variable-length fields whose
    # encoding NattKh couldn't fully decode (e.g. SequencerDesc with the
    # optional-object variant) are tagged below — the walker returns None
    # for entries that hit those, matching NattKh's own behavior.
    "StageInfo_CloseFilterEntry": [  # sub_141065180 element: 15B fixed
        ("data", "[u8;15]"),
    ],
    "StageInfo_Field584Entry": [  # sub_141067210 element: 7B fixed
        ("data", "[u8;7]"),
    ],
    "StageInfo_Field608Entry": [  # sub_141067080 element: u32 + CString
        ("key", "u32"),
        ("name", "CString"),
    ],
    "StageInfo_RewardDropSetEntry": [  # sub_14105FE60 element: 28B fixed
        ("data", "[u8;28]"),
    ],
    "StageInfo_Field840Entry": [
        # sub_141066350 element: u32 + u8 + (optional 7B if u8 != 0)
        # Modeled as a tagged variant so the walker handles both cases.
        ("key", "u32"),
        ("payload", "StageInfo_Field840OptPayload"),
    ],
    "StageInfo_SequencerDescNamePair": [
        ("a", "CString"),
        ("b", "CString"),
    ],
    "StageInfo_SequencerDescElement": [
        # sub_141052550: enum2B + CString + enum2B + u32 + enum2B + u8
        ("enum_a", "u16"),
        ("name", "CString"),
        ("enum_b", "u16"),
        ("u32_a", "u32"),
        ("enum_c", "u16"),
        ("u8_a", "u8"),
    ],
    "StageInfo_SequencerDescField146cEntry": [
        # sub_14106C170 element: CString + u32 + u32
        ("name", "CString"),
        ("u32_a", "u32"),
        ("u32_b", "u32"),
    ],
    "StageInfo_SequencerDesc": [
        # sub_141C952A0: CString + u32 + CString + 12B + u32 + 8*u8 +
        # enum2B + tagged optional object + 2*CString + u32 count + N*pairs +
        # u32 count + N*elements + u32 count + N*field146cEntry +
        # 2*CArray<u16> + 4*CArray<u32>
        ("name_a", "CString"),
        ("u32_a", "u32"),
        ("name_b", "CString"),
        ("vec3", "[f32;3]"),
        ("u32_b", "u32"),
        ("flags", "[u8;8]"),
        ("enum_a", "u16"),
        # Optional object — when flag is 1, the inner format is unknown
        # (NattKh's parser returns -1). The tagged variant below lets the
        # walker fail cleanly for entries that have flag=1.
        ("optional_obj", "StageInfo_SequencerDescOptObj"),
        ("name_c", "CString"),
        ("name_d", "CString"),
        ("name_pairs", "CArray<StageInfo_SequencerDescNamePair>"),
        ("elements", "CArray<StageInfo_SequencerDescElement>"),
        ("field146c", "CArray<StageInfo_SequencerDescField146cEntry>"),
        ("u16_arr_a", "CArray<u16>"),
        ("u16_arr_b", "CArray<u16>"),
        ("u32_arr_a", "CArray<u32>"),
        ("u32_arr_b", "CArray<u32>"),
        ("u32_arr_c", "CArray<u32>"),
        ("u32_arr_d", "CArray<u32>"),
    ],
    # ── FieldInfo helpers (ported from NattKh fieldinfo_parser.py) ──────────
    # _complexData (sub_141A7CA00) is the one field NattKh couldn't decode
    # field-by-field; her parser punts and reads the target from end-of-entry.
    # Until the encoding is reversed, the walker can't reliably reach
    # FieldInfo's last 4 fields. Override only walks up to (but not past)
    # _complexData.
    # ── RegionInfo helpers (ported from NattKh regioninfo_parser.py) ────────
    "RegionInfo_KnowledgeEntry": [  # sub_141064C20 element: u32 + u32
        ("key", "u32"),
        ("val", "u32"),
    ],
    "RegionInfo_GimmickAliasEntry": [  # sub_141064D30 element: u32 + u32
        ("key", "u32"),
        ("val", "u32"),
    ],
    "RegionInfo_DomainFactionEntry": [  # sub_141069840 element: u32+u32+u32
        ("condition", "u32"),
        ("domain_faction", "u32"),
        ("prison_stage", "u32"),
    ],
}


# ── Tagged variant definitions ──────────────────────────────────────────────
#
# Each entry: variant_name -> dict with discriminator type + variant payloads
# keyed by discriminator value. Empty payload string = no extra bytes.
# The discriminator is consumed first; total bytes = discriminator + payload.

TAGGED_VARIANT_DEFS: dict[str, dict] = {
    # SubItem (crimson-rs structs.rs lines 277-308)
    "SubItem": {
        "discriminator": "u8",
        "variants": {
            0: "u32",   # ItemKey
            3: "u32",   # CharacterKey
            9: "u32",   # GimmickInfoKey
            14: "",     # None - no payload
        },
    },
    # SealableItemInfo (crimson-rs structs.rs lines 411-487) — read order:
    # type_tag (u8), item_key (u32), unknown0 (u64), then variant payload
    # by tag value.
    "SealableItemInfo": {
        "discriminator": "u8",
        "fixed_prefix": [
            ("item_key", "u32"),
            ("unknown0", "u64"),
        ],
        "variants": {
            0: "u32",       # SealableValue::Item -> ItemKey
            1: "u32",       # SealableValue::Gimmick -> GimmickInfoKey
            2: "CString",   # SealableValue::String
            3: "u32",       # SealableValue::Character -> CharacterKey
            4: "u32",       # SealableValue::Tribe -> TribeInfoKey
        },
    },
    # StageInfo Field840 element optional payload (sub_141066350):
    # u8 flag — if 0, no payload; if 1, sub_141041020 = u8 + 3*enum2B = 7B.
    "StageInfo_Field840OptPayload": {
        "discriminator": "u8",
        "variants": {
            0: "",          # absent
            1: "[u8;7]",    # u8 + 3 * 2B enums = 7 bytes
        },
    },
    # StageInfo SequencerDesc optional object (sub_141066ED0 inner part):
    # u8 flag — if 0, just the flag (no payload, total 1B). If 1, NattKh's
    # parser bails (sub_141BF4F70 is a virtual reader she didn't decode).
    # Modeled here so entries with flag=1 fail cleanly via an unknown
    # discriminator value, matching NattKh's behavior.
    "StageInfo_SequencerDescOptObj": {
        "discriminator": "u8",
        "variants": {
            0: "",          # absent — fast path most stages take
            # 1 deliberately omitted: forces None on entries with the
            # complex variant, same as NattKh punting.
        },
    },
}


# ── Walker ──────────────────────────────────────────────────────────────────


def consume_bytes(type_descriptor: str, body: bytes, off: int,
                  end: int) -> Optional[int]:
    """Return how many bytes ``type_descriptor`` consumes starting at ``off``,
    or None if the bytes can't be safely walked.

    ``end`` is the upper bound (exclusive) for safe reads — usually the
    entry payload end so we don't walk into a neighboring record.
    """
    # Reject negative `off` defensively — `struct.unpack_from(buf, -N)`
    # silently reads from the buffer's end instead of raising, which
    # would let a corrupt cumulative offset produce plausible-looking
    # bytes from unrelated data. Superpowers code review SECURITY
    # finding 2026-04-27.
    if off < 0 or off > end or off > len(body):
        return None
    # Strip whitespace defensively (some schema entries include it)
    td = type_descriptor.strip()

    # Primitive
    width = _PRIMITIVE_WIDTH.get(td)
    if width is not None:
        if off + width > min(end, len(body)):
            return None
        return width

    # CString: u32 len + bytes
    if td == "CString":
        if off + 4 > min(end, len(body)):
            return None
        slen = struct.unpack_from("<I", body, off)[0]
        # Defensive cap: legitimate CStrings inside an entry are well
        # under 10MB. Anything larger is corruption or we're misaligned.
        if slen > 10_000_000:
            return None
        if off + 4 + slen > min(end, len(body)):
            return None
        return 4 + slen

    # LocalizableString: u8 category + u64 index + u32 default_len + bytes
    if td == "LocalizableString":
        head = 1 + 8 + 4
        if off + head > min(end, len(body)):
            return None
        slen = struct.unpack_from("<I", body, off + 1 + 8)[0]
        if slen > 10_000_000:
            return None
        if off + head + slen > min(end, len(body)):
            return None
        return head + slen

    # COptional<T>: u8 flag + (T if flag != 0)
    if td.startswith("COptional<") and td.endswith(">"):
        inner = td[len("COptional<"):-1]
        if off + 1 > min(end, len(body)):
            return None
        flag = body[off]
        if flag == 0:
            return 1
        inner_size = consume_bytes(inner, body, off + 1, end)
        if inner_size is None:
            return None
        return 1 + inner_size

    # CArray<T>: u32 count + count * T
    if td.startswith("CArray<") and td.endswith(">"):
        inner = td[len("CArray<"):-1]
        if off + 4 > min(end, len(body)):
            return None
        count = struct.unpack_from("<I", body, off)[0]
        # Defensive cap — CArrays inside one entry shouldn't exceed
        # millions of elements. Catches misalignment quickly.
        if count > 10_000_000:
            return None
        cur = off + 4
        for _ in range(count):
            inner_size = consume_bytes(inner, body, cur, end)
            if inner_size is None:
                return None
            cur += inner_size
        return cur - off

    # Fixed-length array: [T;N]
    if td.startswith("[") and td.endswith("]") and ";" in td:
        inner_part = td[1:-1]
        inner, count_str = inner_part.split(";", 1)
        try:
            n = int(count_str.strip())
        except ValueError:
            return None
        cur = off
        for _ in range(n):
            inner_size = consume_bytes(inner.strip(), body, cur, end)
            if inner_size is None:
                return None
            cur += inner_size
        return cur - off

    # Sub-struct
    sub_def = SUBSTRUCT_DEFS.get(td)
    if sub_def is not None:
        cur = off
        for _fname, ftype in sub_def:
            consumed = consume_bytes(ftype, body, cur, end)
            if consumed is None:
                return None
            cur += consumed
        return cur - off

    # Tagged variant
    variant = TAGGED_VARIANT_DEFS.get(td)
    if variant is not None:
        disc_type = variant["discriminator"]
        disc_size = _PRIMITIVE_WIDTH.get(disc_type)
        if disc_size is None or off + disc_size > min(end, len(body)):
            return None
        if disc_type == "u8":
            disc_value = body[off]
        elif disc_type == "u16":
            disc_value = struct.unpack_from("<H", body, off)[0]
        elif disc_type == "u32":
            disc_value = struct.unpack_from("<I", body, off)[0]
        else:
            return None
        cur = off + disc_size
        # Optional fixed prefix between discriminator and variant payload
        for _fname, ftype in variant.get("fixed_prefix", []):
            consumed = consume_bytes(ftype, body, cur, end)
            if consumed is None:
                return None
            cur += consumed
        payload_type = variant["variants"].get(disc_value)
        if payload_type is None:
            # Unknown discriminator value — refuse rather than guess.
            return None
        if payload_type == "":
            return cur - off
        consumed = consume_bytes(payload_type, body, cur, end)
        if consumed is None:
            return None
        cur += consumed
        return cur - off

    # Unknown type descriptor
    return None


# ── Schema-side helpers ─────────────────────────────────────────────────────


def is_known_type(type_descriptor: str) -> bool:
    """True when :func:`consume_bytes` knows how to walk this descriptor."""
    td = type_descriptor.strip()
    if td in _PRIMITIVE_WIDTH:
        return True
    if td in ("CString", "LocalizableString"):
        return True
    if td.startswith("COptional<") and td.endswith(">"):
        return is_known_type(td[len("COptional<"):-1])
    if td.startswith("CArray<") and td.endswith(">"):
        return is_known_type(td[len("CArray<"):-1])
    if td.startswith("[") and td.endswith("]") and ";" in td:
        inner = td[1:-1].split(";", 1)[0].strip()
        return is_known_type(inner)
    if td in SUBSTRUCT_DEFS:
        return True
    if td in TAGGED_VARIANT_DEFS:
        return True
    return False


def primitive_width(type_descriptor: str) -> Optional[int]:
    """Return fixed width for primitives, None for variable types."""
    return _PRIMITIVE_WIDTH.get(type_descriptor.strip())
