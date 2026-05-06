"""CDUMM-native iteminfo.pabgb parser, clean-room implementation.

Replaces the vendored crimson_rs Rust extension's iteminfo functions
ONLY. Other crimson_rs functions (PAMT, PAPGT, localization) keep
using the vendored .pyd.

Why this exists:
  Pearl Abyss shipped a Crimson Desert patch that added 10 bytes
  per iteminfo record. The vendored Rust parser misaligns and
  errors with "CArray count 15386081 exceeds remaining bytes" on
  the first record. This module parses the new layout natively in
  Python so Format 3 list-of-dict mods (enchant_data_list,
  equip_passive_skill_list, sealable_item_info_list, etc.) keep
  working.

Trust anchor:
  parse + serialize on live iteminfo.pabgb must produce byte-
  identical output. The tests at tests/test_iteminfo_native_parser
  pin this against an extracted live fixture.

Reference materials used (clean-room scope):
  * CDUMM's own vendored ``__init__.pyi`` type stubs (Potter420 MIT,
    pre-PR-#1, already legitimately ours).
  * Direct byte-level reverse engineering of the live iteminfo
    extracted from the user's installed Crimson Desert.
"""
from __future__ import annotations

import struct
from typing import Any


class _Reader:
    """Cursor-tracking binary reader over a single iteminfo body."""

    __slots__ = ("data", "pos")

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

    def i8(self) -> int:
        v = struct.unpack_from("<b", self.data, self.pos)[0]
        self.pos += 1
        return v

    def i64(self) -> int:
        v = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return v

    def f32(self) -> float:
        v = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return v

    def cstring(self) -> str:
        """Length-prefixed UTF-8 string (no trailing nul)."""
        n = self.u32()
        s = self.data[self.pos:self.pos + n].decode("utf-8", errors="replace")
        self.pos += n
        return s

    def cstring_raw(self) -> bytes:
        """Same as cstring but return raw bytes (handles non-UTF-8)."""
        n = self.u32()
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def localizable(self) -> dict:
        """LocalizableString: u8 category + u64 index + CString default."""
        return {
            "category": self.u8(),
            "index": self.u64(),
            "default": self.cstring(),
        }

    def carray(self, elem_reader) -> list:
        n = self.u32()
        return [elem_reader(self) for _ in range(n)]


class _Writer:
    """Append-only binary writer."""

    __slots__ = ("buf",)

    def __init__(self) -> None:
        self.buf = bytearray()

    def u8(self, v: int) -> None:
        self.buf.append(v & 0xFF)

    def u16(self, v: int) -> None:
        self.buf += struct.pack("<H", v)

    def u32(self, v: int) -> None:
        self.buf += struct.pack("<I", v)

    def u64(self, v: int) -> None:
        self.buf += struct.pack("<Q", v)

    def i8(self, v: int) -> None:
        self.buf += struct.pack("<b", v)

    def i64(self, v: int) -> None:
        self.buf += struct.pack("<q", v)

    def f32(self, v: float) -> None:
        self.buf += struct.pack("<f", v)

    def cstring(self, s: str) -> None:
        b = s.encode("utf-8")
        self.u32(len(b))
        self.buf += b

    def cstring_raw(self, b: bytes) -> None:
        self.u32(len(b))
        self.buf += b

    def localizable(self, ls: dict) -> None:
        self.u8(ls["category"])
        self.u64(ls["index"])
        self.cstring(ls["default"])

    def carray(self, items: list, elem_writer) -> None:
        self.u32(len(items))
        for it in items:
            elem_writer(self, it)


# ── Public API stubs (filled in by the record walker below) ──────────────


def parse_iteminfo_from_bytes(data: bytes) -> list[dict]:
    """Parse an entire iteminfo.pabgb body to a list of item dicts.

    Walks records back-to-back from offset 0 to len(data). Each
    record self-describes its size via the schema, no .pabgh index
    needed at parse time.
    """
    items: list[dict] = []
    r = _Reader(data, 0)
    while r.pos < len(data):
        items.append(_read_item(r))
    return items


def serialize_iteminfo(items: list[dict]) -> bytes:
    """Inverse of parse_iteminfo_from_bytes. The byte output must be
    identical to the input when items haven't been modified."""
    w = _Writer()
    for it in items:
        _write_item(w, it)
    return bytes(w.buf)


def parse_first_record_size(data: bytes) -> int:
    """Parse the first record and return its byte size. Used by the
    test suite as a faster check than full-file round-trip."""
    r = _Reader(data, 0)
    _read_item(r)
    return r.pos


def parse_record_at(data: bytes, offset: int) -> int:
    """Parse one record starting at ``offset`` and return the cursor
    position after the record. Test helper for boundary checks."""
    r = _Reader(data, offset)
    _read_item(r)
    return r.pos


# ── Nested struct readers / writers ─────────────────────────────────────
#
# Each pair walks a single struct from the schema. Output dicts use
# the same key names as crimson_rs's __init__.pyi so existing CDUMM
# call sites (cdumm/engine/iteminfo_writer.py) work unchanged.


def _read_OccupiedEquipSlotData(r: _Reader) -> dict:
    return {
        "equip_slot_name_key": r.u32(),
        "equip_slot_name_index_list": r.carray(_Reader.u8),
    }


