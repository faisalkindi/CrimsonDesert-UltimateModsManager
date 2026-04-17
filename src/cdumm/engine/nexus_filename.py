"""Shared NexusMods filename parser.

Used by both the GUI (single-drop) and worker (batch import) to extract
the mod id and file version from NexusMods download filenames.

Format: ``{ModName}-{mod_id}-{file_version_parts_dashed}-{unix_timestamp}``

Examples::

    'Legendary Bear Without Tack-934-2-1775958271' -> (934, '2')
    'Better Radial Menus (RAW)-618-1-4-1775912922' -> (618, '1.4')
    'No Letterbox (RAW)-208-1-4-2-1775938453' -> (208, '1.4.2')

Master branch note: even though CDUMM master ships without Nexus API
integration, this parser still runs at import time so the mod's
version column can be populated from the filename. Without it,
most Nexus-downloaded mods import with an empty version field since
mod authors rarely embed version in modinfo.json.
"""
from __future__ import annotations

import re


_NON_GREEDY = re.compile(r'^.+?-(\d+)-(.+)-(\d{10})$')
_GREEDY_ANCHORED = re.compile(
    r'^(.+)-(\d+)-(\d+(?:-\d+){0,2}|-\d+)-(\d{10})$')


def parse_nexus_filename(name: str) -> tuple[int | None, str]:
    """Parse a NexusMods download filename stem.

    Returns ``(nexus_mod_id, file_version)`` or ``(None, '')`` if the
    name does not match the NexusMods convention. The 10-digit unix
    timestamp anchors the end of the pattern.

    Two regexes are used. The primary is a non-greedy match, which
    correctly handles multi-segment versions like ``1-4-2``
    (-> ``"1.4.2"``). When that regex returns a mod_id that falls in
    the 1900-2099 range, the display name probably ended in a year and
    the year was captured instead of the real mod id — we retry with a
    right-anchored greedy regex that ties the version to 1-3 numeric
    segments, letting the display name consume the year prefix.
    """
    m = _NON_GREEDY.match(name)
    if not m:
        return None, ''
    mod_id = int(m.group(1))

    if 1900 <= mod_id <= 2099:
        m2 = _GREEDY_ANCHORED.match(name)
        if m2:
            candidate_id = int(m2.group(2))
            if candidate_id != mod_id and 1 <= candidate_id <= 999999:
                return candidate_id, m2.group(3).replace('-', '.')

    file_ver = m.group(2).replace('-', '.')
    if mod_id < 1 or mod_id > 999999:
        return None, ''
    return mod_id, file_ver


# ── Fallback patterns for non-Nexus-formatted filenames ──────────────

# v-prefixed version attached to word boundary (most reliable):
#   ``NSLWInventoryMod_v107_BagBoost`` -> "107"
#   ``Better Trade Menu v2.1 Fix``     -> "2.1"
#   ``stamina_v1.02.00_infinite``      -> "1.02.00"
_V_PREFIXED = re.compile(r'(?:^|[_\s.\-])v(\d+(?:\.\d+)*)', re.IGNORECASE)

# Bare dotted version (at least one dot to avoid swallowing random ints):
#   ``Glider Stamina Unlimited (1.03.00)`` -> "1.03.00"
#   ``Animations Trimmer 1.03.00``         -> "1.03.00"
_DOTTED_VERSION = re.compile(
    r'(?:^|[_\s\(\[\-])(\d+\.\d+(?:\.\d+)*)(?=[_\s\)\]\-\.]|$)')


def extract_version_from_filename(name: str) -> str:
    """Best-effort version extraction from a mod's filename.

    Tries three sources, in order:

    1. NexusMods timestamped format (``ModName-id-ver-ts``) via
       :func:`parse_nexus_filename`.
    2. ``v``-prefixed version anywhere in the name.
    3. Bare dotted version number.

    Returns an empty string when nothing matches. Safe to call on any
    filename; never raises.
    """
    # 1. Nexus format
    _id, ver = parse_nexus_filename(name)
    if ver:
        return ver
    # 2. v-prefixed (e.g. 'Mod_v1.2', 'ModName v3')
    m = _V_PREFIXED.search(name)
    if m:
        return m.group(1)
    # 3. Bare dotted version (e.g. 'Mod 1.03.00')
    m = _DOTTED_VERSION.search(name)
    if m:
        return m.group(1)
    return ''
