"""Type stubs for crimson_rs - Crimson Desert archive toolkit."""

from __future__ import annotations

from typing import TypedDict

from crimson_rs.enums import Compression as Compression
from crimson_rs.enums import Crypto as Crypto
from crimson_rs.enums import Language as Language

# ── Nested Structs ──────────────────────────────────────────────────────────


class LocalizableString(TypedDict):
    category: int
    """Localization category (u8)."""
    index: int
    """Localization table index (u64)."""
    default: str
    """Default string value."""


class OccupiedEquipSlotData(TypedDict):
    equip_slot_name_key: int
    """u32"""
    equip_slot_name_index_list: list[int]
    """list of u8 values."""


class ItemIconData(TypedDict):
    icon_path: int
    """StringInfoKey (u32)."""
    check_exist_sealed_data: int
    """u8"""
    gimmick_state_list: list[int]
    """list of u32."""


class PassiveSkillLevel(TypedDict):
    skill: int
    """SkillKey (u32)."""
    level: int
    """u32"""


class ReserveSlotTargetData(TypedDict):
    reserve_slot_info: int
    """ReserveSlotKey (u32)."""
    condition_info: int
    """ConditionKey (u32)."""


class SocketMaterialItem(TypedDict):
    item: int
    """ItemKey (u32)."""
    value: int
    """u64"""


class EnchantStatChange(TypedDict):
    stat: int
    """StatusKey (u32)."""
    change_mb: int
    """i64"""


class EnchantLevelChange(TypedDict):
    stat: int
    """StatusKey (u32)."""
    change_mb: int
    """i8"""


class EnchantStatData(TypedDict):
    max_stat_list: list[EnchantStatChange]
    regen_stat_list: list[EnchantStatChange]
    stat_list_static: list[EnchantStatChange]
    stat_list_static_level: list[EnchantLevelChange]


class PriceFloor(TypedDict):
    price: int
    """u64"""
    sym_no: int
    """u32"""
    item_info_wrapper: int
    """ItemKey (u32)."""


class ItemPriceInfo(TypedDict):
    key: int
    """ItemKey (u32)."""
    price: PriceFloor


class EquipmentBuff(TypedDict):
    buff: int
    """BuffKey (u32)."""
    level: int
    """u32"""


class EnchantData(TypedDict):
    level: int
    """u16"""
    enchant_stat_data: EnchantStatData
    buy_price_list: list[ItemPriceInfo]
    equip_buffs: list[EquipmentBuff]


class GimmickVisualPrefabData(TypedDict):
    tag_name_hash: int
    """u32"""
    scale: list[float]
    """[f32; 3] - 3 floats."""
    prefab_names: list[int]
    """StringInfoKey list (u32)."""
    animation_path_list: list[int]
    """StringInfoKey list (u32)."""
    use_gimmick_prefab: int
    """u8"""


class GameEventExecuteData(TypedDict):
    game_event_type: int
    """u8"""
    player_condition: int
    """ConditionKey (u32)."""
    target_condition: int
    """ConditionKey (u32)."""
    event_condition: int
    """ConditionKey (u32)."""


class InventoryChangeData(TypedDict):
    game_event_execute_data: GameEventExecuteData
    to_inventory_info: int
    """InventoryKey (u16)."""


class PageData(TypedDict):
    left_page_texture_path: str
    right_page_texture_path: str
    left_page_related_knowledge_info: int
    """KnowledgeKey (u32)."""
    right_page_related_knowledge_info: int
    """KnowledgeKey (u32)."""


