"""Preset CRUD operations for CDUMM's ReShade tab.

  - `import_preset_file`   copy a .ini from anywhere into the preset folder
  - `delete_preset`        safe delete (Recycle Bin via send2trash)
  - `read_preset_for_merge`    parse into {section: {key: value}}
  - `merge_into_main`      asymmetric merge: main + picked sections from other
  - `write_preset_sections`    serialize a merged dict back to .ini

Pure logic; no Qt.
"""
from __future__ import annotations

import configparser
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from send2trash import send2trash

from cdumm.engine.reshade_detect import _is_preset_file

logger = logging.getLogger(__name__)


# ---- Import --------------------------------------------------------------

def import_preset_file(
    src: Path,
    base_path: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Copy a preset `.ini` from `src` into `base_path`, validating that it's
    actually a preset first.

    Raises:
      ValueError       if src doesn't have .ini extension or isn't a preset file
      FileNotFoundError if src doesn't exist
      FileExistsError  if the destination exists and overwrite=False
    """
    if src.suffix.lower() != ".ini":
        raise ValueError(f"Expected a .ini file, got {src.name!r}")
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


# ---- Delete --------------------------------------------------------------

def delete_preset(preset_path: Path) -> None:
    """Move a preset file to the system Recycle Bin.

    Uses send2trash so the user can recover if they delete by mistake.

    Raises:
      FileNotFoundError if the preset doesn't exist.
    """
    if not preset_path.exists():
        raise FileNotFoundError(f"Preset doesn't exist: {preset_path}")
    send2trash(str(preset_path))
    logger.info("Deleted preset (to Recycle Bin): %s", preset_path)


# ---- Merge ---------------------------------------------------------------

@dataclass(frozen=True)
class MergeResult:
    """Output of merge_into_main: the merged sections plus audit lists."""
    sections: dict[str, dict[str, str]]
    added: list[str] = field(default_factory=list)
    overwrote: list[str] = field(default_factory=list)


def read_preset_for_merge(preset_path: Path) -> dict[str, dict[str, str]]:
    """Parse a preset into {section_name: {key: raw_value}}.

    Preserves ORIGINAL CASE of keys so merge + write produces files that
    look similar to what ReShade itself generates (ReShade uses PascalCase:
    `Threshold=`, `Intensity=`, etc.). Standard configparser lowercases keys
    — we bypass that via `optionxform = str`.
    """
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # preserve case of keys
    try:
        parser.read(preset_path, encoding="utf-8")
    except (OSError, configparser.Error) as e:
        logger.debug("read_preset_for_merge: %s", e)
        return {}
    return {sec: dict(parser.items(sec)) for sec in parser.sections()}


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

    Adds a `Techniques=` line if any [*.fx] sections are present but no
    Techniques= key exists — ReShade needs this to know which effects to
    actually run.
    """
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str

    # ReShade requires a Techniques= line or at least one [*.fx] section
    # for the file to register as a preset. We write out sections as-is and
    # synthesize a Techniques= line in a generic section if missing.
    has_techniques = any("Techniques" in values for values in sections.values())
    fx_sections = [sec for sec in sections if sec.lower().endswith(".fx")]

    for section, values in sections.items():
        parser.add_section(section)
        for key, value in values.items():
            parser.set(section, key, value)

    if not has_techniques and fx_sections:
        # Inject a minimal generic section with Techniques= listing every fx.
        generic = "GENERAL"
        if not parser.has_section(generic):
            parser.add_section(generic)
        parser.set(generic, "Techniques",
                   ",".join(sec.removesuffix(".fx") for sec in fx_sections))

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        parser.write(f, space_around_delimiters=False)
    logger.info("Wrote merged preset: %s (%d sections)", path, len(sections))