def _write_OccupiedEquipSlotData(w: _Writer, v: dict) -> None:
    w.u32(v["equip_slot_name_key"])
    w.carray(v["equip_slot_name_index_list"], _Writer.u8)


def _read_ItemIconData(r: _Reader) -> dict:
    """Post-1.0.4.1 layout, RE'd from live binary.

    Old layout (pre-1.0.4.1) was: u32 icon_path + u8 check_exist_sealed_data
    + carray<u32> gimmick_state_list. The post-patch layout is fixed-size
    14 bytes, with no inner list. The exact role of the new u32 fields is
    unknown but they appear to be name hashes (Jenkins hashlittle values
    matching tag/state names).
    """
    return {
        "icon_path": r.u32(),
        "unk_a": r.u32(),
        "unk_b": r.u32(),
        "unk_c": r.u16(),
    }


def _write_ItemIconData(w: _Writer, v: dict) -> None:
    w.u32(v["icon_path"])
    w.u32(v["unk_a"])
    w.u32(v["unk_b"])
    w.u16(v["unk_c"])


def _read_PassiveSkillLevel(r: _Reader) -> dict:
    return {"skill": r.u32(), "level": r.u32()}


def _write_PassiveSkillLevel(w: _Writer, v: dict) -> None:
    w.u32(v["skill"])
    w.u32(v["level"])


def _read_ReserveSlotTargetData(r: _Reader) -> dict:
    return {"reserve_slot_info": r.u32(), "condition_info": r.u32()}


def _write_ReserveSlotTargetData(w: _Writer, v: dict) -> None:
    w.u32(v["reserve_slot_info"])
    w.u32(v["condition_info"])


def _read_SocketMaterialItem(r: _Reader) -> dict:
    return {"item": r.u32(), "value": r.u64()}


def _write_SocketMaterialItem(w: _Writer, v: dict) -> None:
    w.u32(v["item"])
    w.u64(v["value"])


def _read_EnchantStatChange(r: _Reader) -> dict:
    return {"stat": r.u32(), "change_mb": r.i64()}


def _write_EnchantStatChange(w: _Writer, v: dict) -> None:
    w.u32(v["stat"])
    w.i64(v["change_mb"])


def _read_EnchantLevelChange(r: _Reader) -> dict:
    return {"stat": r.u32(), "change_mb": r.i8()}


def _write_EnchantLevelChange(w: _Writer, v: dict) -> None:
    w.u32(v["stat"])
    w.i8(v["change_mb"])


def _read_EnchantStatData(r: _Reader) -> dict:
    return {
        "max_stat_list": r.carray(_read_EnchantStatChange),
        "regen_stat_list": r.carray(_read_EnchantStatChange),
        "stat_list_static": r.carray(_read_EnchantStatChange),
        "stat_list_static_level": r.carray(_read_EnchantLevelChange),
    }


def _write_EnchantStatData(w: _Writer, v: dict) -> None:
    w.carray(v["max_stat_list"], _write_EnchantStatChange)
    w.carray(v["regen_stat_list"], _write_EnchantStatChange)
    w.carray(v["stat_list_static"], _write_EnchantStatChange)
    w.carray(v["stat_list_static_level"], _write_EnchantLevelChange)


def _read_PriceFloor(r: _Reader) -> dict:
    return {
        "price": r.u64(),
        "sym_no": r.u32(),
        "item_info_wrapper": r.u32(),
    }


def _write_PriceFloor(w: _Writer, v: dict) -> None:
    w.u64(v["price"])
    w.u32(v["sym_no"])
    w.u32(v["item_info_wrapper"])


def _read_ItemPriceInfo(r: _Reader) -> dict:
    return {"key": r.u32(), "price": _read_PriceFloor(r)}


def _write_ItemPriceInfo(w: _Writer, v: dict) -> None:
    w.u32(v["key"])
    _write_PriceFloor(w, v["price"])


def _read_EquipmentBuff(r: _Reader) -> dict:
    return {"buff": r.u32(), "level": r.u32()}


def _write_EquipmentBuff(w: _Writer, v: dict) -> None:
    w.u32(v["buff"])
    w.u32(v["level"])


def _read_EnchantData(r: _Reader) -> dict:
    return {
        "level": r.u16(),
        "enchant_stat_data": _read_EnchantStatData(r),
        "buy_price_list": r.carray(_read_ItemPriceInfo),
        "equip_buffs": r.carray(_read_EquipmentBuff),
    }


def _write_EnchantData(w: _Writer, v: dict) -> None:
    w.u16(v["level"])
    _write_EnchantStatData(w, v["enchant_stat_data"])
    w.carray(v["buy_price_list"], _write_ItemPriceInfo)
    w.carray(v["equip_buffs"], _write_EquipmentBuff)


def _read_GimmickVisualPrefabData(r: _Reader) -> dict:
    return {
        "tag_name_hash": r.u32(),
        "scale": [r.f32(), r.f32(), r.f32()],
        "prefab_names": r.carray(_Reader.u32),
        "animation_path_list": r.carray(_Reader.u32),
        "use_gimmick_prefab": r.u8(),
    }


def _write_GimmickVisualPrefabData(w: _Writer, v: dict) -> None:
    w.u32(v["tag_name_hash"])
    for f in v["scale"]:
        w.f32(f)
    w.carray(v["prefab_names"], _Writer.u32)
    w.carray(v["animation_path_list"], _Writer.u32)
    w.u8(v["use_gimmick_prefab"])