class InspectData(TypedDict):
    item_info: int
    """ItemKey (u32)."""
    gimmick_info: int
    """GimmickInfoKey (u32)."""
    character_info: int
    """CharacterKey (u32)."""
    spawn_reason_hash: int
    """u32"""
    socket_name: str
    speak_character_info: int
    """CharacterKey (u32)."""
    inspect_target_tag: int
    """u32"""
    reward_own_knowledge: int
    """u8"""
    reward_knowledge_info: int
    """KnowledgeKey (u32)."""
    item_desc: LocalizableString
    board_key: int
    """u32"""
    inspect_action_type: int
    """u8"""
    gimmick_state_name_hash: int
    """u32"""
    target_page_index: int
    """u32"""
    is_left_page: int
    """u8"""
    target_page_related_knowledge_info: int
    """KnowledgeKey (u32)."""
    enable_read_after_reward: int
    """u8"""
    refer_to_left_page_inspect_data: int
    """u8"""
    inspect_effect_info_key: int
    """EffectKey (u32)."""
    inspect_complete_effect_info_key: int
    """EffectKey (u32)."""


class InspectAction(TypedDict):
    action_name_hash: int
    """u32"""
    catch_tag_name_hash: int
    """u32"""
    catcher_socket_name: str
    catch_target_socket_name: str


class ItemInfoSharpnessData(TypedDict):
    max_sharpness: int
    """u16"""
    craft_tool_info: int
    """CraftToolKey (u16)."""
    stat_data: EnchantStatData


class ItemBundleData(TypedDict):
    count_mb: int
    """u64"""
    key: int
    """GimmickInfoKey (u32)."""


class UnitData(TypedDict):
    ui_component: str
    minimum: int
    """u32"""
    icon_path: int
    """StringInfoKey (u32)."""
    item_name: LocalizableString
    item_desc: LocalizableString


class MoneyUnitEntry(TypedDict):
    key: int
    """u32"""
    value: UnitData


class MoneyTypeDefine(TypedDict):
    price_floor_value: int
    """u64"""
    unit_data_list_map: list[MoneyUnitEntry]


class PrefabData(TypedDict):
    prefab_names: list[int]
    """StringInfoKey list (u32)."""
    equip_slot_list: list[int]
    """u16 list."""
    tribe_gender_list: list[int]
    """StringInfoKey list (u32)."""
    is_craft_material: int
    """u8"""


class RepairData(TypedDict):
    resource_item_info: int
    """ItemKey (u32)."""
    repair_value: int
    """u16"""
    repair_style: int
    """u8"""
    resource_item_count: int
    """u64"""


class SubItem(TypedDict):
    type_id: int
    """u8 variant tag. 0=Item, 3=Character, 9=Gimmick, 14=None."""
    value: int | None
    """Key value (u32) or None for type_id=14."""


class DropDefaultData(TypedDict):
    drop_enchant_level: int
    """u16"""
    socket_item_list: list[int]
    """ItemKey list (u32)."""
    add_socket_material_item_list: list[SocketMaterialItem]
    default_sub_item: SubItem
    socket_valid_count: int
    """u8"""
    use_socket: int
    """u8"""


class SealableItemInfo(TypedDict):
    type_tag: int
    """u8 variant tag. 0=Item, 1=Gimmick, 2=String, 3=Character, 4=Tribe."""
    item_key: int
    """ItemKey (u32)."""
    unknown0: int
    """u64"""
    value: int | str
    """Key value (u32) for types 0/1/3/4, or str for type 2."""


class DockingChildData(TypedDict):
    gimmick_info_key: int
    """GimmickInfoKey (u32)."""
    character_key: int
    """CharacterKey (u32)."""
    item_key: int
    """ItemKey (u32)."""
    attach_parent_socket_name: str
    attach_child_socket_name: str
    docking_tag_name_hash: list[int]
    """[u32; 4] - 4 ints."""
    docking_equip_slot_no: int
    """u16"""
    spawn_distance_level: int
    """u32"""
    is_item_equip_docking_gimmick: int
    """u8"""
    send_damage_to_parent: int
    """u8"""
    is_body_part: int
    """u8"""
    docking_type: int
    """u8"""
    is_summoner_team: int
    """u8"""
    is_player_only: int
    """u8"""
    is_npc_only: int
    """ConditionKey (u32)."""
    is_sync_break_parent: int
    """u8"""
    hit_part: int
    """u8"""
    detected_by_npc: int
    """u8"""
    is_bag_docking: int
    """u8"""
    enable_collision: int
    """u8"""
    disable_collision_with_other_gimmick: int
    """u8"""
    docking_slot_key: str


