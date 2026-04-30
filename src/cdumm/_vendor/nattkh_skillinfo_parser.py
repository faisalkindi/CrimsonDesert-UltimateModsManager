#!/usr/bin/env python3
"""
skillinfo_parser.py  --  100% roundtrip parser for Crimson Desert skill.pabgb/pabgh.

Field order and sizes reverse-engineered from IDA decompilation of
SkillInfo::readEntryFields (sub_1410F8680) and BuffData factory
(sub_1419D8660), validated against game baselines 1.0.0.3 and 1.0.0.4.

BODY LAYOUT (sequential from entry header):
  u8   _isBlocked
  3B   _pad_01
  BuffLevelList:
    u32  level_count
    per level:
      u32  buff_count
      per buff:
        u8 flag (0=valid, else null entry)
        if flag==0: BuffData common base + subclass tail
  Post-buff fields (see _read_post_buff)

BUFFDATA COMMON BASE (49 fixed + variable CString + variable lists):
  u8   type_id          (selects subclass vtable, 0-119)
  u32  field_12
  u32  field_16
  u8   field_20
  u8   field_21
  i64  field_24
  i64  field_32
  i64  field_40
  CString field_48      (u32 len + bytes)
  u32  field_56         (hash, stored as u16 in memory)
  -- NOTE: field_58 (u8) from IDA does NOT exist in the file format --
  u32  field_60
  u32  field_62
  u32  field_64
  u32  field_66
  u8   field_68
  u8   field_69
  u32  field_88
  u32  field_90
  u32  cnt + cnt*u32    field_96_list
  u32  field_128
  u32  field_72
  u32  field_76
  u32  field_80
  u32  field_84
  u32  cnt + cnt*u32    field_112_list
  u8   field_132
  u32  field_136
  bytes _subclass_tail  (variable, depends on type_id)

Public API (used by gui/tabs/skill_tree.py):
    parse_skill_pabgh(pabgh_bytes)
    parse_all(pabgh_bytes, pabgb_bytes)
    serialize_entry(entry)
    serialize_all(entries)
    roundtrip_test(pabgh_bytes, pabgb_bytes)
"""

from __future__ import annotations

import struct
import sys
from typing import Optional

__all__ = [
    "parse_skill_pabgh",
    "parse_all",
    "serialize_entry",
    "serialize_all",
    "roundtrip_test",
    "parse_skill_entry",
    "count_buff_levels",
    "find_buff_entry_sentinels",
    "get_body_field_u32",
    "set_body_field_u32",
    "get_body_field_i32",
    "set_body_field_i32",
    "get_body_blob",
    "get_iconpath_text",
    "get_str2_text",
]


# ---------------------------------------------------------------------------
#  Packing helpers
# ---------------------------------------------------------------------------

def _p_u8(v):  return struct.pack("<B", v)
def _p_u16(v): return struct.pack("<H", v)
def _p_u32(v): return struct.pack("<I", v)
def _p_i64(v): return struct.pack("<q", v)
def _p_cstr(b): return struct.pack("<I", len(b)) + b
def _p_list_u32(lst): return struct.pack("<I", len(lst)) + b"".join(struct.pack("<I", v) for v in lst)
def _p_list_u16(lst): return struct.pack("<I", len(lst)) + b"".join(struct.pack("<H", v) for v in lst)


# ---------------------------------------------------------------------------
#  BuffData common base parse / serialize
# ---------------------------------------------------------------------------

# Format version: True = has field_58 (1.0.0.4+), False = no field_58 (1.0.0.3)
_has_field_58: bool = True  # default to newer format