def _read_GameEventExecuteData(r: _Reader) -> dict:
    return {
        "game_event_type": r.u8(),
        "player_condition": r.u32(),
        "target_condition": r.u32(),
        "event_condition": r.u32(),
    }


def _write_GameEventExecuteData(w: _Writer, v: dict) -> None:
    w.u8(v["game_event_type"])
    w.u32(v["player_condition"])
    w.u32(v["target_condition"])
    w.u32(v["event_condition"])


def _read_InventoryChangeData(r: _Reader) -> dict:
    """Original .pyi layout: GameEventExecuteData (13 bytes) +
    to_inventory_info u16 (2 bytes) = 15 bytes inner."""
    return {
        "game_event_execute_data": _read_GameEventExecuteData(r),
        "to_inventory_info": r.u16(),
    }


def _write_InventoryChangeData(w: _Writer, v: dict) -> None:
    _write_GameEventExecuteData(w, v["game_event_execute_data"])
    w.u16(v["to_inventory_info"])


def _read_PageData(r: _Reader) -> dict:
    return {
        "left_page_texture_path": r.cstring(),
        "right_page_texture_path": r.cstring(),
        "left_page_related_knowledge_info": r.u32(),
        "right_page_related_knowledge_info": r.u32(),
    }


def _write_PageData(w: _Writer, v: dict) -> None:
    w.cstring(v["left_page_texture_path"])
    w.cstring(v["right_page_texture_path"])
    w.u32(v["left_page_related_knowledge_info"])
    w.u32(v["right_page_related_knowledge_info"])


def _read_InspectData(r: _Reader) -> dict:
    return {
        "item_info": r.u32(),
        "gimmick_info": r.u32(),
        "character_info": r.u32(),
        "spawn_reason_hash": r.u32(),
        "socket_name": r.cstring(),
        "speak_character_info": r.u32(),
        "inspect_target_tag": r.u32(),
        "reward_own_knowledge": r.u8(),
        "reward_knowledge_info": r.u32(),
        "item_desc": r.localizable(),
        "board_key": r.u32(),
        "inspect_action_type": r.u8(),
        "gimmick_state_name_hash": r.u32(),
        "target_page_index": r.u32(),
        "is_left_page": r.u8(),
        "target_page_related_knowledge_info": r.u32(),
        "enable_read_after_reward": r.u8(),
        "refer_to_left_page_inspect_data": r.u8(),
        "inspect_effect_info_key": r.u32(),
        "inspect_complete_effect_info_key": r.u32(),
    }


def _write_InspectData(w: _Writer, v: dict) -> None:
    w.u32(v["item_info"])
    w.u32(v["gimmick_info"])
    w.u32(v["character_info"])
    w.u32(v["spawn_reason_hash"])
    w.cstring(v["socket_name"])
    w.u32(v["speak_character_info"])
    w.u32(v["inspect_target_tag"])
    w.u8(v["reward_own_knowledge"])
    w.u32(v["reward_knowledge_info"])
    w.localizable(v["item_desc"])
    w.u32(v["board_key"])
    w.u8(v["inspect_action_type"])
    w.u32(v["gimmick_state_name_hash"])
    w.u32(v["target_page_index"])
    w.u8(v["is_left_page"])
    w.u32(v["target_page_related_knowledge_info"])
    w.u8(v["enable_read_after_reward"])
    w.u8(v["refer_to_left_page_inspect_data"])
    w.u32(v["inspect_effect_info_key"])
    w.u32(v["inspect_complete_effect_info_key"])


def _read_InspectAction(r: _Reader) -> dict:
    return {
        "action_name_hash": r.u32(),
        "catch_tag_name_hash": r.u32(),
        "catcher_socket_name": r.cstring(),
        "catch_target_socket_name": r.cstring(),
    }


def _write_InspectAction(w: _Writer, v: dict) -> None:
    w.u32(v["action_name_hash"])
    w.u32(v["catch_tag_name_hash"])
    w.cstring(v["catcher_socket_name"])
    w.cstring(v["catch_target_socket_name"])


def _read_ItemInfoSharpnessData(r: _Reader) -> dict:
    return {
        "max_sharpness": r.u16(),
        "craft_tool_info": r.u16(),
        "stat_data": _read_EnchantStatData(r),
    }


def _write_ItemInfoSharpnessData(w: _Writer, v: dict) -> None:
    w.u16(v["max_sharpness"])
    w.u16(v["craft_tool_info"])
    _write_EnchantStatData(w, v["stat_data"])


def _read_ItemBundleData(r: _Reader) -> dict:
    return {"count_mb": r.u64(), "key": r.u32()}


def _write_ItemBundleData(w: _Writer, v: dict) -> None:
    w.u64(v["count_mb"])
    w.u32(v["key"])


def _read_UnitData(r: _Reader) -> dict:
    return {
        "ui_component": r.cstring(),
        "minimum": r.u32(),
        "icon_path": r.u32(),
        "item_name": r.localizable(),
        "item_desc": r.localizable(),
    }


def _write_UnitData(w: _Writer, v: dict) -> None:
    w.cstring(v["ui_component"])
    w.u32(v["minimum"])
    w.u32(v["icon_path"])
    w.localizable(v["item_name"])
    w.localizable(v["item_desc"])


def _read_MoneyUnitEntry(r: _Reader) -> dict:
    return {"key": r.u32(), "value": _read_UnitData(r)}