# ── ItemInfo ────────────────────────────────────────────────────────────────


class ItemInfo(TypedDict):
    """A single item parsed from the iteminfo binary file."""

    # Identity
    key: int
    """Unique item ID. ItemKey (u32)."""
    string_key: str
    """String identifier, e.g. ``"Pyeonjeon_Arrow"``."""
    is_blocked: int
    """u8"""
    max_stack_count: int
    """u64"""
    item_name: LocalizableString
    broken_item_prefix_string: int
    """LocalStringInfoKey (u32)."""

    # Inventory & Equipment
    inventory_info: int
    """InventoryKey (u16)."""
    equip_type_info: int
    """EquipTypeKey (u32)."""
    occupied_equip_slot_data_list: list[OccupiedEquipSlotData]
    item_tag_list: list[int]
    """u32 list."""
    equipable_hash: int
    """u32"""
    consumable_type_list: list[int]
    """u32 list."""
    item_use_info_list: list[int]
    """ItemUseKey list (u32)."""
    item_icon_list: list[ItemIconData]
    map_icon_path: int
    """StringInfoKey (u32)."""
    money_icon_path: int
    """StringInfoKey (u32)."""
    use_map_icon_alert: int
    """u8"""
    item_type: int
    """u8"""
    material_key: int
    """u32"""
    material_match_info: int
    """MaterialMatchKey (u32)."""
    item_desc: LocalizableString
    item_desc2: LocalizableString
    equipable_level: int
    """u32"""
    category_info: int
    """CategoryKey (u16)."""
    knowledge_info: int
    """KnowledgeKey (u32)."""
    knowledge_obtain_type: int
    """u8"""
    destroy_effec_info: int
    """EffectKey (u32)."""
    equip_passive_skill_list: list[PassiveSkillLevel]
    use_immediately: int
    """u8"""
    apply_max_stack_cap: int
    """u8"""
    extract_multi_change_info: int
    """MultiChangeKey (u32)."""
    item_memo: str
    filter_type: str
    gimmick_info: int
    """GimmickInfoKey (u32)."""
    gimmick_tag_list: list[str]
    max_drop_result_sub_item_count: int
    """u32"""
    use_drop_set_target: int
    """u8"""
    is_all_gimmick_sealable: int
    """u8"""
    sealable_item_info_list: list[SealableItemInfo]
    sealable_character_info_list: list[SealableItemInfo]
    sealable_gimmick_info_list: list[SealableItemInfo]
    sealable_gimmick_tag_list: list[SealableItemInfo]
    sealable_tribe_info_list: list[SealableItemInfo]
    sealable_money_info_list: list[int]
    """ItemKey list (u32)."""
    delete_by_gimmick_unlock: int
    """u8"""
    gimmick_unlock_message_local_string_info: int
    """LocalStringInfoKey (u32)."""
    can_disassemble: int
    """u8"""
    transmutation_material_gimmick_list: list[int]
    """GimmickInfoKey list (u32)."""
    transmutation_material_item_list: list[int]
    """ItemKey list (u32)."""
    transmutation_material_item_group_list: list[int]
    """ItemGroupKey list (u16)."""
    is_register_trade_market: int
    """u8"""
    multi_change_info_list: list[int]
    """MultiChangeKey list (u32)."""
    is_editor_usable: int
    """u8"""
    discardable: int
    """u8"""
    is_dyeable: int
    """u8"""
    is_editable_grime: int
    """u8"""
    is_destroy_when_broken: int
    """u8"""
    quick_slot_index: int
    """u8"""
    reserve_slot_target_data_list: list[ReserveSlotTargetData]
    item_tier: int
    """u8"""
    is_important_item: int
    """u8"""
    apply_drop_stat_type: int
    """u8"""
    drop_default_data: DropDefaultData
    prefab_data_list: list[PrefabData]
    enchant_data_list: list[EnchantData]
    gimmick_visual_prefab_data_list: list[GimmickVisualPrefabData]
    price_list: list[ItemPriceInfo]
    docking_child_data: DockingChildData | None
    inventory_change_data: InventoryChangeData | None
    fixed_page_data_list: list[PageData]
    dynamic_page_data_list: list[PageData]
    inspect_data_list: list[InspectData]
    inspect_action: InspectAction
    default_sub_item: SubItem
    cooltime: int
    """i64"""
    item_charge_type: int
    """u8"""
    sharpness_data: ItemInfoSharpnessData
    max_charged_useable_count: int
    """u32"""
    hackable_character_group_info_list: list[int]
    """CharacterGroupKey list (u16)."""
    item_group_info_list: list[int]
    """ItemGroupKey list (u16)."""
    discard_offset_y: float
    """f32"""
    hide_from_inventory_on_pop_item: int
    """u8"""
    is_shield_item: int
    """u8"""
    is_tower_shield_item: int
    """u8"""
    is_wild: int
    """u8"""
    packed_item_info: int
    """ItemKey (u32)."""
    unpacked_item_info: int
    """ItemKey (u32)."""
    convert_item_info_by_drop_npc: int
    """ItemKey (u32)."""
    look_detail_game_advice_info_wrapper: int
    """GameAdviceInfoKey (u32)."""
    look_detail_mission_info: int
    """MissionKey (u32)."""
    enable_alert_system_to_ui: int
    """u8"""
    usable_alert: int
    """u8"""
    is_save_game_data_at_use_item: int
    """u8"""
    is_logout_at_use_item: int
    """u8"""
    shared_cool_time_group_name_hash: int
    """u32"""
    item_bundle_data_list: list[ItemBundleData]
    money_type_define: MoneyTypeDefine | None
    emoji_texture_id: str
    enable_equip_in_clone_actor: int
    """u8"""
    is_blocked_store_sell: int
    """u8"""
    is_preorder_item: int
    """u8"""
    respawn_time_seconds: int
    """i64"""
    max_endurance: int
    """u16"""
    repair_data_list: list[RepairData]