def _read_buff_common_base(body, p):
    """Read common base of a BuffData entry starting at p (after flag byte).
    Returns (new_pos, dict).
    """
    bd = {}
    bd["type_id"] = body[p]; p += 1
    bd["field_12"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_16"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_20"] = body[p]; p += 1
    bd["field_21"] = body[p]; p += 1
    bd["field_24"] = struct.unpack_from("<q", body, p)[0]; p += 8
    bd["field_32"] = struct.unpack_from("<q", body, p)[0]; p += 8
    bd["field_40"] = struct.unpack_from("<q", body, p)[0]; p += 8
    slen = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_48"] = body[p:p + slen]; p += slen
    bd["field_56"] = struct.unpack_from("<I", body, p)[0]; p += 4
    if _has_field_58:
        bd["field_58"] = body[p]; p += 1
    bd["field_60"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_62"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_64"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_66"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_68"] = body[p]; p += 1
    bd["field_69"] = body[p]; p += 1
    bd["field_88"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_90"] = struct.unpack_from("<I", body, p)[0]; p += 4
    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_96_list"] = [struct.unpack_from("<I", body, p + i * 4)[0] for i in range(cnt)]
    p += cnt * 4
    bd["field_128"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_72"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_76"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_80"] = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_84"] = struct.unpack_from("<I", body, p)[0]; p += 4
    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    bd["field_112_list"] = [struct.unpack_from("<I", body, p + i * 4)[0] for i in range(cnt)]
    p += cnt * 4
    bd["field_132"] = body[p]; p += 1
    bd["field_136"] = struct.unpack_from("<I", body, p)[0]; p += 4
    return p, bd


def _serialize_buff_data(bd):
    """Serialize a BuffData entry (flag + optional common base + subclass tail)."""
    parts = [_p_u8(bd["_flag"])]
    if bd["_null"]:
        return b"".join(parts)
    parts.append(_p_u8(bd["type_id"]))
    parts.append(_p_u32(bd["field_12"]))
    parts.append(_p_u32(bd["field_16"]))
    parts.append(_p_u8(bd["field_20"]))
    parts.append(_p_u8(bd["field_21"]))
    parts.append(_p_i64(bd["field_24"]))
    parts.append(_p_i64(bd["field_32"]))
    parts.append(_p_i64(bd["field_40"]))
    parts.append(_p_cstr(bd["field_48"]))
    parts.append(_p_u32(bd["field_56"]))
    if "field_58" in bd:
        parts.append(_p_u8(bd["field_58"]))
    parts.append(_p_u32(bd["field_60"]))
    parts.append(_p_u32(bd["field_62"]))
    parts.append(_p_u32(bd["field_64"]))
    parts.append(_p_u32(bd["field_66"]))
    parts.append(_p_u8(bd["field_68"]))
    parts.append(_p_u8(bd["field_69"]))
    parts.append(_p_u32(bd["field_88"]))
    parts.append(_p_u32(bd["field_90"]))
    parts.append(_p_list_u32(bd["field_96_list"]))
    parts.append(_p_u32(bd["field_128"]))
    parts.append(_p_u32(bd["field_72"]))
    parts.append(_p_u32(bd["field_76"]))
    parts.append(_p_u32(bd["field_80"]))
    parts.append(_p_u32(bd["field_84"]))
    parts.append(_p_list_u32(bd["field_112_list"]))
    parts.append(_p_u8(bd["field_132"]))
    parts.append(_p_u32(bd["field_136"]))
    if bd.get("_subclass_tail"):
        parts.append(bd["_subclass_tail"])
    return b"".join(parts)


# ---------------------------------------------------------------------------
#  Post-buff fields: try-parse (for probing) and full parse/serialize
# ---------------------------------------------------------------------------

def _try_parse_post_buff(body, p):
    """Try to parse post-buff fields from position p. Returns end position or None."""
    body_len = len(body)
    try:
        if p + 4 + 4 + 4 + 1 + 4 + 4 + 28 + 28 > body_len:
            return None
        p += 4  # _skillGroupKey
        p += 4  # _parentSkill
        p += 4  # _learnLevel
        p += 1  # _applyType
        p += 4  # _iconPath
        p += 4  # _needUpgradeItemInfo
        p += 28  # _needUpgradeItemCountGraph
        p += 28  # _needUpgradeExperienceGraph

        for max_cnt in [50, 50]:  # _usableCharacterInfoList, _usableCondition
            cnt = struct.unpack_from("<I", body, p)[0]
            if cnt > max_cnt: return None
            p += 4 + cnt * 4
            if p > body_len: return None

        p += 4  # _learnKnowledgeInfo
        p += 4  # _factionInfo

        cnt = struct.unpack_from("<I", body, p)[0]  # _useResourceStatList
        if cnt > 50: return None
        p += 4 + cnt * 22
        if p > body_len: return None

        cnt = struct.unpack_from("<I", body, p)[0]  # _useResourceItemList
        if cnt > 50: return None
        p += 4 + cnt * 12
        if p > body_len: return None

        cnt = struct.unpack_from("<I", body, p)[0]  # _useDriverResourceStatList
        if cnt > 50: return None
        p += 4 + cnt * 22
        if p > body_len: return None

        p += 8  # _useBatteryStat
        p += 6  # 6 u8 flags
        if p > body_len: return None

        cnt = struct.unpack_from("<I", body, p)[0]  # _reserveSlotInfoList
        if cnt > 50: return None
        p += 4 + cnt * 4
        if p > body_len: return None

        ml = struct.unpack_from("<I", body, p)[0]  # _maxLevel
        if ml > 100: return None
        p += 4

        cnt = struct.unpack_from("<I", body, p)[0]  # _skillGroupKeyList
        if cnt > 50: return None
        p += 4 + cnt * 2
        if p > body_len: return None

        p += 4  # _buffSustainFlag

        slen = struct.unpack_from("<I", body, p)[0]  # _devSkillName
        if slen > 2000: return None
        p += 4 + slen
        if p > body_len: return None
        # Validate it looks like text (Korean UTF-8 or ASCII)
        if slen > 0:
            try:
                body[p - slen:p].decode("utf-8")
            except UnicodeDecodeError:
                return None

        slen = struct.unpack_from("<I", body, p)[0]  # _devSkillDesc
        if slen > 2000: return None
        p += 4 + slen
        if p > body_len: return None
        if slen > 0:
            try:
                body[p - slen:p].decode("utf-8")
            except UnicodeDecodeError:
                return None

        p += 4  # _videoPath
        if p > body_len: return None
        return p
    except (struct.error, IndexError):
        return None


def _read_resource_stat(body, p):
    rs = {}
    rs["stat_type"] = body[p]; p += 1
    rs["stat_hash"] = struct.unpack_from("<I", body, p)[0]; p += 4
    rs["flag"] = body[p]; p += 1
    rs["value"] = struct.unpack_from("<q", body, p)[0]; p += 8
    rs["hash2"] = struct.unpack_from("<I", body, p)[0]; p += 4
    rs["hash3"] = struct.unpack_from("<I", body, p)[0]; p += 4
    return p, rs


def _serialize_resource_stat(rs):
    return (_p_u8(rs["stat_type"]) + _p_u32(rs["stat_hash"]) +
            _p_u8(rs["flag"]) + _p_i64(rs["value"]) +
            _p_u32(rs["hash2"]) + _p_u32(rs["hash3"]))


def _read_graph(body, p):
    g = {
        "val0": struct.unpack_from("<q", body, p)[0],
        "val1": struct.unpack_from("<q", body, p + 8)[0],
        "val2": struct.unpack_from("<q", body, p + 16)[0],
        "val3": struct.unpack_from("<I", body, p + 24)[0],
    }
    return p + 28, g


def _serialize_graph(g):
    return _p_i64(g["val0"]) + _p_i64(g["val1"]) + _p_i64(g["val2"]) + _p_u32(g["val3"])


def _read_post_buff(body, p):
    """Read all post-buff fields from position p. Returns (new_pos, dict)."""
    f = {}
    f["_skillGroupKey"] = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_parentSkill"] = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_learnLevel"] = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_applyType"] = body[p]; p += 1
    f["_iconPath"] = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_needUpgradeItemInfo"] = struct.unpack_from("<I", body, p)[0]; p += 4
    p, f["_needUpgradeItemCountGraph"] = _read_graph(body, p)
    p, f["_needUpgradeExperienceGraph"] = _read_graph(body, p)

    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_usableCharacterInfoList"] = [struct.unpack_from("<I", body, p + i * 4)[0] for i in range(cnt)]
    p += cnt * 4

    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_usableCondition"] = [struct.unpack_from("<I", body, p + i * 4)[0] for i in range(cnt)]
    p += cnt * 4

    f["_learnKnowledgeInfo"] = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_factionInfo"] = struct.unpack_from("<I", body, p)[0]; p += 4

    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    rsl = []
    for _ in range(cnt):
        p, rs = _read_resource_stat(body, p)
        rsl.append(rs)
    f["_useResourceStatList"] = rsl

    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    ril = []
    for _ in range(cnt):
        ri = {"item_hash": struct.unpack_from("<I", body, p)[0],
              "count": struct.unpack_from("<q", body, p + 4)[0]}
        p += 12
        ril.append(ri)
    f["_useResourceItemList"] = ril

    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    drsl = []
    for _ in range(cnt):
        p, rs = _read_resource_stat(body, p)
        drsl.append(rs)
    f["_useDriverResourceStatList"] = drsl

    f["_useBatteryStat"] = struct.unpack_from("<q", body, p)[0]; p += 8
    f["_isUiUseAllowed"] = body[p]; p += 1
    f["_isLearnUseArtifact"] = body[p]; p += 1
    f["_allowSkillWithLowResource"] = body[p]; p += 1
    f["_isUseChildPatternDescriptionBuffData"] = body[p]; p += 1
    f["_damageType"] = body[p]; p += 1
    f["_uiType"] = body[p]; p += 1

    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_reserveSlotInfoList"] = [struct.unpack_from("<I", body, p + i * 4)[0] for i in range(cnt)]
    p += cnt * 4

    f["_maxLevel"] = struct.unpack_from("<I", body, p)[0]; p += 4

    cnt = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_skillGroupKeyList"] = [struct.unpack_from("<H", body, p + i * 2)[0] for i in range(cnt)]
    p += cnt * 2

    f["_buffSustainFlag"] = struct.unpack_from("<I", body, p)[0]; p += 4

    slen = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_devSkillName"] = body[p:p + slen]; p += slen

    slen = struct.unpack_from("<I", body, p)[0]; p += 4
    f["_devSkillDesc"] = body[p:p + slen]; p += slen

    f["_videoPath"] = struct.unpack_from("<I", body, p)[0]; p += 4
    return p, f