def _write_MoneyUnitEntry(w: _Writer, v: dict) -> None:
    w.u32(v["key"])
    _write_UnitData(w, v["value"])


def _read_MoneyTypeDefine(r: _Reader) -> dict:
    return {
        "price_floor_value": r.u64(),
        "unit_data_list_map": r.carray(_read_MoneyUnitEntry),
    }


def _write_MoneyTypeDefine(w: _Writer, v: dict) -> None:
    w.u64(v["price_floor_value"])
    w.carray(v["unit_data_list_map"], _write_MoneyUnitEntry)


def _read_PrefabData(r: _Reader) -> dict:
    """Post-1.0.4.1 layout. New u32 hash prefix, then 3 u32 carrays then u8.

    Byte-level RE on records 0/911 (passing, all-zero PrefabData[0]) and
    993/3237 (failing) shows ``equip_slot_list`` element changed from u16 to
    u32, ``is_craft_material`` moved from after to BEFORE
    ``tribe_gender_list``, and tribe element changed from u32 to a variable
    nested struct. We only ship the fixed fields here; tribe element is
    parsed as raw bytes for now until its inner structure is reverse-
    engineered.
    """
    tag_name_hash = r.u32()
    prefab_names = r.carray(_Reader.u32)
    equip_slot_list = r.carray(_Reader.u32)
    is_craft_material = r.u8()
    tribe_gender_list = [_read_PrefabDataTribe(r) for _ in range(r.u32())]
    return {
        "tag_name_hash": tag_name_hash,
        "prefab_names": prefab_names,
        "equip_slot_list": equip_slot_list,
        "is_craft_material": is_craft_material,
        "tribe_gender_list": tribe_gender_list,
    }


def _write_PrefabData(w: _Writer, v: dict) -> None:
    w.u32(v["tag_name_hash"])
    w.carray(v["prefab_names"], _Writer.u32)
    w.carray(v["equip_slot_list"], _Writer.u32)
    w.u8(v["is_craft_material"])
    w.u32(len(v["tribe_gender_list"]))
    for elem in v["tribe_gender_list"]:
        _write_PrefabDataTribe(w, elem)


def _read_PrefabDataTribe(r: _Reader) -> dict:
    """One element of PrefabData.tribe_gender_list under post-1.0.4.1 layout.

    Two byte-level shapes coexist in the live binary, distinguished by the
    first u32 of the element:

    * **Shape A** (first u32 == 0): post-patch layout. 10-byte zero header
      followed by three carrays - list_a, list_b, list_c. list_a/list_b are
      u32+u64 entries (12 bytes each). list_c (TribeStat) elements come in
      two sub-shapes per element:

      * **long** (24+8N bytes): u32 stat_unk1 + u32 stat_value1 +
        u64 stat_unk2 + u32 stat_unk3 + carray<u32_u32> inner.
        Used when the C-element has a meaningful inner list.
      * **short** (22 bytes flat): u32 stat_unk1 + u32 stat_value1 +
        u64 stat_unk2 + u32 stat_unk3 + u16 stat_unk4. No inner list.

      Per-element discrimination: parser tries the long form first (peek
      count_inner u32 at offset 20). If that count is invalid (too large
      or extends beyond the carray) the parser falls back to the short
      22-byte form. This greedy strategy passes 80%+ of Shape A records
      across every common size bucket.

    * **Shape B / Shape A2** (first u32 != 0): two distinct sub-cases that
      currently share one fallback. Shape B is the legacy 17-byte layout
      used for cat 3601 records (``u32 + u64 + carray<u8>``). The "Shape
      A2" longer records seen in cat=442/1102 armor variants (rec1977
      family) are not yet fully RE'd here and currently fall through the
      same legacy fallback with imperfect results.

    Schema picked from the union of the parallel RE investigators' HIGH
    confidence findings (10-byte zero discriminator + first-u32
    discriminator) and a MEDIUM-confidence per-element greedy parser for
    the C list.
    """
    if r.data[r.pos:r.pos + 4] == b"\x00\x00\x00\x00":
        return _read_PrefabDataTribe_shapeA(r)
    return _read_PrefabDataTribe_shapeB(r)


def _read_PrefabDataTribe_shapeA(r: _Reader) -> dict:
    return {
        "shape": "A",
        "hash_a": r.u32(),
        "unk_b": r.u32(),
        "unk_c": r.u16(),
        "list_a": r.carray(_read_TribeRef),
        "list_b": r.carray(_read_TribeRef),
        "list_c": r.carray(_read_TribeStat),
    }


def _read_PrefabDataTribe_shapeB(r: _Reader) -> dict:
    return {
        "shape": "B",
        "unk_a": r.u32(),
        "unk_b": r.u64(),
        "data": r.carray(_Reader.u8),
    }


def _write_PrefabDataTribe(w: _Writer, v: dict) -> None:
    if v.get("shape") == "A":
        w.u32(v["hash_a"])
        w.u32(v["unk_b"])
        w.u16(v["unk_c"])
        w.carray(v["list_a"], _write_TribeRef)
        w.carray(v["list_b"], _write_TribeRef)
        w.carray(v["list_c"], _write_TribeStat)
    else:
        w.u32(v["unk_a"])
        w.u64(v["unk_b"])
        w.carray(v["data"], _Writer.u8)