# ── Module Functions ────────────────────────────────────────────────────────


def parse_iteminfo_from_file(path: str) -> list[ItemInfo]:
    """Parse all items from a binary file.

    Args:
        path: Path to the decompressed iteminfo binary file.

    Returns:
        List of item dicts with full type info.

    Raises:
        IOError: If the file cannot be read.
        ValueError: On parse errors.
    """
    ...


def parse_iteminfo_from_bytes(data: bytes) -> list[ItemInfo]:
    """Parse all items from raw bytes.

    Args:
        data: Raw binary data.

    Returns:
        List of item dicts.

    Raises:
        ValueError: On parse errors.
    """
    ...


def write_iteminfo_to_file(items: list[ItemInfo], path: str) -> None:
    """Serialize items and write to a file.

    Args:
        items: List of item dicts (same structure as returned by parse_iteminfo_from_file).
        path: Output file path.

    Raises:
        IOError: On write failure.
        KeyError: If a required field is missing.
        ValueError: On invalid data.
    """
    ...


def serialize_iteminfo(items: list[ItemInfo]) -> bytes:
    """Serialize items to raw bytes.

    Args:
        items: List of item dicts.

    Returns:
        Binary data as bytes.
    """
    ...


# ── Localization ──────────────────────────────────────────────────────────


class LocalizationEntry(TypedDict):
    unk_id: int
    """u64"""
    string_key: str
    """String key identifier."""
    string_value: str
    """Localized string value (UTF-8)."""