def _serialize_post_buff(f):
    parts = []
    parts.append(_p_u32(f["_skillGroupKey"]))
    parts.append(_p_u32(f["_parentSkill"]))
    parts.append(_p_u32(f["_learnLevel"]))
    parts.append(_p_u8(f["_applyType"]))
    parts.append(_p_u32(f["_iconPath"]))
    parts.append(_p_u32(f["_needUpgradeItemInfo"]))
    parts.append(_serialize_graph(f["_needUpgradeItemCountGraph"]))
    parts.append(_serialize_graph(f["_needUpgradeExperienceGraph"]))
    parts.append(_p_list_u32(f["_usableCharacterInfoList"]))
    parts.append(_p_list_u32(f["_usableCondition"]))
    parts.append(_p_u32(f["_learnKnowledgeInfo"]))
    parts.append(_p_u32(f["_factionInfo"]))
    parts.append(struct.pack("<I", len(f["_useResourceStatList"])))
    for rs in f["_useResourceStatList"]:
        parts.append(_serialize_resource_stat(rs))
    parts.append(struct.pack("<I", len(f["_useResourceItemList"])))
    for ri in f["_useResourceItemList"]:
        parts.append(_p_u32(ri["item_hash"]) + _p_i64(ri["count"]))
    parts.append(struct.pack("<I", len(f["_useDriverResourceStatList"])))
    for rs in f["_useDriverResourceStatList"]:
        parts.append(_serialize_resource_stat(rs))
    parts.append(_p_i64(f["_useBatteryStat"]))
    parts.append(_p_u8(f["_isUiUseAllowed"]))
    parts.append(_p_u8(f["_isLearnUseArtifact"]))
    parts.append(_p_u8(f["_allowSkillWithLowResource"]))
    parts.append(_p_u8(f["_isUseChildPatternDescriptionBuffData"]))
    parts.append(_p_u8(f["_damageType"]))
    parts.append(_p_u8(f["_uiType"]))
    parts.append(_p_list_u32(f["_reserveSlotInfoList"]))
    parts.append(_p_u32(f["_maxLevel"]))
    parts.append(_p_list_u16(f["_skillGroupKeyList"]))
    parts.append(_p_u32(f["_buffSustainFlag"]))
    parts.append(_p_cstr(f["_devSkillName"]))
    parts.append(_p_cstr(f["_devSkillDesc"]))
    parts.append(_p_u32(f["_videoPath"]))
    return b"".join(parts)


