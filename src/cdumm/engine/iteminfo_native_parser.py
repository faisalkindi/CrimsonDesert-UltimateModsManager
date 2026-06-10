"""CDUMM-native iteminfo.pabgb parser, clean-room implementation.

GitHub #182 (CD 1.09, in progress):
  Crimson Desert 1.09 changed the iteminfo schema. _ITEM_FIELDS as
  shipped here desyncs on the very first record because the new game
  patch added at least three pieces. Reverse-engineered findings,
  not yet wired into the writer because shipping a partial schema
  edit would corrupt round-trip writes on the unmodelled bytes:

    1. After field 30 ``extract_multi_change_info`` (u32) the new
       layout inserts a struct
         u16 marker (always 0xFFFF), u32-length-prefixed cstring
         (Korean filter-category name like "탄약류 아이템그룹"),
         three u32s (the middle one is a per-group key like 1001367
         or 18010001, the outer two are 0 in every record sampled).
       18+N bytes total.

    2. After field 39 ``is_all_gimmick_sealable`` (u8) the new layout
       inserts an 8-byte marker block
         u8 0, u8 0, u8 0x01, u32 hash 0x9D7C0DD0.
       The hash appears exactly once per record across the whole
       5.5 MB file (6314 hits = 6314 records), so it's a fixed
       schema sentinel and not data.

    3. Record tail. Walking back from each record's known boundary
       (record 0 is exactly 631 bytes, confirmed by sniffing the
       next record header at offset 631 keyed Arrow=50001), the
       last 30 bytes look like this on the arrow-family items:

         offset rec_end-30 to -29  : 00 00            (constant)
         offset rec_end-28 to -25  : E1 53 0F 00 etc  (varies per
                                                       item; LE u32
                                                       reads 1004001
                                                       on most arrows
                                                       and 0 / a
                                                       different key
                                                       on others)
         offset rec_end-24 to -21  : 00 00 00 00      (constant - empty
                                                       cstring length
                                                       most plausibly
                                                       emoji_texture_id)
         offset rec_end-20         : 00               (constant)
         offset rec_end-19         : 01               (CONSTANT 0x01 -
                                                       enable_equip_in
                                                       _clone_actor=1)
         offset rec_end-18         : 00 or 01         (varies - one of
                                                       the is_X booleans)
         offset rec_end-17 to -15  : 00 00 00         (constant)
         offset rec_end-14 to -7   : 00 00 00 00 00 00 00 00
                                     (8 zero bytes  - i64
                                      respawn_time_seconds = 0)
         offset rec_end-6 to -5    : FF FF            (CONSTANT u16
                                                       max_endurance =
                                                       0xFFFF = 65535
                                                       = "unbreakable")
         offset rec_end-4 to -1    : 00 00 00 00      (constant u32
                                                       repair_data_list
                                                       count = 0)

       So the last ~28 bytes ARE part of the original schema (not
       a new 1.09 sentinel block - earlier note was wrong). The
       0xFFFF at -6 is max_endurance=65535, not a marker. The
       0x000F53E1 at -28 to -25 is a per-item key value that varies.

  Middle region (post-field-43 to pre-tail-fields, ~320 bytes for
  Pyeonjeon_Arrow) still misreads. The first u32 at "field 44
  position" reads 65792 (= 0x00010100), which is invalid as a
  sealable carray count. The 0x000F4240 (= 1,000,000) constant
  appears 16 bytes later and recurs across arrow records with
  near-identical surrounding bytes ("00 01 01 00 00 00 00 00 01 02
  00 00 00 40 42 0F"). Without RTTI mapping for the CD 1.09 build
  this region cannot be pinned to specific schema fields with
  confidence; speculating would risk shipping a partial fix that
  corrupts round-trip writes. Next focused session should compare
  byte patterns across non-arrow items (Quiver, MultiArrow,
  Poison_Arrow all give different shapes) to triangulate which
  schema position varies.



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

    __slots__ = ("data", "pos", "rec_end")

    def __init__(
        self,
        data: bytes,
        pos: int = 0,
        rec_end: int | None = None,
    ) -> None:
        self.data = data
        self.pos = pos
        # Optional upper bound for the current record. When known (from
        # the .pabgh boundary walker), forward-walk fallbacks cap their
        # scan range here so an empty-GVP record's needle search doesn't
        # latch onto the next record's GVP.
        self.rec_end = rec_end

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


def _looks_like_record_header(data: bytes, p: int) -> bool:
    """Heuristic: does ``p`` look like the start of an iteminfo record?

    Pattern: u32 key (>=1, <2e6), u32 string_key_len (4..100), then
    ``string_key_len`` ASCII-printable bytes (letters/digits/underscore/
    hyphen), followed by u8 is_blocked (0 or 1) and u64 max_stack_count
    (1..1e8). Used by the streaming parser to bound rec_end when prefab
    parsing over-consumes and we have no .pabgh index to consult.
    """
    if p < 0 or p + 17 > len(data):
        return False
    key = struct.unpack_from("<I", data, p)[0]
    if key == 0 or key > 2_000_000:
        return False
    n = struct.unpack_from("<I", data, p + 4)[0]
    if n < 4 or n > 100:
        return False
    if p + 8 + n + 9 > len(data):
        return False
    sk = data[p + 8:p + 8 + n]
    for b in sk:
        if not (
            (0x41 <= b <= 0x5A)  # A-Z
            or (0x61 <= b <= 0x7A)  # a-z
            or (0x30 <= b <= 0x39)  # 0-9
            or b in (0x5F, 0x2D)  # _ -
        ):
            return False
    blocked = data[p + 8 + n]
    if blocked > 1:
        return False
    max_stack = struct.unpack_from("<Q", data, p + 8 + n + 1)[0]
    if max_stack < 1 or max_stack > 100_000_000:
        return False
    return True


def _find_next_record_start(
    data: bytes, search_start: int, search_end: int
) -> int:
    """Scan [search_start, search_end) for the first plausible record
    header. Returns -1 if none found. Used as a streaming-mode rec_end
    approximation for the prefab opaque-salvage path."""
    if search_end > len(data):
        search_end = len(data)
    p = search_start
    while p < search_end:
        if _looks_like_record_header(data, p):
            return p
        p += 1
    return -1


def parse_iteminfo_from_bytes(data: bytes) -> list[dict]:
    """Parse an entire iteminfo.pabgb body to a list of item dicts.

    Walks records back-to-back from offset 0 to len(data). Each
    record self-describes its size via the schema, no .pabgh index
    needed at parse time. When a record's prefab walker silently
    over-consumes and the post-prefab boundary check needs an
    rec_end anchor, sniff the next plausible record header and use
    it as the bound.
    """
    items: list[dict] = []
    pos = 0
    while pos < len(data):
        rec_start = pos
        # Sniff the next record header within a generous bound so the
        # prefab opaque-salvage path has an rec_end. Search starts a
        # bit past the smallest plausible record size to avoid matching
        # the current record's own embedded text.
        sniff_start = rec_start + 200
        sniff_end = min(rec_start + 30000, len(data))
        next_start = _find_next_record_start(
            data, sniff_start, sniff_end
        )
        if next_start < 0:
            next_start = len(data)
        r = _Reader(data, rec_start, rec_end=next_start)
        items.append(_read_item(r))
        pos = r.pos
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


def parse_record_at(
    data: bytes, offset: int, rec_end: int | None = None
) -> int:
    """Parse one record starting at ``offset`` and return the cursor
    position after the record. Test helper for boundary checks.

    When ``rec_end`` is provided (known from the .pabgh index), it is
    threaded through the reader so forward-walk fallbacks cap their
    needle search at the record boundary instead of latching onto the
    next record's GVP entry.
    """
    r = _Reader(data, offset, rec_end=rec_end)
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


def _read_ItemInfoSharpnessData(r: _Reader, dsi_type: int = 15) -> dict:
    """Post-1.0.4.1 sharpness layout.

    Single shape (W): 13-byte W-header + u32 stat_count + N*12 stats +
    3-byte tail. Empty = 20 bytes.

    The pre-2026-05-08 parser had a "PW" variant that prepended a
    13-byte p_prefix when default_sub_item.type_id == 0. Those 13
    bytes were actually a TRAILING field of default_sub_item, NOT a
    prefix of sharpness_data. The misattribution shifted cooltime/
    unk_post_cooltime_a/b 13 bytes earlier on disk than where mod
    authors target them, which made Format 3 cooltime intents corrupt
    the trailing block and crash the game on launch (hhkbble's
    My_ItemBuffs_Mod on item 1001250 / thief gloves). The 13 bytes
    are now read by _read_DefaultSubItem when type_id < 14.

    CD 1.09 (GitHub #182): when default_sub_item is POPULATED
    (type_id < 14) one extra u8 precedes the W-header. Located by
    correlation — under the 1.09 base schema exactly the records with
    dsi.type_id == 0 misparsed (349/349) while every type_id == 15
    record parsed exact, and consuming one byte here realigns all of
    them (6314/6314 on the 1.09 fixture). The byte is stored as
    ``pre_unk_109`` only when present, so the writer can stay
    symmetric without re-deriving the dsi condition.
    """
    out_pre = None
    if dsi_type < 14:
        out_pre = r.u8()
    # W-header: 13 bytes (u8 unk_a + u16 max_sharpness + u32 craft_tool_info
    # + 6 trailing bytes; trailing bytes empirically zero, treated as opaque)
    w_unk_a = r.u8()
    max_sharpness = r.u16()
    craft_tool_info = r.u32()
    w_trailing = bytes(r.data[r.pos:r.pos + 6])
    r.pos += 6
    # carray<EnchantStatChange> stat_list_static: u32 count + 12*N
    stat_count = r.u32()
    stat_list: list[dict] = []
    for _ in range(stat_count):
        stat_list.append({"stat": r.u32(), "change_mb": r.i64()})
    # 3-byte tail (always zero in fixture; opaque)
    tail = bytes(r.data[r.pos:r.pos + 3])
    r.pos += 3
    out = {
        "shape": "W",
        "p_prefix": None,
        "w_unk_a": w_unk_a,
        "max_sharpness": max_sharpness,
        "craft_tool_info": craft_tool_info,
        "w_trailing": w_trailing,
        "stat_list": stat_list,
        "tail": tail,
    }
    if out_pre is not None:
        out["pre_unk_109"] = out_pre
    return out


def _write_ItemInfoSharpnessData(w: _Writer, v: dict) -> None:
    # PW shape no longer exists; the 13-byte p_prefix is now part of
    # default_sub_item. For mods authored under the pre-fix parser
    # whose data still carries a populated p_prefix, write it through
    # default_sub_item.unk_a/b/c instead. Raw write here would
    # double-count the 13 bytes.
    #
    # CD 1.09 conditional lead byte (see _read_ItemInfoSharpnessData):
    # present iff the reader stored it, so write-by-key-presence keeps
    # the round-trip symmetric without re-deriving the dsi condition.
    if "pre_unk_109" in v:
        w.u8(v["pre_unk_109"])
    w.u8(v.get("w_unk_a", 0))
    w.u16(v.get("max_sharpness", 0))
    w.u32(v.get("craft_tool_info", 0))
    trailing = v.get("w_trailing") or b"\x00" * 6
    w.buf += bytes(trailing)
    stat_list = v.get("stat_list") or []
    w.u32(len(stat_list))
    for s in stat_list:
        w.u32(s["stat"])
        w.i64(s["change_mb"])
    tail = v.get("tail") or b"\x00" * 3
    w.buf += bytes(tail)


def _read_ItemBundleData(r: _Reader) -> dict:
    return {"count_mb": r.u64(), "key": r.u32()}


def _write_ItemBundleData(w: _Writer, v: dict) -> None:
    w.u64(v["count_mb"])
    w.u32(v["key"])


def _read_UnitData(r: _Reader) -> dict:
    # CD 1.10 added a second u32 hash right after icon_path. Verified
    # on record 1's Copper/Silver money units: 1.09 goes icon_path ->
    # localizables directly, 1.10 carries one extra non-constant u32
    # (0xa953c324 on Copper, 0xc52007c6 on Silver) in between.
    # GitHub #182.
    return {
        "ui_component": r.cstring(),
        "minimum": r.u32(),
        "icon_path": r.u32(),
        "icon_path_b_110": r.u32(),
        "item_name": r.localizable(),
        "item_desc": r.localizable(),
    }


def _write_UnitData(w: _Writer, v: dict) -> None:
    w.cstring(v["ui_component"])
    w.u32(v["minimum"])
    w.u32(v["icon_path"])
    w.u32(v.get("icon_path_b_110", 0))
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
    tribe_count = r.u32()
    tribe_gender_list: list[dict] = []
    tribe_opaque = False
    snap_before_tribes = r.pos
    try:
        for i in range(tribe_count):
            tribe_gender_list.append(_read_PrefabDataTribe(r, i, tribe_count))
    except Exception:
        # Family C (264 records, tcnt=11) and other multi-tribe shapes
        # whose layout isn't fully RE'd. Forward-walk to the next field's
        # GVP scale needle and consume the entire tribe block opaquely.
        r.pos = snap_before_tribes
        opaque = _shapeA2_forward_walk(r)
        if opaque is None:
            # Re-raise the original error by retrying parse
            for i in range(tribe_count):
                tribe_gender_list.append(_read_PrefabDataTribe(r, i, tribe_count))
        else:
            tribe_gender_list = [opaque]
            tribe_opaque = True
    return {
        "tag_name_hash": tag_name_hash,
        "prefab_names": prefab_names,
        "equip_slot_list": equip_slot_list,
        "is_craft_material": is_craft_material,
        # Preserve the original tribe_count u32 so the writer reproduces
        # it byte-for-byte even when tribe_gender_list collapses into a
        # single opaque blob via _shapeA2_forward_walk.
        "tribe_count": tribe_count,
        "tribe_opaque": tribe_opaque,
        "tribe_gender_list": tribe_gender_list,
    }


def _write_PrefabData(w: _Writer, v: dict) -> None:
    w.u32(v["tag_name_hash"])
    w.carray(v["prefab_names"], _Writer.u32)
    w.carray(v["equip_slot_list"], _Writer.u32)
    w.u8(v["is_craft_material"])
    # When the tribe block was opaque-salvaged, restore the original
    # tribe_count u32 (the opaque bytes already encode all tribes) so
    # round-trip is byte-identical. Otherwise emit list length as before.
    if v.get("tribe_opaque"):
        w.u32(v.get("tribe_count", len(v["tribe_gender_list"])))
    else:
        w.u32(len(v["tribe_gender_list"]))
    for elem in v["tribe_gender_list"]:
        _write_PrefabDataTribe(w, elem)


def _read_PrefabDataTribe(r: _Reader, elem_index: int = 0, total_count: int = 1) -> dict:
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
    # In multi-tribe records (total_count > 1), every tribe element is Shape A
    # with the per-element index encoded in the first u32 (hash_a). The
    # zero-discriminator only fits tribe[0] of single-tribe records.
    if elem_index > 0 or total_count > 1:
        return _read_PrefabDataTribe_shapeA(r)
    # Shape A3 (cluster A from RESIDUAL_71_findings.md, 9 records keys
    # 1001106..1001115). Fixed 150-byte tribe[0] starting with u16(0) +
    # u32(list_a_count) where list_a_count is small (1..15). Pattern in
    # bytes: 00 00 NN 00 00 00 with NN = la_count low byte. This must be
    # checked BEFORE Shape A2 because Shape A2's discriminator can collide
    # if first list_a entry's stat_key is also small. Without this, the
    # records fall through to Shape B and get parsed bogusly. Route to
    # the GVP forward-walk so the tribe is consumed opaquely.
    if (
        total_count == 1
        and elem_index == 0
        and r.data[r.pos] == 0
        and r.data[r.pos + 1] == 0
        and 1 <= r.data[r.pos + 2] <= 15
        and r.data[r.pos + 3] == 0
        and r.data[r.pos + 4] == 0
        and r.data[r.pos + 5] == 0
    ):
        snap = r.pos
        opaque = _shapeA2_forward_walk(r)
        if opaque is not None:
            return opaque
        r.pos = snap
    if r.data[r.pos:r.pos + 4] == b"\x00\x00\x00\x00":
        # Family E (53 records): Shape A's list_a/list_b parses fine but
        # list_c carries FooterOuter-typed entries instead of TribeStat.
        # Try Shape A first; on any failure (or if Shape A returns but
        # consumed an unreasonable amount of the buffer), fall back to
        # forward-walk to GVP needle.
        snap = r.pos
        try:
            result = _read_PrefabDataTribe_shapeA(r)
            # Sanity check: tribes should be at most ~2KB for single-tribe
            # records. If Shape A consumed more, list_c count was bogus.
            if total_count == 1 and (r.pos - snap) > 2048:
                raise ValueError(
                    f"Shape A consumed {r.pos - snap} bytes (likely Family E)"
                )
            # Boundary check: same as Shape A2.
            if total_count == 1 and elem_index == 0:
                if not _looks_like_gvp_scale_needle(r.data, r.pos + 8):
                    # Find first GVP needle with sane (1-10) preceding count
                    nearby = -1
                    sp = r.pos
                    se = min(r.pos + 1500, len(r.data))
                    if r.rec_end is not None:
                        bounded = r.rec_end - 40
                        if bounded < se:
                            se = bounded
                    while True:
                        cand = _find_gvp_needle(r.data, sp, se)
                        if cand < 0:
                            break
                        if cand - 8 >= r.pos:
                            cnt = struct.unpack_from(
                                "<I", r.data, cand - 8
                            )[0]
                            if 1 <= cnt <= 10:
                                nearby = cand
                                break
                        sp = cand + 1
                    if nearby > 0:
                        end = nearby - 8
                        if end > r.pos:
                            extra = bytes(r.data[r.pos:end])
                            r.pos = end
                            return {
                                "shape": "A_padded",
                                "inner": result,
                                "extra": extra,
                            }
            return result
        except Exception:
            r.pos = snap
            if total_count == 1 and elem_index == 0:
                opaque = _shapeA2_forward_walk(r)
                if opaque is not None:
                    return opaque
            r.pos = snap
            return _read_PrefabDataTribe_shapeA(r)  # re-raise original
    # Shape A2 candidate: first u32 != 0 AND bytes 4..8 = 00 00 00 00
    # (unk_b == 0). chain_outer_cnt at bytes 8..12 may be any small value
    # in {1, 2, 3, 4, 5} per SHAPE_A2_findings_v3.md (Families B/D/F/G with
    # cnt_outer != 1). Try Shape A2 first; if it raises, fall back to
    # Shape B (cat 3601 17-byte layout).
    if (
        r.data[r.pos + 4:r.pos + 8] == b"\x00\x00\x00\x00"
        and r.data[r.pos + 9:r.pos + 12] == b"\x00\x00\x00"
        and 1 <= r.data[r.pos + 8] <= 15
    ):
        cnt_outer_byte = r.data[r.pos + 8]
        snap = r.pos
        # cnt_outer != 1 case: Family B/D/F/G per SHAPE_A2_findings_v3.md.
        # The v1 ODD/EVEN chain walker can't decode this layout and either
        # raises or silently terminates at a false 10-zero match. Skip it
        # and go straight to GVP forward-walk for these records.
        if cnt_outer_byte != 1 and total_count == 1 and elem_index == 0:
            opaque = _shapeA2_forward_walk(r)
            if opaque is not None:
                return opaque
            r.pos = snap
        try:
            result = _read_PrefabDataTribe_shapeA2(r)
            # Sanity check: Shape A2 with cnt_outer=1 typically <= 200 bytes.
            if total_count == 1 and (r.pos - snap) > 2048:
                raise ValueError(
                    f"Shape A2 consumed {r.pos - snap} bytes"
                )
            # Boundary check: when single-tribe and last (only) PrefabData
            # entry, the next field is gimmick_visual_prefab_data_list.
            # If GVP isn't right after our cursor, we've under-consumed
            # the tribe (rare cnt_outer=1 records with extra trailing
            # carray<TribeStat>). Force forward-walk.
            if total_count == 1 and elem_index == 0:
                if not _looks_like_gvp_scale_needle(r.data, r.pos + 8):
                    # Either GVP is empty (count=0) or we under-consumed.
                    # If a needle exists nearby (within 256 bytes), forward
                    # walk to it.
                    # Find first GVP needle with sane (1-10) preceding count
                    nearby = -1
                    sp = r.pos
                    se = min(r.pos + 1500, len(r.data))
                    if r.rec_end is not None:
                        bounded = r.rec_end - 40
                        if bounded < se:
                            se = bounded
                    while True:
                        cand = _find_gvp_needle(r.data, sp, se)
                        if cand < 0:
                            break
                        if cand - 8 >= r.pos:
                            cnt = struct.unpack_from(
                                "<I", r.data, cand - 8
                            )[0]
                            if 1 <= cnt <= 10:
                                nearby = cand
                                break
                        sp = cand + 1
                    if nearby > 0:
                        end = nearby - 8
                        if end > r.pos:
                            extra = bytes(r.data[r.pos:end])
                            r.pos = end
                            return {
                                "shape": "A2_padded",
                                "inner": result,
                                "extra": extra,
                            }
            return result
        except Exception:
            r.pos = snap
            if total_count == 1 and elem_index == 0:
                opaque = _shapeA2_forward_walk(r)
                if opaque is not None:
                    return opaque
            r.pos = snap
            # Fall through to Shape B
    return _read_PrefabDataTribe_shapeB(r)


def _looks_like_gvp_scale_needle(buf, p: int) -> bool:
    """True when buf[p:p+12] reads as 3 identical finite f32s with a
    plausible "scale-shaped" value (per RESIDUAL_20_findings.md).

    Live data shows {1.0, 1.8} and possibly other GVP scale values.
    Predicate: three equal f32s in (0.01, 100.0), strictly finite (NaN/inf
    excluded by the equality + range checks).
    """
    if p < 0 or p + 12 > len(buf):
        return False
    try:
        a, b, c = struct.unpack_from("<fff", buf, p)
    except struct.error:
        return False
    # Equality check excludes NaN automatically.
    if not (a == b == c):
        return False
    if not (0.01 < a < 100.0):
        return False
    return True


def _find_gvp_needle(buf, start: int, end: int) -> int:
    """Scan [start, end) for the first GVP entry start position.

    Returns the absolute position of the 3-equal-f32 scale triple. The
    caller is responsible for the -8 carray count guard. Sliding 1-byte
    window: tribe payloads can be byte-aligned anywhere.
    """
    if end > len(buf):
        end = len(buf)
    p = start
    limit = end - 12
    while p <= limit:
        if _looks_like_gvp_scale_needle(buf, p):
            return p
        p += 1
    return -1


def _shapeA2_forward_walk(r: _Reader) -> dict | None:
    """Forward-walk fallback for Shape A2 cnt_outer != 1 (Families B/D/F/G).

    Scans ahead from the current cursor for the GVP scale needle (3
    identical finite f32s in (0.01, 100.0)) which sits 8 bytes into a
    gimmick_visual_prefab_data_list element when count >= 1. Tribe bytes
    between the cursor and (needle_pos - 8) are consumed opaquely.

    Live data shows scale values {1.0, 1.8} (and possibly more); see
    _looks_like_gvp_scale_needle. Returns a dict with shape='A2_opaque'
    on success, None if no needle is found within a sane range.
    """
    snap = r.pos
    # Cap scan to avoid runaway (tribes observed up to ~1100 bytes per
    # SHAPE_A2_findings_v3 sample data; cap at 1500 bytes to avoid
    # latching onto the GVP of the NEXT record's PrefabData entry, which
    # would push parser cursor beyond the current record boundary).
    scan_end = min(snap + 1500, len(r.data))
    # When the record's true end is known (threaded in from the .pabgh
    # boundary walker), cap scan_end at rec_end minus a safety margin
    # for the downstream fixed-size fields (price_list count u32 +
    # docking_child_data optional flag + a few more u8/u32 trailers).
    # Without this cap, an empty-GVP record would have no needle in its
    # own range and the scan would latch onto the next record's GVP.
    if r.rec_end is not None:
        bounded = r.rec_end - 40
        if bounded < scan_end:
            scan_end = bounded
    # Find needle that also has a SANE GVP count (1-10) at -8 bytes,
    # to filter out false-positive needles inside tribe data.
    search_pos = snap
    needle_pos = -1
    while True:
        cand = _find_gvp_needle(r.data, search_pos, scan_end)
        if cand < 0:
            break
        # Validate: GVP count u32 at cand-8 should be small (1-10).
        if cand - 8 >= snap:
            cnt = struct.unpack_from("<I", r.data, cand - 8)[0]
            if 1 <= cnt <= 10:
                needle_pos = cand
                break
        search_pos = cand + 1
    if needle_pos < 0:
        return None
    # GVP entry layout: u32 tag_hash + 3*f32 scale + carray<u32> + carray<u32> + u8
    # The needle is at offset +4 of the entry. So entry starts at needle_pos - 4.
    # Before the entry is the GVP carray count (u32). So gvp_start = needle_pos - 8.
    end = needle_pos - 8
    if end < snap:
        return None
    opaque = bytes(r.data[snap:end])
    r.pos = end
    return {
        "shape": "A2_opaque",
        "bytes": opaque,
    }


def _read_PrefabDataTribe_shapeA(r: _Reader) -> dict:
    return {
        "shape": "A",
        "hash_a": r.u32(),
        "unk_b": r.u32(),
        "unk_c": r.u16(),
        "list_a": r.carray(_read_TribeRef),
        "list_b": r.carray(_read_TribeRef),
        "list_c": _read_TribeStat_list(r),
    }


def _read_TribeStat_list(r: _Reader) -> list:
    """Read carray<TribeStat> with strategy detection for short(22) vs v3(26).

    Per SIZE_98_findings.md: 162 records use a 26-byte v3 C-element form,
    indistinguishable from the 22-byte short form by per-element bytes alone.
    Discriminator is total cregion size: short = 22*N, v3 = 26*N. The chosen
    form must land the cursor at a valid next-field boundary.

    Strategy:
      1. Snapshot pos. Read count.
      2. Try short (22-byte) read of all N elements via _read_TribeStat.
      3. After short read, peek the bytes that would follow. If they look
         like the next valid field (GVP carray count + GVP element start
         carrying the float-1.0 scale needle 8 bytes after), commit short.
      4. Else rewind and try v3 (26-byte) form.
      5. Else fall back to short (the original behaviour).
    """
    snap_pos = r.pos
    count = r.u32()
    if count == 0:
        return []

    elements_start = r.pos

    def _try_short() -> tuple[list[dict] | None, int]:
        r.pos = elements_start
        out: list[dict] = []
        try:
            for _ in range(count):
                out.append(_read_TribeStat(r))
        except Exception:
            return None, r.pos
        return out, r.pos

    def _try_v3() -> tuple[list[dict] | None, int]:
        r.pos = elements_start
        out: list[dict] = []
        try:
            for _ in range(count):
                if r.pos + 26 > len(r.data):
                    return None, r.pos
                stat_unk1 = r.u32()
                stat_value1 = r.u32()
                stat_unk2 = r.u64()
                stat_unk3 = r.u32()
                stat_unk4 = r.u32()
                stat_unk5 = r.u16()
                out.append({
                    "form": "v3",
                    "stat_unk1": stat_unk1,
                    "stat_value1": stat_value1,
                    "stat_unk2": stat_unk2,
                    "stat_unk3": stat_unk3,
                    "stat_unk4": stat_unk4,
                    "stat_unk5": stat_unk5,
                })
        except Exception:
            return None, r.pos
        return out, r.pos

    GVP_NEEDLE = b"\x00\x00\x80\x3F\x00\x00\x80\x3F\x00\x00\x80\x3F"

    def _looks_like_next_field(pos: int) -> bool:
        """Tight boundary check: when this is the last tribe element AND
        GVP list has >= 1 entry, the bytes at pos are
        ``u32 gvp_count(=>=1) + u32 gvp_tag_name_hash + GVP_NEEDLE``. So
        the needle should sit at exactly pos+8.

        Falls back to a softer check (needle anywhere in next 64 bytes)
        when the tight check fails, to handle multi-tribe/empty-GVP
        records.
        """
        if pos < 0 or pos > len(r.data):
            return False
        if r.data[pos + 8:pos + 20] == GVP_NEEDLE:
            return True
        return False

    # Try short first
    short_out, short_pos = _try_short()
    short_ok = short_out is not None and _looks_like_next_field(short_pos)
    if short_ok:
        return short_out

    # Try v3
    v3_out, v3_pos = _try_v3()
    v3_ok = v3_out is not None and _looks_like_next_field(v3_pos)
    if v3_ok and not short_ok:
        return v3_out

    # If neither boundary check matched but short parsed cleanly, prefer
    # short (the historical default). This preserves baseline behaviour
    # for records where the GVP needle isn't directly downstream (e.g.,
    # multi-tribe records where the next field is another tribe element).
    if short_out is not None:
        r.pos = short_pos
        return short_out
    if v3_out is not None:
        r.pos = v3_pos
        return v3_out
    # Last resort: original short read raises naturally
    r.pos = elements_start
    return [_read_TribeStat(r) for _ in range(count)]


def _read_PrefabDataTribe_shapeB(r: _Reader) -> dict:
    return {
        "shape": "B",
        "unk_a": r.u32(),
        "unk_b": r.u64(),
        "data": r.carray(_Reader.u8),
    }


def _read_PrefabDataTribe_shapeA2(r: _Reader) -> dict:
    """Shape A2 (per SHAPE_A2_findings_v2.md): tribe-variant override.

    Layout:
      u32 hash_a (!= 0)
      u32 unk_b (= 0)
      u32 chain_outer_cnt (= 1)
      <chain alternating EVEN(12) / ODD(9) entries> ending at a 10-byte
        zero terminator
      TribeFooter {
        u32 cnt_outer
        for each cnt_outer: FooterOuter {
          u32 hash, u32 value, u64 unk, u32 cnt_inner,
          cnt_inner * FooterInner(20 bytes: u32 a, u32 b, u64 unk, u32 c)
        }
        u32 trailing
      }
    """
    hash_a = r.u32()
    unk_b = r.u32()
    chain_outer_cnt = r.u32()
    chain: list[dict] = []
    depth = 0
    while True:
        if r.pos + 10 > len(r.data):
            raise IndexError("ShapeA2 chain ran past buffer")
        if r.data[r.pos:r.pos + 10] == b"\x00" * 10:
            r.pos += 10
            break
        if depth % 2 == 0:
            entry = {
                "depth": depth,
                "kind": "odd",
                "hash": r.u32(),
                "zero8": r.u8(),
                "cnt": r.u32(),
            }
        else:
            entry = {
                "depth": depth,
                "kind": "even",
                "hash": r.u32(),
                "zero32": r.u32(),
                "cnt": r.u32(),
            }
        chain.append(entry)
        depth += 1

    cnt_outer = r.u32()
    footer_outers: list[dict] = []
    for _ in range(cnt_outer):
        outer = {
            "hash": r.u32(),
            "value": r.u32(),
            "unk": r.u64(),
            "inner": [],
        }
        cnt_inner = r.u32()
        for _ in range(cnt_inner):
            outer["inner"].append({
                "a": r.u32(),
                "b": r.u32(),
                "unk": r.u64(),
                "c": r.u32(),
            })
        footer_outers.append(outer)
    trailing = r.u32()

    return {
        "shape": "A2",
        "hash_a": hash_a,
        "unk_b": unk_b,
        "chain_outer_cnt": chain_outer_cnt,
        "chain": chain,
        "footer_outers": footer_outers,
        "trailing": trailing,
    }


def _write_PrefabDataTribe_shapeA2(w: _Writer, v: dict) -> None:
    w.u32(v["hash_a"])
    w.u32(v["unk_b"])
    w.u32(v["chain_outer_cnt"])
    for entry in v["chain"]:
        if entry["kind"] == "even":
            w.u32(entry["hash"])
            w.u32(entry["zero32"])
            w.u32(entry["cnt"])
        else:
            w.u32(entry["hash"])
            w.u8(entry["zero8"])
            w.u32(entry["cnt"])
    w.u8(0); w.u8(0); w.u8(0); w.u8(0); w.u8(0)
    w.u8(0); w.u8(0); w.u8(0); w.u8(0); w.u8(0)
    w.u32(len(v["footer_outers"]))
    for outer in v["footer_outers"]:
        w.u32(outer["hash"])
        w.u32(outer["value"])
        w.u64(outer["unk"])
        w.u32(len(outer["inner"]))
        for inner in outer["inner"]:
            w.u32(inner["a"])
            w.u32(inner["b"])
            w.u64(inner["unk"])
            w.u32(inner["c"])
    w.u32(v["trailing"])


def _write_PrefabDataTribe(w: _Writer, v: dict) -> None:
    shape = v.get("shape")
    if shape == "A":
        w.u32(v["hash_a"])
        w.u32(v["unk_b"])
        w.u16(v["unk_c"])
        w.carray(v["list_a"], _write_TribeRef)
        w.carray(v["list_b"], _write_TribeRef)
        w.carray(v["list_c"], _write_TribeStat)
    elif shape == "A_padded":
        _write_PrefabDataTribe(w, v["inner"])
        w.buf += bytes(v["extra"])
    elif shape == "A2":
        _write_PrefabDataTribe_shapeA2(w, v)
    elif shape == "A2_opaque":
        w.buf += bytes(v["bytes"])
    elif shape == "A2_padded":
        _write_PrefabDataTribe_shapeA2(w, v["inner"])
        w.buf += bytes(v["extra"])
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
    form = v.get("form")
    if form == "long":
        w.u32(v["stat_unk1"])
        w.u32(v["stat_value1"])
        w.u64(v["stat_unk2"])
        w.u32(v["stat_unk3"])
        w.carray(v["inner"], _write_TribeStatInner)
    elif form == "v3":
        w.u32(v["stat_unk1"])
        w.u32(v["stat_value1"])
        w.u64(v["stat_unk2"])
        w.u32(v["stat_unk3"])
        w.u32(v["stat_unk4"])
        w.u16(v["stat_unk5"])
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

    Post-1.0.4.1 layout for the populated form (type_id < 14):
        u8 type_id + u32 value + i64 unk_a + u32 unk_b + u8 unk_c

    Total: 18 bytes when populated, 1 byte when type_id is the
    sentinel value (14, 15, or 255 — the None forms).

    Pre-2026-05-08 the parser stopped at the u32 value and treated
    the trailing 13-byte block (i64 + u32 + u8) as a p_prefix on
    sharpness_data PW shape. That misattribution was byte-conservative
    on round-trip (parser was internally consistent) but it shifted
    cooltime / unk_post_cooltime_a / unk_post_cooltime_b 13 bytes
    earlier on disk than where mod authors target them via Format 3.
    Cooltime intents at the pre-fix offset corrupted the trailing
    block, which the engine validates on load, and the game crashed
    on launch. Bug confirmed against hhkbble's My_ItemBuffs_Mod on
    item 1001250 (thief gloves cooldown).
    """
    type_id = r.u8()
    if type_id < 14:
        value = r.u32()
        unk_a = r.i64()
        unk_b = r.u32()
        unk_c = r.u8()
        return {
            "type_id": type_id, "value": value,
            "unk_a": unk_a, "unk_b": unk_b, "unk_c": unk_c,
        }
    return {"type_id": type_id, "value": None}


def _write_DefaultSubItem(w: _Writer, v: dict) -> None:
    w.u8(v["type_id"])
    if v["type_id"] < 14:
        w.u32(v["value"])
        w.i64(v.get("unk_a", 0))
        w.u32(v.get("unk_b", 0))
        w.u8(v.get("unk_c", 0))


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
    # CD 1.10 removed the duplicate material_match_info u32 that used
    # to follow material_key (verified on record 10044, the one item
    # where material_key != material_match_info in 1.09: the
    # material_match value is gone from the 1.10 bytes). GitHub #182.
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
    # CD 1.09 removed extract_additional_drop_set_info (the post-
    # 1.0.4.1 u32 that used to sit here): in 1.09+ the u16
    # minimum_extract_enchant_level (0xffff sentinel) follows the
    # extract u32 directly. Verified by byte-diff of record 2200
    # across 1.05/1.09/1.10. GitHub #182.
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
    # CD 1.09 added one u8 here (zero in every sampled record).
    # Located by byte-shift analysis of record 2200: after the
    # extract-block removal the streams realign one byte apart
    # starting exactly between is_housing_only and quick_slot_index.
    # GitHub #182.
    ("unk_flag_109", "u8"),
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


# Lantern records (equip_type_info == LANTERN_EQ_TYPE) carry a 12-byte
# (u32, u32, u32) class-metadata block between material_match_info and
# item_desc. Per RESIDUAL_10_findings.md, this is the only equip_type
# in the fixture that triggers a conditional struct here. Round-trip
# the three u32s as lantern_unk_a/b/c on the record dict.
LANTERN_EQ_TYPE = 0x97C2FAE8


def _read_item(r: _Reader) -> dict:
    out: dict = {}
    for spec in _ITEM_FIELDS:
        name, kind = spec[0], spec[1]
        # Conditional 12-byte block before item_desc on lantern records.
        if name == "item_desc" and out.get("equip_type_info") == LANTERN_EQ_TYPE:
            out["lantern_unk_a"] = r.u32()
            out["lantern_unk_b"] = r.u32()
            out["lantern_unk_c"] = r.u32()
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
            if name == "prefab_data_list":
                # Wrap in try/forward-walk: when prefab_data parsing fails
                # (bogus count from upstream misalignment OR an unhandled
                # tribe shape), advance cursor opaquely to the GVP needle
                # of the next field. Stored as a sentinel dict that the
                # writer detects and emits raw.
                snap = r.pos
                try:
                    parsed = r.carray(spec[2])
                except Exception:
                    parsed = None
                    r.pos = snap
                    opaque = _shapeA2_forward_walk(r)
                    if opaque is not None:
                        out[name] = {
                            "_opaque": True,
                            "bytes": opaque["bytes"],
                        }
                    else:
                        # Re-raise original error
                        r.pos = snap
                        out[name] = r.carray(spec[2])
                if parsed is not None:
                    # Post-prefab boundary sanity check: even when the
                    # carray returned without raising, the inner tribe
                    # walk may have silently misaligned, leaving cursor
                    # short of (or past) the real GVP boundary. The
                    # next field (gimmick_visual_prefab_data_list) is a
                    # carray with a u32 count; observed sane counts in
                    # this dataset are 0..10. If we see a count well
                    # above that, OR a non-zero count without the GVP
                    # scale needle right after, the prefab path
                    # misaligned. Try (1) the rec_end-bounded forward
                    # walk to find a needle, and (2) when that fails,
                    # a small-delta cursor adjustment proven by trial
                    # parsing the rest of the schema to rec_end.
                    suspicious = False
                    if r.pos + 4 <= len(r.data):
                        gvp_cnt = struct.unpack_from(
                            "<I", r.data, r.pos
                        )[0]
                        if gvp_cnt > 100:
                            suspicious = True
                        elif gvp_cnt > 0 and not _looks_like_gvp_scale_needle(
                            r.data, r.pos + 8
                        ):
                            # Non-zero count but no scale needle right
                            # after: the GVP carray's first element
                            # would be malformed. Misalignment.
                            suspicious = True
                        elif gvp_cnt == 0 and r.rec_end is not None:
                            # gvp_cnt==0 looks fine on its face, but if
                            # a GVP scale needle exists anywhere within
                            # the prefab/post-prefab range bounded by
                            # rec_end, the prefab parser silently over-
                            # (or under-) consumed past the real GVP
                            # boundary and we landed on a stray zero.
                            # Trigger opaque salvage so the existing
                            # _shapeA2_forward_walk (which scans from
                            # snap) re-anchors. Validate the needle has
                            # a sane gvp_count at needle-8 to filter
                            # out false-positive triples inside real
                            # prefab tribe data.
                            scan_end = max(snap, r.rec_end - 40)
                            search_pos = snap
                            while True:
                                cand = _find_gvp_needle(
                                    r.data, search_pos, scan_end
                                )
                                if cand < 0:
                                    break
                                if cand - 8 >= snap:
                                    cnt = struct.unpack_from(
                                        "<I", r.data, cand - 8
                                    )[0]
                                    if 1 <= cnt <= 10:
                                        suspicious = True
                                        break
                                search_pos = cand + 1
                    if suspicious:
                        post_pos = r.pos
                        r.pos = snap
                        opaque = _shapeA2_forward_walk(r)
                        if opaque is not None:
                            out[name] = {
                                "_opaque": True,
                                "bytes": opaque["bytes"],
                            }
                        elif r.rec_end is not None:
                            # Forward walk found no needle (record has
                            # an empty GVP). Try a small-delta trial
                            # parse: the prefab parser may have over-
                            # or under-consumed by a few bytes due to
                            # an unhandled tribe sub-shape. The tribe
                            # data we already parsed isn't trustworthy,
                            # so emit the entire prefab block as opaque.
                            r.pos = snap
                            delta = _trial_continue_to_rec_end(
                                r.data, post_pos, r.rec_end, out
                            )
                            if delta is not None:
                                end_pos = post_pos + delta
                                if end_pos >= snap:
                                    out[name] = {
                                        "_opaque": True,
                                        "bytes": bytes(
                                            r.data[snap:end_pos]
                                        ),
                                    }
                                    r.pos = end_pos
                                else:
                                    # Adjusted position would be before
                                    # snap (impossible). Keep parsed.
                                    r.pos = post_pos
                                    out[name] = parsed
                            else:
                                # Trial parse found no valid delta;
                                # keep the (misaligned) parse and let
                                # downstream raise as before.
                                r.pos = post_pos
                                out[name] = parsed
                        else:
                            r.pos = post_pos
                            out[name] = parsed
                    else:
                        out[name] = parsed
            else:
                out[name] = r.carray(spec[2])
        elif kind == "struct":
            if name == "sharpness_data":
                # Sharpness shape (W vs PW) depends on default_sub_item.type_id
                # per SHARPNESS_findings.md: dsi=0 -> PW, else W.
                dsi = out.get("default_sub_item") or {}
                dsi_type = int(dsi.get("type_id", 15))
                out[name] = _read_ItemInfoSharpnessData(r, dsi_type)
            else:
                out[name] = spec[2](r)
        elif kind == "optional":
            out[name] = _read_optional(r, spec[2])
        else:
            raise ValueError(f"unknown kind {kind!r} for field {name!r}")
    return out


def _write_item(w: _Writer, it: dict) -> None:
    for spec in _ITEM_FIELDS:
        name, kind = spec[0], spec[1]
        # Symmetric lantern conditional: emit 12 bytes only when
        # equip_type_info matches. Read values from the record dict.
        if name == "item_desc" and it.get("equip_type_info") == LANTERN_EQ_TYPE:
            w.u32(it["lantern_unk_a"])
            w.u32(it["lantern_unk_b"])
            w.u32(it["lantern_unk_c"])
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
            if isinstance(v, dict) and v.get("_opaque"):
                w.buf += bytes(v["bytes"])
            else:
                w.carray(v, spec[3])
        elif kind == "struct":
            spec[3](w, v)
        elif kind == "optional":
            _write_optional(w, v, spec[3])
        else:
            raise ValueError(f"unknown kind {kind!r} for field {name!r}")


# Cache: fields that come AFTER prefab_data_list. Computed lazily.
_FIELDS_AFTER_PREFAB: list | None = None


def _fields_after_prefab() -> list:
    global _FIELDS_AFTER_PREFAB
    if _FIELDS_AFTER_PREFAB is None:
        out = []
        collect = False
        for spec in _ITEM_FIELDS:
            if collect:
                out.append(spec)
            if spec[0] == "prefab_data_list":
                collect = True
        _FIELDS_AFTER_PREFAB = out
    return _FIELDS_AFTER_PREFAB


def _trial_continue(data: bytes, start: int, rec_end: int,
                    dsi_type: int) -> int:
    """Trial-parse all post-prefab fields starting at ``start``.

    Returns the final cursor position on success, -1 on any exception
    or if the parse runs past ``rec_end``. Discards the parsed values.
    Used by the post-prefab boundary sanity check to verify a candidate
    cursor adjustment lands at exactly rec_end.
    """
    r = _Reader(data, start, rec_end=rec_end)
    try:
        for spec in _fields_after_prefab():
            name, kind = spec[0], spec[1]
            if kind == "u8":
                r.u8()
            elif kind == "u16":
                r.u16()
            elif kind == "u32":
                r.u32()
            elif kind == "u64":
                r.u64()
            elif kind == "i64":
                r.i64()
            elif kind == "f32":
                r.f32()
            elif kind == "cstring":
                r.cstring()
            elif kind == "localizable":
                r.localizable()
            elif kind == "carray_u8":
                r.carray(_Reader.u8)
            elif kind == "carray_u16":
                r.carray(_Reader.u16)
            elif kind == "carray_u32":
                r.carray(_Reader.u32)
            elif kind == "carray_cstring":
                r.carray(_Reader.cstring)
            elif kind == "carray":
                r.carray(spec[2])
            elif kind == "struct":
                if name == "sharpness_data":
                    _read_ItemInfoSharpnessData(r, dsi_type)
                else:
                    spec[2](r)
            elif kind == "optional":
                _read_optional(r, spec[2])
            if r.pos > rec_end:
                return -1
        return r.pos
    except Exception:
        return -1


def _trial_continue_to_rec_end(data: bytes, post_pos: int, rec_end: int,
                                partial_out: dict) -> int | None:
    """Find a small cursor delta s.t. the rest of the schema parses to
    exactly rec_end.

    The prefab parser may over- or under-consume by a few bytes when an
    unhandled tribe sub-shape is silently misparsed. We try small deltas
    around ``post_pos`` and pick whichever one lands the rest-of-schema
    parse at ``rec_end``. Returns the delta (so the new cursor is
    ``post_pos + delta``), or None when no delta in the search range
    works.

    The dsi (default_sub_item) field hasn't been parsed yet (it comes
    after prefab), so we don't know the sharpness form. Try both.
    """
    # Search range: small bounded sweep around post_pos. Empirically
    # the misalignment for the 64 silent-misalign records is exactly 2
    # bytes, but allow a wider window for future variants. Try 0
    # first (trivial: catches any case where the GVP cnt was bogus
    # but the parse happens to land anyway).
    for delta in (0, -2, 2, -4, 4, -1, 1, -3, 3, -6, 6, -8, 8):
        ts = post_pos + delta
        if ts < 0 or ts > rec_end:
            continue
        for dsi in (15, 0):
            if _trial_continue(data, ts, rec_end, dsi) == rec_end:
                return delta
    return None
