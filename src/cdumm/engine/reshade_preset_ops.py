"""Preset CRUD operations for CDUMM's ReShade tab.

  - `import_preset_file`      copy a .ini from anywhere into the preset folder
  - `filter_visible_presets`  drop hidden paths from a preset list (soft-hide
                              from CDUMM's view; the .ini file stays on disk)
  - `read_preset_for_merge`   parse into {section: {key: value}}
  - `merge_into_main`         asymmetric merge: main + picked sections from other
  - `write_preset_sections`   serialize a merged dict back to .ini

Pure logic; no Qt.
"""
from __future__ import annotations

import configparser
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from cdumm.engine.reshade_detect import _is_preset_file

logger = logging.getLogger(__name__)


# ---- Import --------------------------------------------------------------

RESERVED_FILENAMES = {"reshade.ini"}  # case-insensitive compare


def import_preset_file(
    src: Path,
    base_path: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Copy a preset `.ini` from `src` into `base_path`, validating that it's
    actually a preset first.

    Raises:
      ValueError       if src isn't a .ini, isn't a preset, or is named like
                       a ReShade config file (`ReShade.ini`). Rejecting the
                       reserved name prevents accidentally wiping the user's
                       ReShade config by importing a preset pack that happens
                       to include its own ReShade.ini.
      FileNotFoundError if src doesn't exist
      FileExistsError  if the destination exists and overwrite=False
    """
    if src.suffix.lower() != ".ini":
        raise ValueError(f"Expected a .ini file, got {src.name!r}")
    if src.name.lower() in RESERVED_FILENAMES:
        raise ValueError(
            f"Refusing to import {src.name!r}: that name is reserved for "
            "ReShade's own config file. Rename the preset and try again.")
    if not src.exists():
        raise FileNotFoundError(f"Source file doesn't exist: {src}")
    if not _is_preset_file(src):
        raise ValueError(
            f"{src.name!r} does not look like a ReShade preset "
            "(no Techniques= line or [*.fx] section). "
            "Double-check you picked a preset file, not a config file.")

    base_path.mkdir(parents=True, exist_ok=True)
    dest = base_path / src.name
    if dest.exists() and not overwrite:
        raise FileExistsError(
            f"A preset named {src.name!r} already exists at {dest}. "
            "Rename the file or pass overwrite=True.")

    shutil.copy2(src, dest)
    logger.info("Imported preset: %s -> %s", src, dest)
    return dest


# ---- Hide (soft-delete) ---------------------------------------------------

def _canonical_path(p: Path | str) -> str:
    """Normalize a path for comparison (case-insensitive on Windows,
    collapses mixed separators and ./ segments)."""
    s = str(p)
    return os.path.normcase(os.path.normpath(s))


def relative_to_base(preset: Path, base_path: Path) -> str:
    """Return a preset's path expressed relative to base_path when possible,
    else the absolute path. Uses forward slashes for stability across systems.

    Storing hidden/identifier paths relative to base_path means the state
    survives moving the game directory -- the filenames and substructure
    stay stable even if the root moves.
    """
    try:
        rel = preset.resolve(strict=False).relative_to(
            base_path.resolve(strict=False))
        return str(rel).replace("\\", "/")
    except (ValueError, OSError):
        return str(preset)


def filter_visible_presets(
    presets: list[Path],
    hidden: set[str],
    base_path: Path | None = None,
) -> list[Path]:
    """Drop paths in `hidden` from `presets`, preserving order of the rest.

    Each entry in `hidden` can be either:
      - an absolute path (legacy / cross-install user), OR
      - a path relative to `base_path` (preferred new format).

    Both forms are normalized and compared case-insensitively so a single
    hidden-list can match regardless of which form was saved.

    Stale entries that don't correspond to any current preset are silently
    ignored -- this is fail-safe: if the user's game dir moved and the hidden
    list is stale, presets re-appear rather than staying orphaned.
    """
    if not hidden:
        return list(presets)

    # Build the comparison set: canonical absolute path + canonical relative path.
    comparison_set: set[str] = set()
    for h in hidden:
        comparison_set.add(_canonical_path(h))
        if base_path is not None and not Path(h).is_absolute():
            comparison_set.add(_canonical_path(base_path / h))

    def _matches_hidden(preset: Path) -> bool:
        if _canonical_path(preset) in comparison_set:
            return True
        if base_path is not None:
            rel = relative_to_base(preset, base_path)
            if _canonical_path(rel) in comparison_set:
                return True
        return False

    return [p for p in presets if not _matches_hidden(p)]


# ---- Merge ---------------------------------------------------------------

@dataclass(frozen=True)
class MergeResult:
    """Output of merge_into_main: the merged sections plus audit lists."""
    sections: dict[str, dict[str, str]]
    added: list[str] = field(default_factory=list)
    overwrote: list[str] = field(default_factory=list)


# Special section name used internally to hold keys that appear at the top
# of a preset file BEFORE any [section] header. ReShade's own preset files
# often start with Techniques=, PreprocessorDefinitions=, and shader-binding
# keys at the top level — there's no section header for these. configparser
# can't parse that layout (MissingSectionHeaderError), so we stash them in
# this synthetic section for merging and emit them back without the header
# on write.
_PREAMBLE_SECTION = "__preamble__"


def read_preset_for_merge(preset_path: Path) -> dict[str, dict[str, str]]:
    """Parse a preset into {section_name: {key: raw_value}}.

    Preserves ORIGINAL CASE of keys so merge + write produces files that
    look similar to what ReShade itself generates (ReShade uses PascalCase:
    `Threshold=`, `Intensity=`, etc.). Standard configparser lowercases keys
    — we bypass that via `optionxform = str`.

    Handles the ReShade-specific preset layout where `Techniques=` and
    other keys appear at the top of the file BEFORE any `[section]` header:
    those keys are stashed under a synthetic `__preamble__` section so they
    survive parsing, merging, and writing.
    """
    try:
        raw_text = preset_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("read_preset_for_merge: read failed %s: %s", preset_path, e)
        return {}

    # Check if the file starts with keys before the first section header.
    # If so, prepend a synthetic section so configparser accepts the file.
    text_for_parser = raw_text
    first_header_idx = _find_first_section_header(raw_text)
    if first_header_idx != 0:
        text_for_parser = f"[{_PREAMBLE_SECTION}]\n{raw_text}"

    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text_for_parser)
    except configparser.Error as e:
        logger.debug("read_preset_for_merge: parse failed %s: %s", preset_path, e)
        return {}
    return {sec: dict(parser.items(sec)) for sec in parser.sections()}


def _find_first_section_header(text: str) -> int:
    """Return the char index where the first `[section]` header begins, or
    0 if the file starts with a section, or -1 if no header exists.

    Lines before the first header that contain `=` signs (keys with no
    section) are what we need to handle specially.
    """
    import re
    for match in re.finditer(r"^\s*\[[^\]]+\]\s*$", text, re.MULTILINE):
        return match.start()
    return -1


def merge_into_main(
    main: dict[str, dict[str, str]],
    other: dict[str, dict[str, str]],
    sections_to_take: list[str],
) -> MergeResult:
    """Asymmetric merge: `main` stays as the base; each section in
    `sections_to_take` is copied from `other` into the result (overwriting
    any existing section in `main`).

    Sections that exist only in `main` are always preserved. Sections from
    `other` that the user didn't pick are ignored.
    """
    merged = {sec: dict(values) for sec, values in main.items()}
    added: list[str] = []
    overwrote: list[str] = []

    for section in sections_to_take:
        if section not in other:
            logger.debug("merge: requested section %s not in other; skipping",
                         section)
            continue
        if section in merged:
            overwrote.append(section)
        else:
            added.append(section)
        merged[section] = dict(other[section])

    return MergeResult(sections=merged, added=added, overwrote=overwrote)


def write_preset_sections(
    path: Path,
    sections: dict[str, dict[str, str]],
) -> None:
    """Serialize a {section: {key: value}} dict to `path` as a ReShade preset.

    The synthetic `__preamble__` section (if present) is emitted FIRST as
    bare top-level keys with no section header — this is the ReShade layout
    that put them in __preamble__ in the first place, so round-tripping
    produces a file ReShade can read.

    Other sections are written as standard `[section]` blocks. No synthesis
    of Techniques= or any other key — if the merged dict has no Techniques=
    line, ReShade initializes with no effects enabled, which is safe.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    preamble = sections.get(_PREAMBLE_SECTION, {})
    regular = {k: v for k, v in sections.items() if k != _PREAMBLE_SECTION}

    with open(path, "w", encoding="utf-8") as f:
        # Preamble: bare keys at the top of the file (ReShade's own format).
        for key, value in preamble.items():
            f.write(f"{key}={value}\n")

        # Regular sections, separated by a blank line.
        if preamble and regular:
            f.write("\n")
        parser = configparser.RawConfigParser(interpolation=None)
        parser.optionxform = str
        for section, values in regular.items():
            parser.add_section(section)
            for key, value in values.items():
                parser.set(section, key, value)
        parser.write(f, space_around_delimiters=False)

    total = len(preamble) + sum(len(v) for v in regular.values())
    logger.info(
        "Wrote merged preset: %s (%d sections, %d keys including preamble)",
        path, len(regular), total)
