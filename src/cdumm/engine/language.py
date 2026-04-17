"""Steam language detection + localisation-group mapping.

Ports JMM V9.9.1's ``STEAM_LANG_TO_GROUP`` / ``GROUP_TO_PALOC_SUFFIX`` tables
plus ``DetectSteamLanguage``. Lets a PAZ-replacement mod that ships localised
assets target the user's actual Steam language by redirecting its group dir
and rewriting the ``localizationstring_<suffix>.paloc`` filename inside the
mod's PAMT (see ``paz_parse.rewrite_pamt_localization_filename``).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STEAM_APP_ID = "3321460"

# Steam language → Crimson Desert group directory.
STEAM_LANG_TO_GROUP: dict[str, str] = {
    "korean":    "0019",
    "english":   "0020",
    "japanese":  "0021",
    "russian":   "0022",
    "turkish":   "0023",
    "spanish":   "0024",
    "latam":     "0025",
    "french":    "0026",
    "german":    "0027",
    "italian":   "0028",
    "polish":    "0029",
    "brazilian": "0030",
    "tchinese":  "0031",
    "schinese":  "0032",
}

# Group → .paloc suffix embedded in the PAMT.
GROUP_TO_PALOC_SUFFIX: dict[str, str] = {
    "0019": "kor",
    "0020": "eng",
    "0021": "jpn",
    "0022": "rus",
    "0023": "tur",
    "0024": "spa-es",
    "0025": "spa-mx",
    "0026": "fre",
    "0027": "ger",
    "0028": "ita",
    "0029": "pol",
    "0030": "por-br",
    "0031": "zho-tw",
    "0032": "zho-cn",
}

LOCALIZATION_GROUPS: frozenset[str] = frozenset(GROUP_TO_PALOC_SUFFIX.keys())


def detect_steam_language(game_dir: Path) -> str | None:
    """Read the Crimson Desert appmanifest for the active Steam language.

    Mirrors JMM ``DetectSteamLanguage`` — looks at
    ``<game_dir>/../../appmanifest_3321460.acf`` (Steam stores these in the
    library root, two levels above the game install). Returns the language
    token (e.g. ``"english"``, ``"korean"``) or ``None`` if the manifest
    can't be parsed.
    """
    manifest = game_dir.parent.parent / f"appmanifest_{STEAM_APP_ID}.acf"
    if not manifest.exists():
        return None
    try:
        for raw in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line.lower().startswith('"language"'):
                continue
            parts = [p for p in line.split('"') if p]
            if len(parts) >= 2:
                return parts[-1]
    except OSError as e:
        logger.debug("Steam language detection failed: %s", e)
    return None


def lang_to_group(language: str | None) -> str | None:
    """Resolve ``detect_steam_language`` output to a group dir (or None)."""
    if not language:
        return None
    return STEAM_LANG_TO_GROUP.get(language.lower())


def rewrite_localization_pamt_for_language(
    pamt_bytes: bytes, source_group: str, target_group: str,
) -> bytes | None:
    """Rewrite a single-file localisation PAMT so its embedded ``.paloc``
    filename uses ``target_group``'s suffix instead of ``source_group``'s.

    Returns the rewritten bytes or None if the rewrite isn't applicable
    (non-localisation group, unchanged target, or pattern not found).
    """
    if source_group == target_group:
        return None
    from_suffix = GROUP_TO_PALOC_SUFFIX.get(source_group)
    to_suffix = GROUP_TO_PALOC_SUFFIX.get(target_group)
    if not from_suffix or not to_suffix:
        return None
    from cdumm.archive.paz_parse import rewrite_pamt_localization_filename
    return rewrite_pamt_localization_filename(
        pamt_bytes, from_suffix, to_suffix)