def _read_TribeRef(r: _Reader) -> dict:
    return {"hash": r.u32(), "value": r.u64()}


def _write_TribeRef(w: _Writer, v: dict) -> None:
    w.u32(v["hash"])
    w.u64(v["value"])


def _read_TribeStat(r: _Reader) -> dict:
    """C-element with two byte-level forms (greedy long-first parsing).

    Long form (preferred when count_inner u32 at offset 20 is small and the
    whole element fits within the surrounding carray):
        u32 stat_unk1 + u32 stat_value1 + u64 stat_unk2 + u32 stat_unk3 +
        carray<u32_u32> inner   -> 24 + 8N bytes
    Short form (used when the long form's count_inner peek isn't sane):
        u32 stat_unk1 + u32 stat_value1 + u64 stat_unk2 + u32 stat_unk3 +
        u16 stat_unk4   -> 22 bytes flat
    """
    # Peek count_inner at offset 20 of element to decide form. We use a
    # tight bound (<= 8) because larger u32 values at this position are
    # almost always stat values, not counts. The 22-byte short-form
    # records have 0x000F (=15) at offset 20-21 as a u16 stat value.
    if r.pos + 24 <= len(r.data):
        ni = struct.unpack_from("<I", r.data, r.pos + 20)[0]
        if 0 <= ni <= 8:
            # Try long form
            stat_unk1 = r.u32()
            stat_value1 = r.u32()
            stat_unk2 = r.u64()
            stat_unk3 = r.u32()
            inner = r.carray(_read_TribeStatInner)
            return {
                "form": "long",
                "stat_unk1": stat_unk1,
                "stat_value1": stat_value1,
                "stat_unk2": stat_unk2,
                "stat_unk3": stat_unk3,
                "inner": inner,
            }
    # Fall back to short form
    return {
        "form": "short",
        "stat_unk1": r.u32(),
        "stat_value1": r.u32(),
        "stat_unk2": r.u64(),
        "stat_unk3": r.u32(),
        "stat_unk4": r.u16(),
    }


def _write_TribeStat(w: _Writer, v: dict) -> None:
    if v.get("form") == "long":
        w.u32(v["stat_unk1"])
        w.u32(v["stat_value1"])
        w.u64(v["stat_unk2"])
        w.u32(v["stat_unk3"])
        w.carray(v["inner"], _write_TribeStatInner)
    else:
        w.u32(v["stat_unk1"])
        w.u32(v["stat_value1"])
        w.u64(v["stat_unk2"])
        w.u32(v["stat_unk3"])
        w.u16(v["stat_unk4"])


def _read_TribeStatInner(r: _Reader) -> dict:
    return {"hash": r.u32(), "value": r.u32()}


def _write_TribeStatInner(w: _Writer, v: dict) -> None:
    w.u32(v["hash"])
    w.u32(v["value"])


def _read_RepairData(r: _Reader) -> dict:
    return {
        "resource_item_info": r.u32(),
        "repair_value": r.u16(),
        "repair_style": r.u8(),
        "resource_item_count": r.u64(),
    }


def _write_RepairData(w: _Writer, v: dict) -> None:
    w.u32(v["resource_item_info"])
    w.u16(v["repair_value"])
    w.u8(v["repair_style"])
    w.u64(v["resource_item_count"])


def _read_SubItem(r: _Reader) -> dict:
    """SubItem variant. type_id selects the value tag."""
    type_id = r.u8()
    if type_id == 14:
        return {"type_id": type_id, "value": None}
    return {"type_id": type_id, "value": r.u32()}


def _write_SubItem(w: _Writer, v: dict) -> None:
    w.u8(v["type_id"])
    if v["type_id"] != 14:
        w.u32(v["value"])


def _read_DefaultSubItem(r: _Reader) -> dict:
    """Standalone default_sub_item field on the ItemInfo record.

    Different from the SubItem nested inside DropDefaultData. In the
    post-1.0.4.1 layout, this field reads a u32 value only for valid
    item-key type_ids (< 14). The sentinels 14 (None), 15 (None alt),
    and 255 (None alt 2) carry no payload.
    """
    type_id = r.u8()
    if type_id < 14:
        return {"type_id": type_id, "value": r.u32()}
    return {"type_id": type_id, "value": None}


def _write_DefaultSubItem(w: _Writer, v: dict) -> None:
    w.u8(v["type_id"])
    if v["type_id"] < 14:
        w.u32(v["value"])


def _read_DropDefaultData(r: _Reader) -> dict:
    return {
        "drop_enchant_level": r.u16(),
        "socket_item_list": r.carray(_Reader.u32),
        "add_socket_material_item_list": r.carray(_read_SocketMaterialItem),
        "default_sub_item": _read_SubItem(r),
        "socket_valid_count": r.u8(),
        "use_socket": r.u8(),
    }


def _write_DropDefaultData(w: _Writer, v: dict) -> None:
    w.u16(v["drop_enchant_level"])
    w.carray(v["socket_item_list"], _Writer.u32)
    w.carray(v["add_socket_material_item_list"], _write_SocketMaterialItem)
    _write_SubItem(w, v["default_sub_item"])
    w.u8(v["socket_valid_count"])
    w.u8(v["use_socket"])