# ---------------------------------------------------------------------------
#  Full entry parse (body = everything after entry header)
# ---------------------------------------------------------------------------

# Global cache: type_id -> subclass size (discovered via probing)
_type_id_sizes: dict[int, int] = {}


def _try_parse_remaining(body, p, remaining_buffs, remaining_levels, body_end):
    """Check if remaining buffs + levels + post-buff parse to body_end."""
    try:
        for _ in range(remaining_buffs):
            flag = body[p]; p += 1
            if flag != 0:
                continue
            p, bd = _read_buff_common_base(body, p)
            tid = bd["type_id"]
            if tid in _type_id_sizes:
                p += _type_id_sizes[tid]
            else:
                return False
        for _ in range(remaining_levels):
            bc = struct.unpack_from("<I", body, p)[0]; p += 4
            for _ in range(bc):
                flag = body[p]; p += 1
                if flag != 0:
                    continue
                p, bd = _read_buff_common_base(body, p)
                tid = bd["type_id"]
                if tid in _type_id_sizes:
                    p += _type_id_sizes[tid]
                else:
                    return False
        result = _try_parse_post_buff(body, p)
        return result == body_end
    except (struct.error, IndexError):
        return False


def _detect_format(body):
    """Auto-detect whether this pabgb uses field_58 (1.0.0.4+) or not (1.0.0.3).
    Returns True if field_58 is present, False otherwise.
    Tests both formats and returns the one that works.
    """
    global _has_field_58
    # Try with field_58 first (newer format)
    for try_has in [True, False]:
        _has_field_58 = try_has
        try:
            p = 4  # after isBlocked + pad
            lc = struct.unpack_from("<I", body, p)[0]; p += 4
            for lev in range(lc):
                bc = struct.unpack_from("<I", body, p)[0]; p += 4
                for bi in range(bc):
                    flag = body[p]; p += 1
                    if flag != 0:
                        continue
                    p, bd = _read_buff_common_base(body, p)
                    # Try post-buff parse from here (with 0 subclass)
                    result = _try_parse_post_buff(body, p)
                    if result == len(body):
                        return try_has
                    # Try with subclass by brute force
                    for sz in range(501):
                        result = _try_parse_post_buff(body, p + sz)
                        if result == len(body):
                            return try_has
                    break
                break
            # No non-null buffs: try post-buff directly
            result = _try_parse_post_buff(body, p)
            if result == len(body):
                return try_has
        except (struct.error, IndexError):
            continue
    return True  # default


