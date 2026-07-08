# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Ported into CDUMM from:
#   NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS (MPL-2.0)
#   https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS
#   CrimsonGameMods/characterinfo_full_parser.py
# Copyright (c) 2026 RicePaddySoftware
# Re-ported for the 1.13.00 layout drift (every record was failing to
# parse). CDUMM was still carrying the pre-1.13 fixed-offset tail walk;
# this adopts upstream's adaptive bool-block scan and, critically,
# stops discarding a record's already-successfully-read fields (name,
# hash block: appearance/prefab/skeleton/skeletonVariation) just
# because an unrelated field further into the record could not be
# located.
"""CharacterInfo PABGB parser — extracts mount, NPC, and combat fields from
characterinfo.pabgb using the IDA-decoded reader order (sub_141037900).

Parses the header + scalar fields + boolean block covering:
  - Mount fields: _vehicleInfo, _callMercenaryCoolTime, _callMercenarySpawnDuration
  - Combat fields: _isAttackable, _isAggroTargetable, _sendKillEventOnDead, _invincibility
  - NPC fields: _isEnableFriendly, and 40 other boolean flags

As of game patch 1.13.00, the stretch of the record between the
action-chart/skeleton hash block and the boolean-flags block no longer
has a fixed byte length (Pearl Abyss appears to have inserted at least
one new field there). The hash block itself -- covering upper/lower
action chart, appearance, prefab path, skeleton name and skeleton
variation -- is unaffected and still sits at the same fixed offsets
relative to its own start. Rather than walk a fixed byte count to
reach the boolean block (which now overshoots/undershoots and throws),
we scan for it. If the scan fails, the record is still returned with
every field parsed up to that point intact and ``_partial_parse`` set,
instead of being discarded outright.

``_flagC`` (GitHub #150's fifth field) sat at a fixed offset relative
to the hash block in the pre-1.13 layout, but that offset is no longer
reliable -- a byte-value spot check across all 1.13 vanilla records
shows it is no longer a clean 0/1/2 enum for every entry. Rather than
risk writing to the wrong byte for the records where it has drifted,
``_flagC`` is no longer resolved here; the ``flag_c`` Format 3 field
now consistently reports "could not locate field" instead of writing a
plausible-looking but wrong offset. Re-deriving it properly is left for
a follow-up.
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


def _find_bool_block(data, search_start, end):
    """Scan for the bool block: 8+ consecutive bytes that are all 0 or 1.

    Replaces a fixed byte-count walk, which broke on 1.13.00 when Pearl
    Abyss changed the length of the stretch between the hash block and
    the bool block. Starts searching from search_start (right after the
    hash-fields block).
    """
    limit = min(end, search_start + 300)
    for bp in range(search_start, limit - 8):
        if all(data[bp + j] in (0, 1) for j in range(8)):
            extended = data[bp:bp + 20] if bp + 20 <= end else data[bp:end]
            if sum(1 for b in extended if b in (0, 1)) >= min(15, len(extended)):
                return bp
    return None


def parse_entry(data, offset, end):
    """Parse one CharacterInfo entry through the boolean block.

    Returns dict with all parsed fields + byte offsets for in-place editing,
    or None on parse failure.

    Only a parse failure *before* the hash block (name / header fields)
    discards the whole record. A failure locating the bool block (which
    sits past a stretch of the record whose length can vary by game
    version) still returns every field read up to that point, with
    ``_partial_parse`` set, so mods that only touch hash-block fields
    (appearance, prefab path, skeleton, skeleton variation) keep working
    even when the bool-block fields can't be found.
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
        # Action-chart / skeleton package block: seven consecutive u32
        # name-hash keys. The block sits at a record-dependent offset
        # (variable-length CStrings precede it), so it is located by
        # walking, not a fixed offset. Field positions verified against
        # vanilla 1.07.00 and re-verified byte-identical on 1.13.00: the
        # Damian record holds the exact hashes the Female Animations mod
        # (GitHub #150) copies onto Kliff. This block is unaffected by
        # the 1.13.00 drift; only what follows it changed length.
        result['_upperActionChartPackageGroupName_offset'] = p
        result['_upperActionChartPackageGroupName_key'] = struct.unpack_from('<I', data, p)[0]
        result['_lowerActionChartPackageGroupName_offset'] = p + 4
        result['_lowerActionChartPackageGroupName_key'] = struct.unpack_from('<I', data, p + 4)[0]
        result['_appearanceName_stream_offset'] = p + 12
        result['_appearanceName_key'] = struct.unpack_from('<I', data, p + 12)[0]
        result['_characterPrefabPath_stream_offset'] = p + 16
        result['_characterPrefabPath_key'] = struct.unpack_from('<I', data, p + 16)[0]
        result['_skeletonName_offset'] = p + 20
        result['_skeletonName_key'] = struct.unpack_from('<I', data, p + 20)[0]
        result['_skeletonVariationName_offset'] = p + 24
        result['_skeletonVariationName_key'] = struct.unpack_from('<I', data, p + 24)[0]
        p += 28

        # Everything from here on drifted in 1.13.00 by a variable
        # amount, so it gets its own try/except: a failure here must
        # not throw away the hash-block fields already captured above.
        try:
            bool_start = _find_bool_block(data, p, end)
            if bool_start is not None and bool_start + 40 <= end:
                bool_fields = {}
                for bi in range(40):
                    bool_fields[bi] = data[bool_start + bi]

                result['_invincibility_offset'] = bool_start + 0
                result['_invincibility'] = data[bool_start + 0]

                result['_isAttackable_offset'] = bool_start + 1
                result['_isAttackable'] = data[bool_start + 1]

                result['_isAggroTargetable_offset'] = bool_start + 2
                result['_isAggroTargetable'] = data[bool_start + 2]

                result['_isValid_offset'] = bool_start + 3
                result['_isValid'] = data[bool_start + 3]

                result['_boolBlock'] = bool_fields
                result['_parsed_bytes'] = (bool_start + 40) - offset
            else:
                result['_partial_parse'] = True

            result['_entry_size'] = end - offset
        except (struct.error, IndexError) as e:
            log.debug(
                "Partial parse for %s (post-hash-block fields skipped): %s",
                result.get('name', '?'), e)
            result['_partial_parse'] = True
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
