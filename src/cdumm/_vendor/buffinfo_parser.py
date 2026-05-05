"""buffinfo.pabgb byte walker for CDUMM Format 3 apply path.

Decodes the BuffInfo wrapper struct (13 fields per entry) and exposes
per-field byte offsets so ``_intents_to_v2_changes`` can emit byte
patches at the right location for intents like::

    {"entry": "BuffLevel_Socket_ContributionExp",
     "key": 1000114,
     "field": "min_level",
     "op": "set", "new": 1}

The variable-length ``buff_data_list`` region is currently treated as
an opaque byte slice , each item is a tagged variant from a 120-
member family that this module doesn't yet decode. The wrapper-level
fields (key, name, is_blocked, count, min/max level, sequencer name,
template/component, status info, flags) ARE decoded and round-trip
byte-perfectly.

On-disk layout (verified against all 280 entries of CD v1.05 vanilla
buffinfo.pabgb)::

    [ 0:  4]  key                              u32 LE
    [ 4:  8]  name length                      u32 LE
    [ 8:N0]   name                             utf-8
    [N0    ]  is_blocked                       u8
    [N0+1 :N0+5]  buff_data_list count         u32 LE
    [N0+5 :N1]    buff_data_list items         opaque (variant tags)
    [N1   :N1+4]  min_level                    u32 LE
    [N1+4 :N1+8]  max_level                    u32 LE
    [N1+8 :N1+12] sequencer_file_name length   u32 LE
    [N1+12:N2]    sequencer_file_name          utf-8
    [N2   ]   buff_level_calculate_type        u8
    [N2+1 :N2+5]  ui_template_name             u32 LE
    [N2+5 :N2+9]  ui_component_name            u32 LE
    [N2+9 :N2+13] elemental_status_info        u32 LE
    [N2+13]   is_use_skill_info_pattern_descr  u8
    [N2+14]   use_counting_by_global_timer     u8
    [end of entry]

The opaque ``[N0+5 : N1]`` region is located by walking BACKWARD from
the entry's known end size: the trailing 15 bytes are fixed-width
fields, before that comes the sequencer_file_name CString, before
that two u32 levels. Solving the fixed-point relation
``cstring_len_at_pos == cstring_len`` finds N1 deterministically.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


# Trailing fixed-width region after sequencer_file_name's UTF-8 bytes.
# Comprises buff_level_calculate_type (1) + ui_template_name (4) +
# ui_component_name (4) + elemental_status_info (4) +
# is_use_skill_info_pattern_description (1) + use_counting_by_global_timer (1).
_TRAILING_FIXED_BYTES = 15

# Bytes between buff_data_list items end and the sequencer_file_name
# length prefix: min_level (4) + max_level (4). The length prefix
# itself sits AT this offset and is what the back-walk searches for,
# so it's deliberately excluded here.
_POST_ITEMS_PRE_CSTRING_BYTES = 8

# Per-item header bytes preceding the optional payload:
# 4 bytes prefix integer + 1 byte absent indicator.
_ITEM_HEADER_BYTES = 5

# Offsets within the BuffDataBase (the common payload prefix that
# follows the item header when absent_flag == 0). All offsets are
# relative to the start of the payload (i.e. position + 5 from the
# start of the item).
_DBASE_TAG = 0
_DBASE_ID = 1
_DBASE_NAME_ID = 5
_DBASE_FLAGS_A = 9
_DBASE_FLAGS_B = 10
_DBASE_QWORD_A = 11
_DBASE_QWORD_B = 19
_DBASE_QWORD_C = 27
_DBASE_ASSET_PATH_LEN = 35  # u32 length, then UTF-8 bytes
# Fields whose positions depend on the asset_path cstring length get
# computed at parse time. The constants below are the *offsets within
# the post-cstring region*, i.e. relative to (asset_path_end_offset).
_AFTER_CSTRING_CATEGORY = 0
_AFTER_CSTRING_BY58 = 4
_AFTER_CSTRING_LOOKUP_A_60 = 5
_AFTER_CSTRING_LOOKUP_B_62 = 9
_AFTER_CSTRING_LOOKUP_C_64 = 13
_AFTER_CSTRING_LOOKUP_D_66 = 17
_AFTER_CSTRING_BY68 = 21
_AFTER_CSTRING_BY69 = 22
_AFTER_CSTRING_LOOKUP_88 = 23
_AFTER_CSTRING_LOOKUP_90 = 27
_AFTER_CSTRING_FIRST_ARRAY_LEN = 31
# After the first u32-array (length-prefixed): 5 u32 fields then a
# second u32-array, then a 1-byte and a final u32.
_AFTER_FIRST_ARRAY_U32_AT128 = 0
_AFTER_FIRST_ARRAY_U32_AT72 = 4
_AFTER_FIRST_ARRAY_U32_AT76 = 8
_AFTER_FIRST_ARRAY_U32_AT80 = 12
_AFTER_FIRST_ARRAY_U32_AT84 = 16
_AFTER_FIRST_ARRAY_SECOND_ARRAY_LEN = 20
_AFTER_SECOND_ARRAY_BY132 = 0
_AFTER_SECOND_ARRAY_U32_AT136 = 1
_DBASE_FIXED_TAIL_BYTES = 5  # by132 + u32_at136

# Per-variant tail size in bytes (the bytes that follow the 28-field
# common payload prefix, before the next item begins). Two derivation
# rounds:
#
#  Round 1 (single-item entries): for an entry with count=1 and a
#  present item, items_total - 5 - csize = tail. Found 9 sizes.
#
#  Round 2 (homogeneous N-item entries): for an entry where ALL N
#  items have the same tag and same csize,
#  tail = items_total/N - 5 - csize. Confirmed by walking the entry
#  with the candidate tail and verifying every item's tag and csize
#  match. When multiple homogeneous entries with the same tag agree
#  on tail, it's confirmed; if they disagree, the tag has a variable
#  tail and stays unknown.
#
#  Cross-validation: walking ALL 280 vanilla entries with this
#  expanded table walks 198 to completion with zero contradictions
#  (no entry overshoots/undershoots min_level_offset).
#
# Tags missing from this table that appear in vanilla but have a
# VARIABLE tail (multiple sizes observed across homogeneous entries):
#   17 (sizes {0, 41, 42}), 95 (sizes {5, 12}), 37 ({11, 15}),
#   115 ({38, 42, 54, 66, 74}). These need per-tag structural
#   decoders, deferred. Note: tag 17:0 IS kept here because round 1
#   single-item evidence gives that exact value, and it walks 1
#   vanilla entry without contradiction. Removing it loses that
#   coverage; keeping it is safe because no walk yet observed has
#   contradicted it.
_VARIANT_TAIL_SIZES: dict[int, int] = {
    0: 117,   # round 1
    1: 29,    # round 2: 1 homogeneous entry
    2: 12,    # round 2: 5 homogeneous entries
    3: 12,    # round 1
    5: 33,    # round 2: 2 homogeneous entries
    6: 30,    # round 2: 2 homogeneous entries
    7: 20,    # round 1
    12: 28,   # round 2: 5 homogeneous entries
    14: 12,   # round 2: 5 homogeneous entries
    17: 0,    # round 1 (variable in round 2; keeping single-item value)
    19: 13,   # round 2: 2 homogeneous entries
    24: 13,   # round 2: 1 homogeneous entry
    30: 5,    # round 2: 3 homogeneous entries
    54: 14,   # round 1
    59: 17,   # round 2: 1 homogeneous entry
    65: 12,   # round 1
    70: 8,    # round 1
    74: 0,    # round 2: 1 homogeneous entry
    80: 8,    # round 1
    82: 16,   # round 1
    89: 4,    # round 2: 1 homogeneous entry
    90: 12,   # round 2: 1 homogeneous entry
    104: 9,   # round 2: 8 homogeneous entries
    105: 5,   # round 2: 5 homogeneous entries
    106: 12,  # round 2: 6 homogeneous entries
    107: 2,   # round 2: 1 homogeneous entry
    109: 4,   # round 2: 1 homogeneous entry
    116: 12,  # round 2: 3 homogeneous entries
}


@dataclass(frozen=True)
class BuffItemHeader:
    """Header that precedes each entry in the ``buff_data_list``
    region. Two fields:

    * ``prefix_id`` , 4-byte unsigned integer (purpose unclear from
      vanilla data alone, always observed as 1; we read and
      round-trip it verbatim).
    * ``absent_flag`` , 1 byte. ``0x00`` means the item is present
      and a payload of variable length follows. Any non-zero value
      means the item is absent and no payload is present (the next
      item's header follows immediately).

    ``payload_offset`` is the byte offset within the entry where the
    optional payload starts (just past the 5-byte header). It's
    meaningful only when ``absent_flag == 0``.
    """
    prefix_id: int
    prefix_id_offset: int
    absent_flag: int
    absent_flag_offset: int
    payload_offset: int


@dataclass(frozen=True)
class BuffPayloadCommon:
    """Decoded common-prefix region of one buff_data item's payload.

    This is the structure that every present (``absent_flag == 0``)
    item starts with, before the variant-specific tail. It contains
    28 fields , a mix of fixed-width primitives plus three
    variable-length sub-records (one UTF-8 cstring and two
    length-prefixed u32 arrays).

    Each ``_offset`` field is a byte position within the entry that
    CDUMM's intent expander targets for byte patches. The field
    names match the public mod-schema identifiers used in intent
    paths like ``buff_data_list[0].data.base.flags_a``.

    ``end_offset`` is the byte position immediately after this
    common prefix , i.e. where the variant-specific tail begins.
    """
    tag: int
    tag_offset: int
    id: int
    id_offset: int
    name_id: int
    name_id_offset: int
    flags_a: int
    flags_a_offset: int
    flags_b: int
    flags_b_offset: int
    qword_a: int
    qword_a_offset: int
    qword_b: int
    qword_b_offset: int
    qword_c: int
    qword_c_offset: int
    asset_path: str
    asset_path_offset: int  # offset of the length u32
    category: int
    category_offset: int
    by58: int
    by58_offset: int
    lookup_a_60: int
    lookup_a_60_offset: int
    lookup_b_62: int
    lookup_b_62_offset: int
    lookup_c_64: int
    lookup_c_64_offset: int
    lookup_d_66: int
    lookup_d_66_offset: int
    by68: int
    by68_offset: int
    by69: int
    by69_offset: int
    lookup_88: int
    lookup_88_offset: int
    lookup_90: int
    lookup_90_offset: int
    first_array: tuple[int, ...]
    first_array_offset: int  # offset of the length u32
    u32_at128: int
    u32_at128_offset: int
    u32_at72: int
    u32_at72_offset: int
    u32_at76: int
    u32_at76_offset: int
    u32_at80: int
    u32_at80_offset: int
    u32_at84: int
    u32_at84_offset: int
    second_array: tuple[int, ...]
    second_array_offset: int
    by132: int
    by132_offset: int
    u32_at136: int
    u32_at136_offset: int
    end_offset: int


def _read_u32_array(
    entry_bytes: bytes, position: int,
) -> tuple[tuple[int, ...], int]:
    """Read a length-prefixed u32 array at ``position``. Returns
    ``(elements, bytes_consumed_total)`` where bytes_consumed_total
    includes the 4-byte length prefix and ``4 * count`` element bytes.
    """
    if position + 4 > len(entry_bytes):
        raise ValueError(
            f"u32 array length prefix out of range at {position}")
    count = struct.unpack_from("<I", entry_bytes, position)[0]
    if count > 1_000_000:
        raise ValueError(
            f"implausible u32 array length {count} at {position}")
    body_pos = position + 4
    body_end = body_pos + 4 * count
    if body_end > len(entry_bytes):
        raise ValueError(
            f"u32 array of {count} elements overflows entry at "
            f"{position}")
    elements = struct.unpack_from(
        f"<{count}I", entry_bytes, body_pos) if count else ()
    return tuple(elements), 4 + 4 * count


def parse_payload_common(
    entry_bytes: bytes, position: int,
) -> BuffPayloadCommon:
    """Decode the BuffPayloadCommon region starting at ``position``.

    ``position`` is the byte offset within ``entry_bytes`` where the
    payload begins (i.e. just past the item header's 5 bytes when
    absent_flag is 0). Raises ``ValueError`` on any out-of-range
    read.
    """
    if position < 0 or position >= len(entry_bytes):
        raise ValueError(
            f"payload position {position} out of range for entry "
            f"of size {len(entry_bytes)}")

    # Fixed-width prefix (35 bytes) up to the asset_path cstring.
    end_of_fixed = position + _DBASE_ASSET_PATH_LEN
    if end_of_fixed + 4 > len(entry_bytes):
        raise ValueError(
            "buff item payload truncated within fixed prefix")
    tag = entry_bytes[position + _DBASE_TAG]
    id_val = struct.unpack_from(
        "<I", entry_bytes, position + _DBASE_ID)[0]
    name_id = struct.unpack_from(
        "<I", entry_bytes, position + _DBASE_NAME_ID)[0]
    flags_a = entry_bytes[position + _DBASE_FLAGS_A]
    flags_b = entry_bytes[position + _DBASE_FLAGS_B]
    qword_a = struct.unpack_from(
        "<Q", entry_bytes, position + _DBASE_QWORD_A)[0]
    qword_b = struct.unpack_from(
        "<Q", entry_bytes, position + _DBASE_QWORD_B)[0]
    qword_c = struct.unpack_from(
        "<Q", entry_bytes, position + _DBASE_QWORD_C)[0]

    # asset_path cstring at position + 35.
    cstring_len_pos = position + _DBASE_ASSET_PATH_LEN
    asset_path_len = struct.unpack_from(
        "<I", entry_bytes, cstring_len_pos)[0]
    if asset_path_len > 1_000_000:
        raise ValueError(
            f"implausible asset_path length {asset_path_len}")
    asset_path_data_pos = cstring_len_pos + 4
    asset_path_end = asset_path_data_pos + asset_path_len
    if asset_path_end > len(entry_bytes):
        raise ValueError("asset_path overflows entry")
    asset_path = entry_bytes[
        asset_path_data_pos:asset_path_end].decode(
            "utf-8", errors="replace")

    # Post-cstring fixed region (31 bytes up to the first array).
    after_cs = asset_path_end
    needed = after_cs + _AFTER_CSTRING_FIRST_ARRAY_LEN + 4
    if needed > len(entry_bytes):
        raise ValueError(
            "buff item payload truncated in post-cstring region")
    category = struct.unpack_from(
        "<I", entry_bytes,
        after_cs + _AFTER_CSTRING_CATEGORY)[0]
    by58 = entry_bytes[after_cs + _AFTER_CSTRING_BY58]
    lookup_a_60 = struct.unpack_from(
        "<I", entry_bytes,
        after_cs + _AFTER_CSTRING_LOOKUP_A_60)[0]
    lookup_b_62 = struct.unpack_from(
        "<I", entry_bytes,
        after_cs + _AFTER_CSTRING_LOOKUP_B_62)[0]
    lookup_c_64 = struct.unpack_from(
        "<I", entry_bytes,
        after_cs + _AFTER_CSTRING_LOOKUP_C_64)[0]
    lookup_d_66 = struct.unpack_from(
        "<I", entry_bytes,
        after_cs + _AFTER_CSTRING_LOOKUP_D_66)[0]
    by68 = entry_bytes[after_cs + _AFTER_CSTRING_BY68]
    by69 = entry_bytes[after_cs + _AFTER_CSTRING_BY69]
    lookup_88 = struct.unpack_from(
        "<I", entry_bytes,
        after_cs + _AFTER_CSTRING_LOOKUP_88)[0]
    lookup_90 = struct.unpack_from(
        "<I", entry_bytes,
        after_cs + _AFTER_CSTRING_LOOKUP_90)[0]

    # First u32 array.
    first_array_pos = after_cs + _AFTER_CSTRING_FIRST_ARRAY_LEN
    first_array, first_array_size = _read_u32_array(
        entry_bytes, first_array_pos)
    after_first_array = first_array_pos + first_array_size

    # Five u32 fields between the two arrays.
    needed = after_first_array + _AFTER_FIRST_ARRAY_SECOND_ARRAY_LEN + 4
    if needed > len(entry_bytes):
        raise ValueError(
            "buff item payload truncated between arrays")
    u32_at128 = struct.unpack_from(
        "<I", entry_bytes,
        after_first_array + _AFTER_FIRST_ARRAY_U32_AT128)[0]
    u32_at72 = struct.unpack_from(
        "<I", entry_bytes,
        after_first_array + _AFTER_FIRST_ARRAY_U32_AT72)[0]
    u32_at76 = struct.unpack_from(
        "<I", entry_bytes,
        after_first_array + _AFTER_FIRST_ARRAY_U32_AT76)[0]
    u32_at80 = struct.unpack_from(
        "<I", entry_bytes,
        after_first_array + _AFTER_FIRST_ARRAY_U32_AT80)[0]
    u32_at84 = struct.unpack_from(
        "<I", entry_bytes,
        after_first_array + _AFTER_FIRST_ARRAY_U32_AT84)[0]

    # Second u32 array.
    second_array_pos = (
        after_first_array + _AFTER_FIRST_ARRAY_SECOND_ARRAY_LEN)
    second_array, second_array_size = _read_u32_array(
        entry_bytes, second_array_pos)
    after_second_array = second_array_pos + second_array_size

    # Final fixed tail: by132 (1) + u32_at136 (4).
    if after_second_array + _DBASE_FIXED_TAIL_BYTES > len(entry_bytes):
        raise ValueError(
            "buff item payload truncated in final tail")
    by132 = entry_bytes[
        after_second_array + _AFTER_SECOND_ARRAY_BY132]
    u32_at136 = struct.unpack_from(
        "<I", entry_bytes,
        after_second_array + _AFTER_SECOND_ARRAY_U32_AT136)[0]
    end_offset = after_second_array + _DBASE_FIXED_TAIL_BYTES

    return BuffPayloadCommon(
        tag=tag, tag_offset=position + _DBASE_TAG,
        id=id_val, id_offset=position + _DBASE_ID,
        name_id=name_id, name_id_offset=position + _DBASE_NAME_ID,
        flags_a=flags_a, flags_a_offset=position + _DBASE_FLAGS_A,
        flags_b=flags_b, flags_b_offset=position + _DBASE_FLAGS_B,
        qword_a=qword_a, qword_a_offset=position + _DBASE_QWORD_A,
        qword_b=qword_b, qword_b_offset=position + _DBASE_QWORD_B,
        qword_c=qword_c, qword_c_offset=position + _DBASE_QWORD_C,
        asset_path=asset_path,
        asset_path_offset=cstring_len_pos,
        category=category,
        category_offset=after_cs + _AFTER_CSTRING_CATEGORY,
        by58=by58,
        by58_offset=after_cs + _AFTER_CSTRING_BY58,
        lookup_a_60=lookup_a_60,
        lookup_a_60_offset=after_cs + _AFTER_CSTRING_LOOKUP_A_60,
        lookup_b_62=lookup_b_62,
        lookup_b_62_offset=after_cs + _AFTER_CSTRING_LOOKUP_B_62,
        lookup_c_64=lookup_c_64,
        lookup_c_64_offset=after_cs + _AFTER_CSTRING_LOOKUP_C_64,
        lookup_d_66=lookup_d_66,
        lookup_d_66_offset=after_cs + _AFTER_CSTRING_LOOKUP_D_66,
        by68=by68, by68_offset=after_cs + _AFTER_CSTRING_BY68,
        by69=by69, by69_offset=after_cs + _AFTER_CSTRING_BY69,
        lookup_88=lookup_88,
        lookup_88_offset=after_cs + _AFTER_CSTRING_LOOKUP_88,
        lookup_90=lookup_90,
        lookup_90_offset=after_cs + _AFTER_CSTRING_LOOKUP_90,
        first_array=first_array,
        first_array_offset=first_array_pos,
        u32_at128=u32_at128,
        u32_at128_offset=after_first_array + _AFTER_FIRST_ARRAY_U32_AT128,
        u32_at72=u32_at72,
        u32_at72_offset=after_first_array + _AFTER_FIRST_ARRAY_U32_AT72,
        u32_at76=u32_at76,
        u32_at76_offset=after_first_array + _AFTER_FIRST_ARRAY_U32_AT76,
        u32_at80=u32_at80,
        u32_at80_offset=after_first_array + _AFTER_FIRST_ARRAY_U32_AT80,
        u32_at84=u32_at84,
        u32_at84_offset=after_first_array + _AFTER_FIRST_ARRAY_U32_AT84,
        second_array=second_array,
        second_array_offset=second_array_pos,
        by132=by132,
        by132_offset=after_second_array + _AFTER_SECOND_ARRAY_BY132,
        u32_at136=u32_at136,
        u32_at136_offset=after_second_array + _AFTER_SECOND_ARRAY_U32_AT136,
        end_offset=end_offset,
    )


def serialize_payload_common(payload: BuffPayloadCommon) -> bytes:
    """Re-emit the bytes of a BuffPayloadCommon. Round-trip check
    for the decoder: ``serialize_payload_common(parse_payload_common(
    bytes, 0)) == bytes`` for any well-formed payload."""
    asset_bytes = payload.asset_path.encode("utf-8")
    out = bytearray()
    out += bytes([payload.tag])
    out += struct.pack("<I", payload.id)
    out += struct.pack("<I", payload.name_id)
    out += bytes([payload.flags_a, payload.flags_b])
    out += struct.pack("<Q", payload.qword_a)
    out += struct.pack("<Q", payload.qword_b)
    out += struct.pack("<Q", payload.qword_c)
    out += struct.pack("<I", len(asset_bytes)) + asset_bytes
    out += struct.pack("<I", payload.category)
    out += bytes([payload.by58])
    out += struct.pack("<I", payload.lookup_a_60)
    out += struct.pack("<I", payload.lookup_b_62)
    out += struct.pack("<I", payload.lookup_c_64)
    out += struct.pack("<I", payload.lookup_d_66)
    out += bytes([payload.by68, payload.by69])
    out += struct.pack("<I", payload.lookup_88)
    out += struct.pack("<I", payload.lookup_90)
    out += struct.pack("<I", len(payload.first_array))
    for v in payload.first_array:
        out += struct.pack("<I", v)
    out += struct.pack("<I", payload.u32_at128)
    out += struct.pack("<I", payload.u32_at72)
    out += struct.pack("<I", payload.u32_at76)
    out += struct.pack("<I", payload.u32_at80)
    out += struct.pack("<I", payload.u32_at84)
    out += struct.pack("<I", len(payload.second_array))
    for v in payload.second_array:
        out += struct.pack("<I", v)
    out += bytes([payload.by132])
    out += struct.pack("<I", payload.u32_at136)
    return bytes(out)


# Mapping from the public mod-schema field name (the leaf in
# ``buff_data_list[N].data.base.X``) to the (offset_attr, width,
# dtype) triple on BuffPayloadCommon. Variable-length array fields
# aren't directly addressable by intent paths so they're not listed.
_PAYLOAD_COMMON_FIELDS: dict[str, tuple[str, int, str]] = {
    "tag": ("tag_offset", 1, "u8"),
    "id": ("id_offset", 4, "u32"),
    "name_id": ("name_id_offset", 4, "u32"),
    "flags_a": ("flags_a_offset", 1, "u8"),
    "flags_b": ("flags_b_offset", 1, "u8"),
    "qword_a": ("qword_a_offset", 8, "u64"),
    "qword_b": ("qword_b_offset", 8, "u64"),
    "qword_c": ("qword_c_offset", 8, "u64"),
    "asset_path": ("asset_path_offset", 0, "cstring"),
    "category": ("category_offset", 4, "u32"),
    "by58": ("by58_offset", 1, "u8"),
    "lookup_a_60": ("lookup_a_60_offset", 4, "u32"),
    "lookup_b_62": ("lookup_b_62_offset", 4, "u32"),
    "lookup_c_64": ("lookup_c_64_offset", 4, "u32"),
    "lookup_d_66": ("lookup_d_66_offset", 4, "u32"),
    "by68": ("by68_offset", 1, "u8"),
    "by69": ("by69_offset", 1, "u8"),
    "lookup_88": ("lookup_88_offset", 4, "u32"),
    "lookup_90": ("lookup_90_offset", 4, "u32"),
    "u32_at128": ("u32_at128_offset", 4, "u32"),
    "u32_at72": ("u32_at72_offset", 4, "u32"),
    "u32_at76": ("u32_at76_offset", 4, "u32"),
    "u32_at80": ("u32_at80_offset", 4, "u32"),
    "u32_at84": ("u32_at84_offset", 4, "u32"),
    "by132": ("by132_offset", 1, "u8"),
    "u32_at136": ("u32_at136_offset", 4, "u32"),
}


def parse_item_header(
    entry_bytes: bytes, position: int,
) -> BuffItemHeader:
    """Decode the 5-byte header that introduces each item in the
    ``buff_data_list`` region.

    ``position`` is the byte offset within ``entry_bytes`` where the
    item begins. Raises ``ValueError`` if there aren't 5 bytes
    available at that position.
    """
    if position < 0 or position + _ITEM_HEADER_BYTES > len(entry_bytes):
        raise ValueError(
            f"buff item header out of range: position {position}, "
            f"entry size {len(entry_bytes)}"
        )
    prefix_id = struct.unpack_from("<I", entry_bytes, position)[0]
    absent_flag = entry_bytes[position + 4]
    return BuffItemHeader(
        prefix_id=prefix_id,
        prefix_id_offset=position,
        absent_flag=absent_flag,
        absent_flag_offset=position + 4,
        payload_offset=position + _ITEM_HEADER_BYTES,
    )


@dataclass(frozen=True)
class BuffinfoEntryHeader:
    """Decoded prefix of a single buffinfo.pabgb entry.

    Verified field-by-field across all 280 entries of CD v1.05
    vanilla buffinfo.pabgb. ``body_start`` is the byte offset where
    the variable-length buff_data_list items begin.
    """
    entry_key: int
    name: str
    is_blocked: int
    is_blocked_offset: int
    buff_data_count: int
    buff_data_count_offset: int
    body_start: int
    prefix_end: int


@dataclass(frozen=True)
class BuffinfoEntry:
    """Full decoded BuffInfo wrapper for one buffinfo.pabgb entry.

    All ``_offset`` fields are byte positions within the entry that
    CDUMM's intent expander can target for byte patches. Fields named
    after the engine schema; values are raw on-disk integers.

    ``buff_data_list_bytes`` is the un-decoded variable-length region
    holding ``buff_data_count`` BuffData items. Future passes will
    decode item internals and expose per-item offsets; until then
    the bytes are preserved verbatim so the entry round-trips.
    """
    # Header (already exposed via parse_entry_prefix)
    entry_key: int
    name: str

    # Wrapper fields with offsets
    is_blocked: int
    is_blocked_offset: int

    buff_data_count: int
    buff_data_count_offset: int

    buff_data_list_bytes: bytes
    buff_data_list_offset: int  # start of items region

    min_level: int
    min_level_offset: int

    max_level: int
    max_level_offset: int

    sequencer_file_name: str
    sequencer_file_name_offset: int  # byte offset of the length u32

    buff_level_calculate_type: int
    buff_level_calculate_type_offset: int

    ui_template_name: int
    ui_template_name_offset: int

    ui_component_name: int
    ui_component_name_offset: int

    elemental_status_info: int
    elemental_status_info_offset: int

    is_use_skill_info_pattern_description: int
    is_use_skill_info_pattern_description_offset: int

    use_counting_by_global_timer: int
    use_counting_by_global_timer_offset: int


def parse_entry_prefix(entry_bytes: bytes) -> BuffinfoEntryHeader:
    """Decode just the header (key + name + is_blocked + count).

    Cheaper than parse_entry when only the header fields are needed
    (e.g. to look up an entry by key without decoding the full
    wrapper). Raises ``ValueError`` on truncation or implausible
    counts.
    """
    if len(entry_bytes) < 8:
        raise ValueError(
            f"buffinfo entry too short for prefix: {len(entry_bytes)}B"
        )
    entry_key = struct.unpack_from("<I", entry_bytes, 0)[0]
    slen = struct.unpack_from("<I", entry_bytes, 4)[0]
    if slen > 1_000_000 or 8 + slen > len(entry_bytes):
        raise ValueError(
            f"buffinfo entry has implausible name length {slen} "
            f"(entry size {len(entry_bytes)}B)"
        )
    name = entry_bytes[8:8 + slen].decode("utf-8", errors="replace")
    prefix_end = 8 + slen

    if prefix_end + 5 > len(entry_bytes):
        raise ValueError(
            f"buffinfo entry truncated at body header: need 5 bytes "
            f"after name, got {len(entry_bytes) - prefix_end}"
        )
    is_blocked = entry_bytes[prefix_end]
    buff_data_count = struct.unpack_from(
        "<I", entry_bytes, prefix_end + 1)[0]
    if buff_data_count > 10_000:
        raise ValueError(
            f"buffinfo entry has implausible buff_data_list count "
            f"{buff_data_count} for entry {name!r}"
        )

    return BuffinfoEntryHeader(
        entry_key=entry_key,
        name=name,
        is_blocked=is_blocked,
        is_blocked_offset=prefix_end,
        buff_data_count=buff_data_count,
        buff_data_count_offset=prefix_end + 1,
        body_start=prefix_end + 5,
        prefix_end=prefix_end,
    )


def _find_sequencer_length(
    entry_bytes: bytes, body_start: int,
) -> int:
    """Locate the sequencer_file_name length by walking backward from
    the entry's known end.

    The trailing 15 bytes are fixed-width fields, so the sequencer's
    UTF-8 bytes end at ``len - 15``. The length u32 sits 4 bytes
    before the UTF-8 starts, so for a candidate length ``L``:

        cstring_len_pos = len - 15 - L - 4

    A consistent layout requires ``u32_at(cstring_len_pos) == L``.
    Iterate small L upward until the relation holds (sequencer names
    are short , typically 0-100 bytes in observed vanilla data).
    """
    n = len(entry_bytes)
    floor_pos = body_start + _POST_ITEMS_PRE_CSTRING_BYTES
    # Maximum candidate L is bounded by the entry size minus the
    # mandatory pre-cstring (12) + trailing (15) overhead.
    max_l = n - floor_pos - _TRAILING_FIXED_BYTES
    if max_l < 0:
        raise ValueError(
            f"buffinfo entry too small to contain wrapper trailer "
            f"(size {n}, body_start {body_start})"
        )
    for candidate_len in range(max_l + 1):
        cstring_len_pos = n - _TRAILING_FIXED_BYTES - candidate_len - 4
        if cstring_len_pos < floor_pos:
            break
        actual = struct.unpack_from("<I", entry_bytes, cstring_len_pos)[0]
        if actual == candidate_len:
            return candidate_len
    raise ValueError(
        "buffinfo entry: could not locate a self-consistent "
        "sequencer_file_name length via backward walk"
    )


def parse_entry(entry_bytes: bytes) -> BuffinfoEntry:
    """Decode the full BuffInfo wrapper for one entry.

    Items inside ``buff_data_list`` are NOT decoded yet; their bytes
    are preserved as ``buff_data_list_bytes`` so the entry round-trips
    via ``serialize_entry``. Wrapper fields all expose ``_offset``
    annotations callers can use to emit byte patches.
    """
    head = parse_entry_prefix(entry_bytes)

    # Locate sequencer_file_name by back-walking from the entry tail.
    seq_len = _find_sequencer_length(entry_bytes, head.body_start)
    n = len(entry_bytes)
    seq_len_pos = n - _TRAILING_FIXED_BYTES - seq_len - 4
    seq_data_pos = seq_len_pos + 4
    sequencer_name = entry_bytes[
        seq_data_pos:seq_data_pos + seq_len].decode(
            "utf-8", errors="replace")

    # min_level + max_level immediately precede the cstring length.
    max_level_pos = seq_len_pos - 4
    min_level_pos = max_level_pos - 4
    min_level = struct.unpack_from("<I", entry_bytes, min_level_pos)[0]
    max_level = struct.unpack_from("<I", entry_bytes, max_level_pos)[0]

    # buff_data_list items occupy [body_start..min_level_pos].
    items_bytes = bytes(entry_bytes[head.body_start:min_level_pos])
    items_offset = head.body_start

    # Trailing fixed-width region. Order from spec is buff_level_-
    # calculate_type, ui_template_name, ui_component_name, elemental_-
    # status_info, is_use_skill_info_pattern_description, use_-
    # counting_by_global_timer.
    blct_pos = seq_data_pos + seq_len
    uit_pos = blct_pos + 1
    uic_pos = uit_pos + 4
    esi_pos = uic_pos + 4
    iuspd_pos = esi_pos + 4
    ucbgt_pos = iuspd_pos + 1

    blct = entry_bytes[blct_pos]
    uit = struct.unpack_from("<I", entry_bytes, uit_pos)[0]
    uic = struct.unpack_from("<I", entry_bytes, uic_pos)[0]
    esi = struct.unpack_from("<I", entry_bytes, esi_pos)[0]
    iuspd = entry_bytes[iuspd_pos]
    ucbgt = entry_bytes[ucbgt_pos]

    return BuffinfoEntry(
        entry_key=head.entry_key,
        name=head.name,
        is_blocked=head.is_blocked,
        is_blocked_offset=head.is_blocked_offset,
        buff_data_count=head.buff_data_count,
        buff_data_count_offset=head.buff_data_count_offset,
        buff_data_list_bytes=items_bytes,
        buff_data_list_offset=items_offset,
        min_level=min_level,
        min_level_offset=min_level_pos,
        max_level=max_level,
        max_level_offset=max_level_pos,
        sequencer_file_name=sequencer_name,
        sequencer_file_name_offset=seq_len_pos,
        buff_level_calculate_type=blct,
        buff_level_calculate_type_offset=blct_pos,
        ui_template_name=uit,
        ui_template_name_offset=uit_pos,
        ui_component_name=uic,
        ui_component_name_offset=uic_pos,
        elemental_status_info=esi,
        elemental_status_info_offset=esi_pos,
        is_use_skill_info_pattern_description=iuspd,
        is_use_skill_info_pattern_description_offset=iuspd_pos,
        use_counting_by_global_timer=ucbgt,
        use_counting_by_global_timer_offset=ucbgt_pos,
    )


def serialize_entry(entry: BuffinfoEntry) -> bytes:
    """Re-emit an entry's bytes from a decoded BuffinfoEntry.

    Used for round-trip verification and (eventually) write-back of
    intent-applied edits. Re-uses ``buff_data_list_bytes`` verbatim
    until the items decoder lands.
    """
    name_bytes = entry.name.encode("utf-8")
    seq_bytes = entry.sequencer_file_name.encode("utf-8")
    out = bytearray()
    out += struct.pack("<I", entry.entry_key)
    out += struct.pack("<I", len(name_bytes))
    out += name_bytes
    out += bytes([entry.is_blocked])
    out += struct.pack("<I", entry.buff_data_count)
    out += entry.buff_data_list_bytes
    out += struct.pack("<I", entry.min_level)
    out += struct.pack("<I", entry.max_level)
    out += struct.pack("<I", len(seq_bytes))
    out += seq_bytes
    out += bytes([entry.buff_level_calculate_type])
    out += struct.pack("<I", entry.ui_template_name)
    out += struct.pack("<I", entry.ui_component_name)
    out += struct.pack("<I", entry.elemental_status_info)
    out += bytes([entry.is_use_skill_info_pattern_description])
    out += bytes([entry.use_counting_by_global_timer])
    return bytes(out)


# Mapping of intent-path field names to a (offset_attr, width, dtype)
# triple. Width is the byte width on disk; dtype is a tag used by the
# intent expander to format the patched bytes correctly.
_WRAPPER_FIELDS: dict[str, tuple[str, int, str]] = {
    "is_blocked": ("is_blocked_offset", 1, "u8"),
    "buff_data_count": ("buff_data_count_offset", 4, "u32"),
    "min_level": ("min_level_offset", 4, "u32"),
    "max_level": ("max_level_offset", 4, "u32"),
    "buff_level_calculate_type":
        ("buff_level_calculate_type_offset", 1, "u8"),
    "ui_template_name": ("ui_template_name_offset", 4, "u32"),
    "ui_component_name": ("ui_component_name_offset", 4, "u32"),
    "elemental_status_info":
        ("elemental_status_info_offset", 4, "u32"),
    "is_use_skill_info_pattern_description":
        ("is_use_skill_info_pattern_description_offset", 1, "u8"),
    "use_counting_by_global_timer":
        ("use_counting_by_global_timer_offset", 1, "u8"),
}


def locate_buff_field(
    entry_bytes: bytes, field_path: str,
) -> tuple[int, int, str] | None:
    """Resolve a field path to ``(byte_offset, width, dtype)`` within
    an entry, or ``None`` if the path can't be resolved yet.

    Currently supported:

    * Wrapper fields (``min_level``, ``max_level``,
      ``ui_template_name``, ``elemental_status_info``, etc.) , see
      ``_WRAPPER_FIELDS`` for the full list.
    * ``buff_data_list[0].absent_flag`` , the absent indicator on
      the first item. Items at indices > 0 still return ``None``
      because walking past a present item's variable-length payload
      requires the variant size table (not yet built).

    Future expansion will add:

    * ``buff_data_list[N].absent_flag`` for any N (needs variant
      size table)
    * ``buff_data_list[N].data.base.{tag, id, name_id, flags_a,
      flags_b, asset_path, category, ...}`` (needs the payload
      common-prefix decoder)
    """
    # Wrapper-level path: no brackets, no dots.
    if "[" not in field_path and "." not in field_path:
        spec = _WRAPPER_FIELDS.get(field_path)
        if spec is None:
            return None
        offset_attr, width, dtype = spec
        entry = parse_entry(entry_bytes)
        return getattr(entry, offset_attr), width, dtype

    # Item-level paths of the shape ``buff_data_list[N].leaf``.
    if field_path.startswith("buff_data_list["):
        try:
            close_bracket = field_path.index("]")
            n = int(field_path[len("buff_data_list["):close_bracket])
            tail = field_path[close_bracket + 1:]
        except (ValueError, IndexError):
            return None
        if n < 0:
            # Negative indices like [-1] are not part of the mod schema.
            # Without this guard ``range(n)`` is empty and the walker
            # silently lands on item 0, masking the malformed path.
            return None
        entry = parse_entry(entry_bytes)
        if n >= entry.buff_data_count:
            return None  # past the end of the list
        # Walk forward through items 0..n-1 to find item n's start.
        position = entry.buff_data_list_offset
        for _ in range(n):
            try:
                hdr = parse_item_header(entry_bytes, position)
            except ValueError:
                return None
            if hdr.absent_flag != 0:
                # Absent items have no payload, just the 5-byte header.
                position += _ITEM_HEADER_BYTES
                continue
            try:
                common = parse_payload_common(
                    entry_bytes, hdr.payload_offset)
            except ValueError:
                return None
            tag_size = _VARIANT_TAIL_SIZES.get(common.tag)
            if tag_size is None:
                # Unknown variant , can't safely walk past it.
                return None
            position = common.end_offset + tag_size
        header = parse_item_header(entry_bytes, position)
        if tail == ".absent_flag":
            return header.absent_flag_offset, 1, "u8"
        if tail == ".leading_lookup":
            return header.prefix_id_offset, 4, "u32"
        if tail.startswith(".data.base."):
            if header.absent_flag != 0:
                return None  # absent items have no payload to address
            leaf = tail[len(".data.base."):]
            spec = _PAYLOAD_COMMON_FIELDS.get(leaf)
            if spec is None:
                return None
            offset_attr, width, dtype = spec
            payload = parse_payload_common(
                entry_bytes, header.payload_offset)
            return getattr(payload, offset_attr), width, dtype
        return None

    return None