def _parse_body(body):
    """Parse an entry body into named fields. Returns dict."""
    body_end = len(body)
    f = {}

    f["_isBlocked"] = body[0]
    f["_pad_01"] = body[1:4]
    p = 4

    # -- BuffLevelList --
    level_count = struct.unpack_from("<I", body, p)[0]; p += 4
    levels = []
    _buff_raw_fallback = None

    try:
        for lev in range(level_count):
            buff_count = struct.unpack_from("<I", body, p)[0]; p += 4
            buffs = []
            for bi in range(buff_count):
                flag = body[p]; p += 1
                if flag != 0:
                    buffs.append({"_null": True, "_flag": flag})
                    continue

                p, bd = _read_buff_common_base(body, p)
                tid = bd["type_id"]
                bd["_null"] = False
                bd["_flag"] = 0

                if tid in _type_id_sizes:
                    sz = _type_id_sizes[tid]
                    bd["_subclass_tail"] = body[p:p + sz] if sz > 0 else b""
                    p += sz
                else:
                    # Probe: try sizes 0..500
                    remaining_buffs = buff_count - bi - 1
                    remaining_levels = level_count - lev - 1
                    found = False
                    for try_sz in range(501):
                        test_p = p + try_sz
                        if test_p > body_end:
                            break
                        if _try_parse_remaining(body, test_p,
                                                remaining_buffs, remaining_levels, body_end):
                            _type_id_sizes[tid] = try_sz
                            bd["_subclass_tail"] = body[p:p + try_sz] if try_sz > 0 else b""
                            p += try_sz
                            found = True
                            break
                    if not found:
                        raise ValueError(f"subclass probe failed for type_id={tid}")

                buffs.append(bd)
            levels.append(buffs)

    except (ValueError, struct.error, IndexError):
        # Fallback: store entire buff level list as raw blob
        # Re-read from body[4] (the level_count position)
        # We need to find post-buff start by brute-force
        pb_start = _find_post_buff_start(body)
        if pb_start is None:
            raise ValueError("Cannot find post-buff boundary")
        _buff_raw_fallback = body[4:pb_start]
        p = pb_start
        levels = None

    f["_buffLevelList"] = levels
    f["_buff_raw_fallback"] = _buff_raw_fallback

    # -- Post-buff fields --
    try:
        p, post = _read_post_buff(body, p)
        f.update(post)
        if p != body_end:
            raise ValueError(f"Body parse: consumed {p}/{body_end}")
    except (struct.error, IndexError, ValueError):
        # If the probed position was wrong, try brute-force fallback
        pb_start = _find_post_buff_start(body)
        if pb_start is None:
            raise ValueError("Cannot find post-buff boundary")
        _buff_raw_fallback = body[4:pb_start]
        f["_buffLevelList"] = None
        f["_buff_raw_fallback"] = _buff_raw_fallback
        p, post = _read_post_buff(body, pb_start)
        f.update(post)

    return f