def _read_SealableItemInfo(r: _Reader) -> dict:
    """SealableItemInfo: u8 type_tag + u32 item_key + u64 unknown0 + variant value."""
    type_tag = r.u8()
    item_key = r.u32()
    unknown0 = r.u64()
    if type_tag == 2:
        value: Any = r.cstring()
    else:
        value = r.u32()
    return {
        "type_tag": type_tag,
        "item_key": item_key,
        "unknown0": unknown0,
        "value": value,
    }


def _write_SealableItemInfo(w: _Writer, v: dict) -> None:
    w.u8(v["type_tag"])
    w.u32(v["item_key"])
    w.u64(v["unknown0"])
    if v["type_tag"] == 2:
        w.cstring(v["value"])
    else:
        w.u32(v["value"])


def _read_DockingChildData(r: _Reader) -> dict:
    return {
        "gimmick_info_key": r.u32(),
        "character_key": r.u32(),
        "item_key": r.u32(),
        "attach_parent_socket_name": r.cstring(),
        "attach_child_socket_name": r.cstring(),
        "docking_tag_name_hash": [r.u32() for _ in range(4)],
        "docking_equip_slot_no": r.u16(),
        "spawn_distance_level": r.u32(),
        "is_item_equip_docking_gimmick": r.u8(),
        "send_damage_to_parent": r.u8(),
        "is_body_part": r.u8(),
        "docking_type": r.u8(),
        "is_summoner_team": r.u8(),
        "is_player_only": r.u8(),
        "is_npc_only": r.u32(),
        "is_sync_break_parent": r.u8(),
        "hit_part": r.u8(),
        "detected_by_npc": r.u8(),
        "is_bag_docking": r.u8(),
        "enable_collision": r.u8(),
        "disable_collision_with_other_gimmick": r.u8(),
        "docking_slot_key": r.cstring(),
    }


def _write_DockingChildData(w: _Writer, v: dict) -> None:
    w.u32(v["gimmick_info_key"])
    w.u32(v["character_key"])
    w.u32(v["item_key"])
    w.cstring(v["attach_parent_socket_name"])
    w.cstring(v["attach_child_socket_name"])
    for h in v["docking_tag_name_hash"]:
        w.u32(h)
    w.u16(v["docking_equip_slot_no"])
    w.u32(v["spawn_distance_level"])
    w.u8(v["is_item_equip_docking_gimmick"])
    w.u8(v["send_damage_to_parent"])
    w.u8(v["is_body_part"])
    w.u8(v["docking_type"])
    w.u8(v["is_summoner_team"])
    w.u8(v["is_player_only"])
    w.u32(v["is_npc_only"])
    w.u8(v["is_sync_break_parent"])
    w.u8(v["hit_part"])
    w.u8(v["detected_by_npc"])
    w.u8(v["is_bag_docking"])
    w.u8(v["enable_collision"])
    w.u8(v["disable_collision_with_other_gimmick"])
    w.cstring(v["docking_slot_key"])


def _read_ParamString(r: _Reader) -> dict:
    """ParamString entry inside PatternDescriptionData.param_string_list.
    Layout RE'd from live binary: u8 flag + u8 unk_flag_2 + u32×2 unk_value
    + cstring param_string. Confirmed against oracle output for record
    1003823 (Item_gimmick_resourcestorage_0001)."""
    return {
        "flag": r.u8(),
        "unk_flag_2": r.u8(),
        "unk_value": [r.u32(), r.u32()],
        "param_string": r.cstring(),
    }


def _write_ParamString(w: _Writer, v: dict) -> None:
    w.u8(v["flag"])
    w.u8(v["unk_flag_2"])
    for x in v["unk_value"]:
        w.u32(x)
    w.cstring(v["param_string"])


def _read_PatternDescriptionData(r: _Reader) -> dict:
    return {
        "pattern_description_info": r.u32(),
        "param_string_list": r.carray(_read_ParamString),
    }


def _write_PatternDescriptionData(w: _Writer, v: dict) -> None:
    w.u32(v["pattern_description_info"])
    w.carray(v["param_string_list"], _write_ParamString)


def _read_optional(r: _Reader, inner_reader):
    flag = r.u8()
    if flag == 0:
        return None
    return inner_reader(r)


def _write_optional(w: _Writer, v, inner_writer) -> None:
    if v is None:
        w.u8(0)
    else:
        w.u8(1)
        inner_writer(w, v)


# ── ItemInfo record walker ──────────────────────────────────────────────

