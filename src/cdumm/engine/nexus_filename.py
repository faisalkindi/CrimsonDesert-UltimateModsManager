"""Shared NexusMods filename parser.

Used by both the GUI (single-drop) and worker (batch import) to extract
mod_id and version from NexusMods download filenames.

Nexus has named downloads two ways.

Dash-separated:
    '{FileName}-{mod_id}-{file_version_parts_dashed}-{unix_timestamp}'
    'Legendary Bear Without Tack-934-2-1775958271' -> (934, '2')
    'Better Radial Menus (RAW)-618-1-4-1775912922' -> (618, '1.4')
    'No Letterbox (RAW)-208-1-4-2-1775938453' -> (208, '1.4.2')

Space-separated, in bug reports from July 2026 on (GitHub #246, #379):
    '{FileName} {mod_id} {version} {YYYY-MM-DDTHH-MMZ} {9-char id}'
    'No Peach Fuzz 3941 1 2026-10-31T18-11Z vEjq5ZqS9' -> (3941, '1')
    'CD QOL Suite 1591 2.2 2026-06-09T02-45Z 5M3ctPNGm' -> (1591, '2.2')
and the same without the timestamp:
    'NPC Trust Gain 350 1.13 hQ8Cntr88' -> (350, '1.13')
The version keeps its dots in this form, and can carry letters
('Cheap Gold Bars 2190 CD1.13.01 2026-07-08T08-25Z 2kQtbOaer').
"""
from __future__ import annotations

import re
from typing import NamedTuple

_NON_GREEDY = re.compile(r'^.+?-(\d+)-(.+)-(\d{10})$')
_GREEDY_ANCHORED = re.compile(
    r'^(.+)-(\d+)-(\d+(?:-\d+){0,2}|-\d+)-(\d{10})$')

# Space-separated form. Every observed name ends in a 9-character id;
# most also carry the timestamp before it.
_SPACED_STAMPED = re.compile(
    r'^(?P<name>.+) (?P<mod_id>\d+) (?P<version>\S+) '
    r'\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z [A-Za-z0-9]{9}$')
_SPACED_BARE = re.compile(
    r'^(?P<name>.+) (?P<mod_id>\d+) (?P<version>\S*\d\S*) '
    r'(?P<token>[A-Za-z0-9]{9})$')
# Without the timestamp, the 9-character id is all that marks the name
# as Nexus's, so a last word that just happens to be nine characters
# long ('1 - Copper replaces Abyssal Artifacts', #466) must not count.
# The ids seen in reports mix cases and digits ('hQ8Cntr88',
# 'UHbidxlb7', 'oj90sq0xd'); none is a plain word, a word followed by
# digits, or all digits.
_PLAIN_WORD = re.compile(r'[A-Z]?[a-z]+\d*|[A-Z]+\d*|\d+')

# A second download of the same file gets '(1)' (Firefox) or ' (1)'
# (Chromium) before the extension: 'vEjq5ZqS9(1).zip' (#465).
_DUPLICATE_SUFFIX = re.compile(r' ?\(\d{1,3}\)$')

# CDUMM stores nested-variant imports as '<archive>||<variant path>' in
# drop_name. '|' cannot appear in a Windows filename, so everything from
# the first '||' on is CDUMM's, not Nexus's.
_VARIANT_MARKER = '||'

# Archive extensions that commonly survive into the stored ``drop_name``
# (especially for manual file drops vs nxm:// downloads). The regex
# anchors on a 10-digit unix timestamp at end-of-string, which breaks
# whenever a trailing ``.zip`` / ``.7z`` / ``.rar`` is left in place.
_ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar", ".paz")


class NexusFilename(NamedTuple):
    """The parts of a NexusMods download name."""

    name: str  # the file's name on the mod page, e.g. 'No Peach Fuzz'
    mod_id: int
    version: str


def _strip_wrapping(name: str) -> str:
    """Drop what CDUMM, the archive and the browser add around a name."""
    name = name.split(_VARIANT_MARKER, 1)[0]
    lowered = name.lower()
    for ext in _ARCHIVE_EXTENSIONS:
        if lowered.endswith(ext):
            name = name[: -len(ext)]
            break
    return _DUPLICATE_SUFFIX.sub('', name)


def split_nexus_filename(name: str) -> NexusFilename | None:
    """Split a NexusMods download name into file name, mod id and version.

    Returns ``None`` when the name follows neither Nexus convention (see
    the module docstring). ``||<variant>`` drop_name suffixes, archive
    extensions and browser ``(1)`` suffixes are ignored.

    Dash-separated names are tried first, exactly as before the
    space-separated form was added (Codex P2 regression fix):

    Two regexes are used. The primary is the historical non-greedy
    match, which correctly handles multi-segment versions like
    ``1-4-2`` (→ ``"1.4.2"``). When that regex returns a mod_id that
    falls in the 1900-2099 range, the display name probably ended in a
    year and the year was captured instead of the real mod id — we
    retry with a right-anchored greedy regex that ties the version to
    1-3 numeric segments, which lets the display name consume the year
    prefix.

    No dash-separated name can match the space-separated patterns, or
    the other way round: one ends in ``-`` plus 10 digits, the other in
    a space plus 9 characters.
    """
    name = _strip_wrapping(name)
    m = _NON_GREEDY.match(name)
    if m:
        mod_id = int(m.group(1))
        if 1900 <= mod_id <= 2099:
            # Year-in-display-name heuristic — retry with greedy anchor.
            m2 = _GREEDY_ANCHORED.match(name)
            if m2:
                candidate_id = int(m2.group(2))
                if candidate_id != mod_id and 1 <= candidate_id <= 999999:
                    return NexusFilename(
                        m2.group(1), candidate_id,
                        m2.group(3).replace('-', '.'))
        if mod_id < 1 or mod_id > 999999:
            return None
        return NexusFilename(
            name[: m.start(1) - 1], mod_id, m.group(2).replace('-', '.'))

    m = _SPACED_STAMPED.match(name)
    if not m:
        m = _SPACED_BARE.match(name)
        if m and _PLAIN_WORD.fullmatch(m.group('token')):
            m = None
    if not m:
        return None
    mod_id = int(m.group('mod_id'))
    if mod_id < 1 or mod_id > 999999:
        return None
    return NexusFilename(m.group('name'), mod_id, m.group('version'))


def parse_nexus_filename(name: str) -> tuple[int | None, str]:
    """Parse a NexusMods download filename stem.

    Returns (nexus_mod_id, file_version) or (None, '') if the name does
    not match a NexusMods convention. See :func:`split_nexus_filename`
    for the forms recognised.

    A trailing archive extension (``.zip`` / ``.7z`` / ``.rar`` /
    ``.paz``) is stripped before matching so manual file drops parse
    the same way nxm:// downloads do. Without this, ``drop_name``
    rendering fell through to the em-dash placeholder for any mod
    imported as a raw archive.
    """
    parts = split_nexus_filename(name)
    if parts is None:
        return None, ''
    return parts.mod_id, parts.version


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

    1. NexusMods download names (``ModName-id-ver-ts`` or
       ``ModName id ver [timestamp] xxxxxxxxx``) via
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