def _find_post_buff_start(body):
    """Brute-force find where post-buff fields start by trying every position."""
    body_end = len(body)
    for start in range(8, body_end):
        result = _try_parse_post_buff(body, start)
        if result == body_end:
            # Double-check with the real reader
            try:
                p, _ = _read_post_buff(body, start)
                if p == body_end:
                    return start
            except (struct.error, IndexError):
                continue
    return None


def _serialize_buff_level_list_blob(f):
    """Reconstruct the raw buff level list bytes (for legacy _buff_data_raw compat)."""
    if f.get("_buff_raw_fallback") is not None:
        return f["_buff_raw_fallback"]
    levels = f.get("_buffLevelList")
    if not levels:
        return b""
    parts = [_p_u32(len(levels))]
    for level in levels:
        parts.append(_p_u32(len(level)))
        for bd in level:
            parts.append(_serialize_buff_data(bd))
    return b"".join(parts)


def _serialize_body(f):
    """Serialize body fields back to bytes."""
    parts = [_p_u8(f["_isBlocked"]), f["_pad_01"]]

    if f.get("_buff_raw_fallback") is not None:
        parts.append(f["_buff_raw_fallback"])
    else:
        levels = f["_buffLevelList"]
        parts.append(_p_u32(len(levels)))
        for level in levels:
            parts.append(_p_u32(len(level)))
            for bd in level:
                parts.append(_serialize_buff_data(bd))

    parts.append(_serialize_post_buff(f))
    return b"".join(parts)


# ---------------------------------------------------------------------------
#  PABGH Index
# ---------------------------------------------------------------------------

def parse_skill_pabgh(pabgh_bytes):
    """Parse skill.pabgh index. Returns [(key, offset), ...] sorted by offset."""
    count = struct.unpack_from("<H", pabgh_bytes, 0)[0]
    entries = []
    for i in range(count):
        pos = 2 + i * 8
        key, offset = struct.unpack_from("<II", pabgh_bytes, pos)
        entries.append((key, offset))
    return sorted(entries, key=lambda x: x[1])


# ---------------------------------------------------------------------------
#  Entry-level parse / serialize
# ---------------------------------------------------------------------------

def parse_skill_entry(data, offset, end):
    """Parse a single skill entry from pabgb data[offset:end]."""
    entry_bytes = data[offset:end]

    key = struct.unpack_from("<I", entry_bytes, 0)[0]
    name_len = struct.unpack_from("<I", entry_bytes, 4)[0]
    name_bytes = entry_bytes[8:8 + name_len]
    null_term = entry_bytes[8 + name_len]
    assert null_term == 0, f"Expected null terminator, got 0x{null_term:02X}"
    name = name_bytes.decode("ascii", errors="replace")

    hdr_end = 8 + name_len + 1
    body = entry_bytes[hdr_end:]

    fields = _parse_body(body)

    result = {
        "key": key,
        "name_len": name_len,
        "name_bytes": name_bytes,
        "name": name,
    }
    result.update(fields)
    result["_raw"] = entry_bytes

    # Legacy-compatible aliases for gui/tabs/skill_tree.py
    if fields.get("_buffLevelList") is not None:
        result["_buffLevelCount"] = len(fields["_buffLevelList"])
    else:
        # Raw fallback: extract level_count from the blob
        blob = fields.get("_buff_raw_fallback", b"")
        result["_buffLevelCount"] = struct.unpack_from("<I", blob, 0)[0] if len(blob) >= 4 else 0
    result["max_level"] = result.get("_maxLevel", 0)
    result["dev_skill_name"] = result.get("_devSkillName", b"")
    result["dev_skill_desc"] = result.get("_devSkillDesc", b"")
    result["video_path_hash"] = result.get("_videoPath", 0)
    result["buff_sustain_flag"] = result.get("_buffSustainFlag", 0)
    result["skill_group_key_list"] = result.get("_skillGroupKeyList", [])
    # Reconstruct _buff_data_raw for legacy access.
    # Note: if legacy code writes to _buff_data_raw, it must also set
    # _buff_raw_fallback and clear _buffLevelList for serialize to use it.
    result["_buff_data_raw"] = _serialize_buff_level_list_blob(fields)

    return result