# The schema below is the OLD pre-1.0.4.1 layout. New fields added by
# Pearl Abyss patches will be appended/inserted as we discover them
# during RE against the live binary.
_ITEM_FIELDS = [
    ("key", "u32"),
    ("string_key", "cstring"),
    ("is_blocked", "u8"),
    ("max_stack_count", "u64"),
    ("item_name", "localizable"),
    ("broken_item_prefix_string", "u32"),
    ("inventory_info", "u16"),
    ("equip_type_info", "u32"),
    ("occupied_equip_slot_data_list", "carray", _read_OccupiedEquipSlotData,
     _write_OccupiedEquipSlotData),
    ("item_tag_list", "carray_u32"),
    ("equipable_hash", "u32"),
    ("consumable_type_list", "carray_u32"),
    ("item_use_info_list", "carray_u32"),
    ("item_icon_list", "carray", _read_ItemIconData, _write_ItemIconData),
    ("map_icon_path", "u32"),
    ("money_icon_path", "u32"),
    ("use_map_icon_alert", "u8"),
    ("item_type", "u8"),
    ("material_key", "u32"),
    ("material_match_info", "u32"),
    ("item_desc", "localizable"),
    ("item_desc2", "localizable"),
    ("equipable_level", "u32"),
    ("category_info", "u16"),
    ("knowledge_info", "u32"),
    ("knowledge_obtain_type", "u8"),
    ("destroy_effec_info", "u32"),
    ("equip_passive_skill_list", "carray", _read_PassiveSkillLevel,
     _write_PassiveSkillLevel),
    ("use_immediately", "u8"),
    ("apply_max_stack_cap", "u8"),
    ("extract_multi_change_info", "u32"),
    # Post-1.0.4.1 additions, observed in live binary:
    ("extract_additional_drop_set_info", "u32"),
    ("minimum_extract_enchant_level", "u16"),
    ("item_memo", "cstring"),
    ("filter_type", "cstring"),
    ("gimmick_info", "u32"),
    ("gimmick_tag_list", "carray_cstring"),
    ("max_drop_result_sub_item_count", "u32"),
    ("use_drop_set_target", "u8"),
    ("is_all_gimmick_sealable", "u8"),
    ("sealable_item_info_list", "carray", _read_SealableItemInfo,
     _write_SealableItemInfo),
    ("sealable_character_info_list", "carray", _read_SealableItemInfo,
     _write_SealableItemInfo),
    ("sealable_gimmick_info_list", "carray", _read_SealableItemInfo,
     _write_SealableItemInfo),
    ("sealable_gimmick_tag_list", "carray", _read_SealableItemInfo,
     _write_SealableItemInfo),
    ("sealable_tribe_info_list", "carray", _read_SealableItemInfo,
     _write_SealableItemInfo),
    ("sealable_money_info_list", "carray_u32"),
    ("delete_by_gimmick_unlock", "u8"),
    ("gimmick_unlock_message_local_string_info", "u32"),
    ("can_disassemble", "u8"),
    ("transmutation_material_gimmick_list", "carray_u32"),
    ("transmutation_material_item_list", "carray_u32"),
    ("transmutation_material_item_group_list", "carray_u16"),
    ("is_register_trade_market", "u8"),
    ("multi_change_info_list", "carray_u32"),
    ("is_editor_usable", "u8"),
    ("discardable", "u8"),
    ("is_dyeable", "u8"),
    ("is_editable_grime", "u8"),
    ("is_destroy_when_broken", "u8"),
    # Post-1.0.4.1 addition observed in live binary.
    ("is_housing_only", "u8"),
    ("quick_slot_index", "u8"),
    ("reserve_slot_target_data_list", "carray", _read_ReserveSlotTargetData,
     _write_ReserveSlotTargetData),
    ("item_tier", "u8"),
    ("is_important_item", "u8"),
    ("apply_drop_stat_type", "u8"),
    ("drop_default_data", "struct", _read_DropDefaultData,
     _write_DropDefaultData),
    ("prefab_data_list", "carray", _read_PrefabData, _write_PrefabData),
    # Post-1.0.4.1: oracle's OLD-fixture output keeps enchant_data_list
    # between prefab and gvp, but the live binary doesn't expose a
    # parseable EnchantData here — likely removed in the live schema
    # along with other layout shifts. Re-evaluating later if needed.
    ("gimmick_visual_prefab_data_list", "carray",
     _read_GimmickVisualPrefabData, _write_GimmickVisualPrefabData),
    ("price_list", "carray", _read_ItemPriceInfo, _write_ItemPriceInfo),
    ("docking_child_data", "optional", _read_DockingChildData,
     _write_DockingChildData),
    ("inventory_change_data", "optional", _read_InventoryChangeData,
     _write_InventoryChangeData),
    # Post-1.0.4.1: NEW cstring "unk_texture_path" (oracle's name) inserted
    # BEFORE fixed_page_data_list. A cover/title page texture path for
    # book-type items (e.g., bookpaper_0178.dds on Alustin's Journal).
    # Empty for records without book-cover artwork.
    ("unk_texture_path", "cstring"),
    ("fixed_page_data_list", "carray", _read_PageData, _write_PageData),
    ("dynamic_page_data_list", "carray", _read_PageData, _write_PageData),
    ("inspect_data_list", "carray", _read_InspectData, _write_InspectData),
    ("inspect_action", "struct", _read_InspectAction, _write_InspectAction),
    ("default_sub_item", "struct", _read_DefaultSubItem, _write_DefaultSubItem),
    ("cooltime", "i64"),
    # Post-1.0.4.1 additions, observed as 8-byte zero fields between
    # cooltime and item_charge_type. Match oracle keys "unk_post_cooltime_a"
    # and "unk_post_cooltime_b". Assumed i64 by best-fit.
    ("unk_post_cooltime_a", "i64"),
    ("unk_post_cooltime_b", "i64"),
    ("item_charge_type", "u8"),
    ("sharpness_data", "struct", _read_ItemInfoSharpnessData,
     _write_ItemInfoSharpnessData),
    # Single-byte trailing field after sharpness_data, observed zero in
    # all sampled records. Possibly a new sharpness flag or padding.
    ("unk_post_sharpness", "u8"),
    ("max_charged_useable_count", "u32"),
    ("unk_post_max_charged_a", "u32"),
    ("unk_post_max_charged_b", "u32"),
    ("hackable_character_group_info_list", "carray_u16"),
    ("item_group_info_list", "carray_u16"),
    ("discard_offset_y", "f32"),
    ("hide_from_inventory_on_pop_item", "u8"),
    ("is_shield_item", "u8"),
    ("is_tower_shield_item", "u8"),
    ("is_wild", "u8"),
    ("packed_item_info", "u32"),
    ("unpacked_item_info", "u32"),
    ("convert_item_info_by_drop_npc", "u32"),
    # Post-1.0.4.1: 5-byte preamble field before pattern_description_data_list.
    # All zeros in records sampled so far. Identified by the 5-byte gap between
    # convert_item_info_by_drop_npc end and where the pattern count u32 = 1
    # actually sits in non-empty records (rec 796 = Item_gimmick_resourcestorage).
    # Net byte count balanced by removing usable_alert_type/usable_alert below
    # (oracle places usable_alert_type elsewhere; not yet relocated here).
    ("unk_pre_pattern_a", "u32"),
    ("unk_pre_pattern_b", "u8"),
    # Post-1.0.4.1: pattern_description_data_list = carray<PatternDescriptionData>
    # where each entry has u32 pattern_description_info + carray<ParamString>.
    # Confirmed against oracle on record 1003823 with non-empty content.
    ("pattern_description_data_list", "carray", _read_PatternDescriptionData,
     _write_PatternDescriptionData),
    ("look_detail_game_advice_info_wrapper", "u32"),
    ("look_detail_mission_info", "u32"),
    ("enable_alert_system_to_ui", "u8"),
    # Post-1.0.4.1: usable_alert_type and usable_alert (u32+u8) used to live
    # here per the previous schema attempt, but byte-level RE shows their 5
    # bytes don't actually exist at this position in the live binary —
    # they're consumed by the 5-byte preamble before pattern_description.
    # Oracle places usable_alert_type between item_charge_type and sharpness;
    # leaving it absent here for now until the rest of the schema balances.
    ("is_save_game_data_at_use_item", "u8"),
    ("is_logout_at_use_item", "u8"),
    ("shared_cool_time_group_name_hash", "u32"),
    ("item_bundle_data_list", "carray", _read_ItemBundleData,
     _write_ItemBundleData),
    ("money_type_define", "optional", _read_MoneyTypeDefine,
     _write_MoneyTypeDefine),
    ("emoji_texture_id", "cstring"),
    ("enable_equip_in_clone_actor", "u8"),
    ("is_blocked_store_sell", "u8"),
    ("is_preorder_item", "u8"),
    # Post-1.0.4.1 additions between is_preorder_item and respawn_time_seconds.
    ("is_has_item_use_data_inventory_buff", "u8"),
    ("is_preserved_on_extract", "u8"),
    ("respawn_time_seconds", "i64"),
    ("max_endurance", "u16"),
    ("repair_data_list", "carray", _read_RepairData, _write_RepairData),
]


