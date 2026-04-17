# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Ported into CDUMM from:
#   NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS (MPL-2.0)
#   https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS
#   CrimsonGameMods/characterinfo_full_parser.py
# Copyright (c) 2026 RicePaddySoftware
# Minor adjustments for CDUMM import hygiene (no behavioural changes).
"""CharacterInfo PABGB parser — extracts mount, NPC, and combat fields from
characterinfo.pabgb using the IDA-decoded reader order (sub_141037900).

Parses the header + scalar fields + boolean block covering:
  - Mount fields: _vehicleInfo, _callMercenaryCoolTime, _callMercenarySpawnDuration
  - Combat fields: _isAttackable, _isAggroTargetable, _sendKillEventOnDead, _invincibility
  - NPC fields: _isEnableFriendly, and 40 other boolean flags
"""

import logging
import struct

log = logging.getLogger(__name__)


def parse_pabgh_index(pabgh_data):
    """Parse characterinfo.pabgh: u16 count, then count * (u32 key + u32 offset)."""
    count = struct.unpack_from('<H', pabgh_data, 0)[0]
    entries = {}
    pos = 2
    for _ in range(count):
        key = struct.unpack_from('<I', pabgh_data, pos)[0]
        offset = struct.unpack_from('<I', pabgh_data, pos + 4)[0]
        entries[key] = offset
        pos += 8
    return entries


def _read_cstring(data, p):
    """Read u32-length-prefixed string, return (string, new_pos)."""
    slen = struct.unpack_from('<I', data, p)[0]
    p += 4
    if slen > 100000:
        return None, p
    s = data[p:p + slen].decode('utf-8', errors='replace')
    return s, p + slen


def _read_locstr(data, p):
    """Read LocalizableString: u8 flag + u64 hash + CString. Return new_pos."""
    p += 1
    p += 8
    slen = struct.unpack_from('<I', data, p)[0]
    p += 4 + slen
    return p


def _read_locstr_with_hash(data, p):
    """Read LocalizableString and return (u64_hash, new_pos).

    The u64 hash is the key used by the paloc localization table.
    """
    p += 1
    hv = struct.unpack_from('<Q', data, p)[0]
    p += 8
    slen = struct.unpack_from('<I', data, p)[0]
    p += 4 + slen
    return hv, p


def parse_entry(data, offset, end):
    """Parse one CharacterInfo entry through the boolean block.

    Returns dict with all parsed fields + byte offsets for in-place editing,
    or None on parse failure.
    """
    p = offset
    result = {}

    try:
        result['entry_key'] = struct.unpack_from('<I', data, p)[0]; p += 4
        name, p = _read_cstring(data, p)
        if name is None:
            return None
        result['name'] = name
        result['_isBlocked'] = data[p]; p += 1

        name_hash, p = _read_locstr_with_hash(data, p)
        desc_hash, p = _read_locstr_with_hash(data, p)
        result['_characterName_hash'] = name_hash
        result['_characterDesc_hash'] = desc_hash

        p += 4
        p += 4

        _, p = _read_cstring(data, p)

        p += 1
        p += 1
        p += 4
        p += 4

        result['_vehicleInfo_offset'] = p
        result['_vehicleInfo'] = struct.unpack_from('<H', data, p)[0]
        p += 2

        result['_callMercenaryCoolTime_offset'] = p
        result['_callMercenaryCoolTime'] = struct.unpack_from('<Q', data, p)[0]
        p += 8

        result['_callMercenarySpawnDuration_offset'] = p
        result['_callMercenarySpawnDuration'] = struct.unpack_from('<Q', data, p)[0]
        p += 8

        result['_mercenaryCoolTimeType'] = data[p]; p += 1

        p += 4 + 2
        p += 4 + 2

        p += 4
        result['_appearanceName_stream_offset'] = p + 12
        result['_appearanceName_key'] = struct.unpack_from('<I', data, p + 12)[0]
        result['_characterPrefabPath_stream_offset'] = p + 16
        result['_characterPrefabPath_key'] = struct.unpack_from('<I', data, p + 16)[0]
        p += 28
        p += 4
        p += 8
        p += 4
        p += 4
        p += 4
        p += 4
        p += 4
        p += 1
        p += 1

        p += 1

        p += 1

        p = _read_locstr(data, p)

        p += 4

        p += 1
        p += 2

        bool_start = p
        bool_fields = {}
        for bi in range(40):
            bool_fields[bi] = data[p + bi]

        result['_isAttackable_offset'] = bool_start + 3
        result['_isAttackable'] = data[bool_start + 3]

        result['_isAggroTargetable_offset'] = bool_start + 4
        result['_isAggroTargetable'] = data[bool_start + 4]

        result['_sendKillEventOnDead_offset'] = bool_start + 17
        result['_sendKillEventOnDead'] = data[bool_start + 17]

        result['_invincibility_offset'] = bool_start + 20
        result['_invincibility'] = data[bool_start + 20]

        result['_boolBlock'] = bool_fields

        p += 40

        result['_parsed_bytes'] = p - offset
        result['_entry_size'] = end - offset

    except (struct.error, IndexError) as e:
        log.debug("Parse error for %s at offset %d: %s", result.get('name', '?'), p, e)
        return None

    return result


def parse_all_entries(pabgb_data, pabgh_data):
    """Parse all CharacterInfo entries, return list of dicts."""
    idx = parse_pabgh_index(pabgh_data)
    sorted_entries = sorted(idx.items(), key=lambda x: x[1])

    results = []
    for i, (key, eoff) in enumerate(sorted_entries):
        if i + 1 < len(sorted_entries):
            end = sorted_entries[i + 1][1]
        else:
            end = len(pabgb_data)
        r = parse_entry(pabgb_data, eoff, end)
        if r:
            results.append(r)

    return results


def build_name_to_body_offset(pabgb_data, pabgh_data):
    """Return a dict mapping entry name -> absolute offset into pabgb body.

    Offsets are the SAME values that `parse_pabgh_index` returns, just keyed
    by entry name instead of u32 key. This is what the JMM v2 / SWISS Knife
    `entry + rel_offset` format anchors against — the mod expresses
    `rel_offset` relative to this returned offset.
    """
    idx = parse_pabgh_index(pabgh_data)
    entries = parse_all_entries(pabgb_data, pabgh_data)
    name_to_offset: dict[str, int] = {}
    for e in entries:
        name = e.get('name')
        key = e.get('entry_key')
        if not name or key is None:
            continue
        body_offset = idx.get(key)
        if body_offset is None:
            continue
        name_to_offset[name] = body_offset
    return name_to_offset