def parse_paloc_bytes(data: bytes) -> list[LocalizationEntry]:
    """Parse localization entries from raw bytes."""
    ...


def serialize_paloc(items: list[LocalizationEntry]) -> bytes:
    """Serialize localization entries to raw bytes."""
    ...


# ── Checksum ───────────────────────────────────────────────────────────────


def calculate_checksum(data: bytes) -> int:
    """Compute the Jenkins hashlittle2 checksum used by Crimson Desert file formats.

    Args:
        data: Raw bytes to checksum.

    Returns:
        u32 checksum value.
    """
    ...


# ── PAPGT Types ────────────────────────────────────────────────────────────


class PapgtEntry(TypedDict):
    group_name: str
    """Pack group name (e.g. "0000", "0001")."""
    is_optional: int
    """u8"""
    language: int
    """u16 bitmask (LanguageType flags)."""
    always_zero: int
    """u8"""
    group_name_offset: int
    """u32 — offset in the group names buffer."""
    pack_meta_checksum: int
    """u32 — checksum of the corresponding 0.pamt file."""


class PapgtData(TypedDict):
    unknown0: int
    """u32"""
    checksum: int
    """u32 — checksum of the post-header data."""
    unknown1: int
    """u8"""
    unknown2: int
    """u16"""
    entries: list[PapgtEntry]


def parse_papgt_file(path: str) -> PapgtData:
    """Parse a PAPGT file (pack group tree meta).

    Args:
        path: Path to the .papgt file.

    Returns:
        Dict with header fields and list of entries.
    """
    ...


def parse_papgt_bytes(data: bytes) -> PapgtData:
    """Parse a PAPGT from raw bytes."""
    ...


def write_papgt_file(data: PapgtData, path: str) -> None:
    """Serialize PAPGT data and write to a file."""
    ...


def serialize_papgt(data: PapgtData) -> bytes:
    """Serialize PAPGT data to raw bytes."""
    ...


# ── PAMT Types ─────────────────────────────────────────────────────────────


class PamtEncryptInfo(TypedDict):
    unknown0: int
    """u8"""
    encrypt_info: bytes
    """3 bytes of encryption key material."""


class PamtChunk(TypedDict):
    id: int
    """u32 — paz file ID."""
    checksum: int
    """u32 — CRC32 of the paz file."""
    size: int
    """u32 — total paz file size."""


class PamtFile(TypedDict):
    name: str
    """Resolved file name."""
    name_offset: int
    """u32 — offset in file names trie buffer."""
    chunk_offset: int
    """u32 — offset within the paz chunk."""
    compressed_size: int
    """u32"""
    uncompressed_size: int
    """u32"""
    chunk_id: int
    """u16 — references a chunk by ID."""
    flags: int
    """u8 — raw flags byte."""
    unknown0: int
    """u8"""
    compression: int
    """u8 — decoded compression type (0=None, 2=LZ4, 3=Zlib, 4=QuickLZ)."""
    crypto: int
    """u8 — decoded crypto type (0=None, 1=ICE, 2=AES, 3=ChaCha20)."""
    is_partial: bool
    """Whether this is a partial-compression entry."""


class PamtDirectory(TypedDict):
    path: str
    """Resolved directory path."""
    name_checksum: int
    """u32"""
    name_offset: int
    """i32 — offset in dir names trie buffer (-1 for root)."""
    file_start_index: int
    """u32"""
    file_count: int
    """u32"""
    files: list[PamtFile]


class PamtData(TypedDict):
    checksum: int
    """u32"""
    unknown0: int
    """u16"""
    encrypt_info: PamtEncryptInfo
    chunks: list[PamtChunk]
    directories: list[PamtDirectory]
    _dir_names_buffer: bytes
    """Raw trie buffer (needed for roundtrip serialization)."""
    _file_names_buffer: bytes
    """Raw trie buffer (needed for roundtrip serialization)."""