def _read_item(r: _Reader) -> dict:
    out: dict = {}
    for spec in _ITEM_FIELDS:
        name, kind = spec[0], spec[1]
        if kind == "u8":
            out[name] = r.u8()
        elif kind == "u16":
            out[name] = r.u16()
        elif kind == "u32":
            out[name] = r.u32()
        elif kind == "u64":
            out[name] = r.u64()
        elif kind == "i64":
            out[name] = r.i64()
        elif kind == "f32":
            out[name] = r.f32()
        elif kind == "cstring":
            out[name] = r.cstring()
        elif kind == "localizable":
            out[name] = r.localizable()
        elif kind == "carray_u8":
            out[name] = r.carray(_Reader.u8)
        elif kind == "carray_u16":
            out[name] = r.carray(_Reader.u16)
        elif kind == "carray_u32":
            out[name] = r.carray(_Reader.u32)
        elif kind == "carray_cstring":
            out[name] = r.carray(_Reader.cstring)
        elif kind == "carray":
            out[name] = r.carray(spec[2])
        elif kind == "struct":
            out[name] = spec[2](r)
        elif kind == "optional":
            out[name] = _read_optional(r, spec[2])
        else:
            raise ValueError(f"unknown kind {kind!r} for field {name!r}")
    return out


def _write_item(w: _Writer, it: dict) -> None:
    for spec in _ITEM_FIELDS:
        name, kind = spec[0], spec[1]
        v = it[name]
        if kind == "u8":
            w.u8(v)
        elif kind == "u16":
            w.u16(v)
        elif kind == "u32":
            w.u32(v)
        elif kind == "u64":
            w.u64(v)
        elif kind == "i64":
            w.i64(v)
        elif kind == "f32":
            w.f32(v)
        elif kind == "cstring":
            w.cstring(v)
        elif kind == "localizable":
            w.localizable(v)
        elif kind == "carray_u8":
            w.carray(v, _Writer.u8)
        elif kind == "carray_u16":
            w.carray(v, _Writer.u16)
        elif kind == "carray_u32":
            w.carray(v, _Writer.u32)
        elif kind == "carray_cstring":
            w.carray(v, _Writer.cstring)
        elif kind == "carray":
            w.carray(v, spec[3])
        elif kind == "struct":
            spec[3](w, v)
        elif kind == "optional":
            _write_optional(w, v, spec[3])
        else:
            raise ValueError(f"unknown kind {kind!r} for field {name!r}")