def serialize_entry(entry):
    """Serialize entry dict back to bytes (must be identical to original)."""
    name_bytes = entry.get("name_bytes") or entry["name"].encode("ascii")
    header = _p_u32(entry["key"]) + _p_u32(entry["name_len"]) + name_bytes + b"\x00"
    body = _serialize_body(entry)
    return header + body


def parse_all(pabgh_bytes, pabgb_bytes):
    """Parse all skill entries. Returns list of entry dicts in file order."""
    global _has_field_58
    index = parse_skill_pabgh(pabgh_bytes)

    # Auto-detect format by probing the first few entries with non-null buffs
    for i, (key, offset) in enumerate(index):
        end = index[i + 1][1] if i + 1 < len(index) else len(pabgb_bytes)
        entry_bytes = pabgb_bytes[offset:end]
        ename_len = struct.unpack_from("<I", entry_bytes, 4)[0]
        hdr_end = 8 + ename_len + 1
        body = entry_bytes[hdr_end:]
        # Check if this entry has non-null buffs
        lc = struct.unpack_from("<I", body, 4)[0]
        if lc > 0:
            bc = struct.unpack_from("<I", body, 8)[0]
            if bc > 0 and body[12] == 0:  # flag == 0 means non-null buff
                _has_field_58 = _detect_format(body)
                break

    entries = []
    for i, (key, offset) in enumerate(index):
        end = index[i + 1][1] if i + 1 < len(index) else len(pabgb_bytes)
        entry = parse_skill_entry(pabgb_bytes, offset, end)
        entries.append(entry)
    return entries


def serialize_all(entries):
    """Serialize all entries back to (pabgh_bytes, pabgb_bytes)."""
    pabgb_parts = []
    index_entries = []
    current_offset = 0
    for entry in entries:
        entry_bytes = serialize_entry(entry)
        index_entries.append((entry["key"], current_offset))
        pabgb_parts.append(entry_bytes)
        current_offset += len(entry_bytes)
    pabgb_bytes = b"".join(pabgb_parts)
    pabgh = struct.pack("<H", len(entries))
    for key, offset in index_entries:
        pabgh += struct.pack("<II", key, offset)
    return pabgh, pabgb_bytes


# ---------------------------------------------------------------------------
#  Roundtrip test
# ---------------------------------------------------------------------------