def parse_pamt_file(path: str) -> PamtData:
    """Parse a PAMT file (pack meta / VFS listing).

    Args:
        path: Path to the .pamt file.

    Returns:
        Dict with header, chunks, and directory/file listings.
    """
    ...


def parse_pamt_bytes(data: bytes) -> PamtData:
    """Parse a PAMT from raw bytes."""
    ...


def write_pamt_file(data: PamtData, path: str) -> None:
    """Serialize PAMT data and write to a file."""
    ...


def serialize_pamt(data: PamtData) -> bytes:
    """Serialize PAMT data to raw bytes."""
    ...


# ── Compression ───────────────────────────────────────────────────────────


def compress_data(data: bytes, compression: int) -> bytes:
    """Compress data using the specified algorithm.

    Args:
        data: Raw bytes to compress.
        compression: 0=None, 2=LZ4, 3=Zlib.

    Returns:
        Compressed bytes.
    """
    ...


def decompress_data(data: bytes, compression: int, uncompressed_size: int) -> bytes:
    """Decompress data using the specified algorithm.

    Args:
        data: Compressed bytes.
        compression: 0=None, 2=LZ4, 3=Zlib.
        uncompressed_size: Expected size after decompression.

    Returns:
        Decompressed bytes.
    """
    ...


# ── Pack Group Builder ────────────────────────────────────────────────────


class PackGroupBuilder:
    """Streaming pack group builder that writes .paz files to disk incrementally.

    Usage::

        builder = PackGroupBuilder("/path/to/0036", compression=2)
        builder.add_file("textures", "icon.dds", raw_bytes)
        builder.add_file_from_path("models", "mesh.obj", "/path/to/mesh.obj")
        pamt_bytes = builder.finish()  # writes .paz + 0.pamt to output_dir
    """

    def __init__(
        self,
        output_dir: str,
        compression: int = 2,
        crypto: int = 0,
        encrypt_info: bytes = b"\x00\x00\x00",
        max_chunk_size: int = 500_000_000,
    ) -> None: ...

    def add_file(self, dir_path: str, file_name: str, data: bytes) -> None:
        """Add a file from raw bytes. Data is compressed/encrypted and appended
        to the current .paz chunk immediately."""
        ...

    def add_file_from_path(self, dir_path: str, file_name: str, file_path: str) -> None:
        """Add a file by reading from a path on disk."""
        ...

    def finish(self) -> bytes:
        """Flush remaining chunk, write 0.pamt to output_dir.
        Returns the raw PAMT bytes (for computing checksum for PAPGT)."""
        ...


def add_papgt_entry(
    papgt_data: PapgtData,
    group_name: str,
    pack_meta_checksum: int,
    is_optional: int,
    language: int,
) -> PapgtData:
    """Add a new entry to a PAPGT dict.

    Args:
        papgt_data: Existing PAPGT data dict.
        group_name: Pack group name (e.g. "0036").
        pack_meta_checksum: Checksum of the group's 0.pamt post-header data.
        is_optional: Whether this group is optional (0 or 1).
        language: Language bitmask (0x3FFF for ALL).

    Returns:
        Updated PAPGT data dict with the new entry and recalculated checksum.
    """
    ...


# ── File Extraction ──────────────────────────────────────────────────────


def extract_file(
    game_dir: str,
    group_name: str,
    dir_path: str,
    file_name: str,
) -> bytes:
    """Extract a single file from a pack group archive.

    Reads the PAMT index, locates the file in the .paz chunk,
    decrypts and decompresses it.

    Args:
        game_dir: Path to the game installation directory.
        group_name: Pack group name (e.g. "0008").
        dir_path: Directory path within the archive (e.g. "gamedata/binary__/client/bin").
        file_name: File name (e.g. "iteminfo.pabgb").

    Returns:
        Raw decompressed file data as bytes.

    Raises:
        IOError: If the PAZ file cannot be read.
        ValueError: If the directory or file is not found in the PAMT.
    """
    ...