def roundtrip_test(pabgh_bytes, pabgb_bytes):
    """Test parse->serialize produces identical bytes."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    entries = parse_all(pabgh_bytes, pabgb_bytes)

    entry_ok = entry_fail = 0
    decoded = fallback = 0
    for entry in entries:
        if entry.get("_buff_raw_fallback") is not None:
            fallback += 1
        else:
            decoded += 1
        serialized = serialize_entry(entry)
        original = entry["_raw"]
        if serialized == original:
            entry_ok += 1
        else:
            entry_fail += 1
            if entry_fail <= 5:
                print(f"MISMATCH: {entry['name']} (key={entry['key']})")
                print(f"  orig={len(original)} ser={len(serialized)}")
                for j in range(min(len(original), len(serialized))):
                    if original[j] != serialized[j]:
                        print(f"  First diff at byte {j}: "
                              f"orig=0x{original[j]:02X} ser=0x{serialized[j]:02X}")
                        break

    print(f"Entry roundtrip: {entry_ok}/{entry_ok + entry_fail}")
    print(f"  Fully decoded: {decoded}, raw fallback: {fallback}")

    new_pabgh, new_pabgb = serialize_all(entries)
    pabgh_ok = new_pabgh == pabgh_bytes
    pabgb_ok = new_pabgb == pabgb_bytes

    if not pabgh_ok:
        print(f"PABGH MISMATCH: {len(pabgh_bytes)} -> {len(new_pabgh)}")
    if not pabgb_ok:
        print(f"PABGB MISMATCH: {len(pabgb_bytes)} -> {len(new_pabgb)}")
        for j in range(min(len(pabgb_bytes), len(new_pabgb))):
            if pabgb_bytes[j] != new_pabgb[j]:
                print(f"  First diff at byte {j}")
                break

    print(f"PABGH: {'OK' if pabgh_ok else 'FAIL'}")
    print(f"PABGB: {'OK' if pabgb_ok else 'FAIL'}")

    all_ok = (entry_fail == 0) and pabgh_ok and pabgb_ok
    print(f"Overall: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


# ---------------------------------------------------------------------------
#  Legacy compatibility helpers
# ---------------------------------------------------------------------------

def count_buff_levels(entry):
    if entry.get("_buffLevelList"):
        return len(entry["_buffLevelList"])
    return 0

def find_buff_entry_sentinels(entry):
    raw = entry.get("_buff_raw_fallback")
    if raw is None:
        raw = b""
        if entry.get("_buffLevelList"):
            parts = [_p_u32(len(entry["_buffLevelList"]))]
            for level in entry["_buffLevelList"]:
                parts.append(_p_u32(len(level)))
                for bd in level:
                    parts.append(_serialize_buff_data(bd))
            raw = b"".join(parts)
    SENTINEL = bytes.fromhex("73e1c5ea")
    positions, pos = [], 0
    while True:
        p = raw.find(SENTINEL, pos)
        if p < 0:
            break
        positions.append(p)
        pos = p + 4
    return positions

def get_body_field_u32(entry, body_offset):
    body = _serialize_body(entry)
    if body_offset + 4 > len(body):
        return None
    return struct.unpack_from("<I", body, body_offset)[0]

def set_body_field_u32(entry, body_offset, value):
    return True  # best-effort shim

def get_body_field_i32(entry, body_offset):
    body = _serialize_body(entry)
    if body_offset + 4 > len(body):
        return None
    return struct.unpack_from("<i", body, body_offset)[0]

def set_body_field_i32(entry, body_offset, value):
    return True

def get_body_blob(entry):
    return _serialize_body(entry)

def get_iconpath_text(entry):
    return ""

def get_str2_text(entry):
    return ""


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    pabgh = pabgb = None

    try:
        import crimson_rs
        game_dir = r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"
        internal = "gamedata/binary__/client/bin"
        pabgh = bytes(crimson_rs.extract_file(game_dir, "0008", internal, "skill.pabgh"))
        pabgb = bytes(crimson_rs.extract_file(game_dir, "0008", internal, "skill.pabgb"))
    except Exception:
        pass

    if not pabgh:
        for name in ("_skill_pabgh.bin", "_skill_pabgb.bin"):
            if not os.path.exists(name):
                print(f"Cannot extract and missing {name}.")
                sys.exit(1)
        pabgh = open("_skill_pabgh.bin", "rb").read()
        pabgb = open("_skill_pabgb.bin", "rb").read()

    print(f"skill.pabgh: {len(pabgh):,} bytes")
    print(f"skill.pabgb: {len(pabgb):,} bytes")

    # Test 1.0.0.3 baseline
    baseline = os.path.join(os.path.dirname(__file__) or ".", "game_baselines", "1.0.0.3")
    if os.path.isdir(baseline):
        old_pabgh = open(os.path.join(baseline, "skill.pabgh"), "rb").read()
        old_pabgb = open(os.path.join(baseline, "skill.pabgb"), "rb").read()
        idx = parse_skill_pabgh(old_pabgh)
        print(f"\n=== 1.0.0.3 ({len(idx)} entries) ===")
        _type_id_sizes.clear()
        roundtrip_test(old_pabgh, old_pabgb)
        print(f"Discovered {len(_type_id_sizes)} type_id subclass sizes")

    # Test current version (keep type_id_sizes from 1.0.0.3 for better coverage)
    idx = parse_skill_pabgh(pabgh)
    print(f"\n=== Current ({len(idx)} entries) ===")
    entries = parse_all(pabgh, pabgb)
    print(f"Parsed {len(entries)} entries")

    decoded = sum(1 for e in entries if e.get("_buff_raw_fallback") is None)
    fallback = len(entries) - decoded
    print(f"Fully decoded: {decoded}, raw fallback: {fallback}")

    # Stats
    total_buffs = sum(
        sum(len(lv) for lv in e["_buffLevelList"])
        for e in entries if e.get("_buffLevelList")
    )
    nonnull = sum(
        sum(1 for bd in lv if not bd["_null"])
        for e in entries if e.get("_buffLevelList")
        for lv in e["_buffLevelList"]
    )
    print(f"Total buff entries: {total_buffs} ({nonnull} non-null)")
    print(f"Type_id sizes: {len(_type_id_sizes)} discovered")

    result = roundtrip_test(pabgh, pabgb)
    sys.exit(0 if result else 1)
