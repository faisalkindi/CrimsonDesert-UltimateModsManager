"""Detect folders whose JSON files are mutually-exclusive alternatives.

Some mod packs organise their alternatives as a folder of JSONs where
each one patches the SAME byte offsets with different data (Gild's
Gear: 7 AbyssGear_*.json each rewriting the same 93 shop slots with
different item IDs). The author's intent is "pick one via the mod
manager, switch later". Importing those as 7 independent sibling
mods (the default compound-archive behaviour) creates a confusing
list where only one mod can be enabled at a time anyway.

This detector flags those folders so the importer can route them to
import_multi_variant (one mod row, variants in the cog picker) rather
than the sibling-per-mod path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def json_offsets(json_path: Path) -> set[tuple[str, int]]:
    """Return the set of (game_file, offset) pairs this JSON patches.

    Changes without a numeric offset (e.g. pure entry-anchored) are
    skipped — they're not what the mutex detector cares about.
    """
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    pairs: set[tuple[str, int]] = set()
    for p in data.get("patches", []) or []:
        if not isinstance(p, dict):
            continue
        gf = p.get("game_file") or ""
        if not isinstance(gf, str):
            continue
        for c in p.get("changes", []) or []:
            if not isinstance(c, dict):
                continue
            raw = c.get("offset")
            if raw is None:
                continue
            try:
                off = int(raw, 0) if isinstance(raw, str) else int(raw)
            except (TypeError, ValueError):
                continue
            pairs.add((gf, off))
    return pairs


def detect_mutex_folder_jsons(
    folder: Path,
) -> list[tuple[Path, dict]] | None:
    """If the folder's JSONs are mutex alternatives, return them as
    parsed (path, data) tuples suitable for import_multi_variant.

    Returns None when the folder has < 2 JSONs or the JSONs are
    disjoint (true independent siblings).
    """
    folder = Path(folder)
    if not folder.is_dir():
        return None
    jsons = sorted(p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() == ".json")
    if len(jsons) < 2:
        return None

    offsets = {p: json_offsets(p) for p in jsons}
    # Mutex iff any two JSONs share at least one (file, offset) pair.
    paths = list(offsets.keys())
    mutex = False
    for i, a in enumerate(paths):
        if not offsets[a]:
            continue
        for b in paths[i + 1:]:
            if offsets[a] & offsets[b]:
                mutex = True
                break
        if mutex:
            break
    if not mutex:
        return None

    # Parse each JSON for import_multi_variant. Skip any that don't
    # carry a valid patches list so we don't blow up the caller.
    parsed: list[tuple[Path, dict]] = []
    for p in jsons:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.debug("mutex detector: skipping %s (%s)", p, e)
            continue
        if not isinstance(data, dict) or "patches" not in data:
            continue
        parsed.append((p, data))
    if len(parsed) < 2:
        return None
    return parsed


def collect_archive_mutex_jsons(
    scan_dir: Path,
) -> list[tuple[Path, dict, str]] | None:
    """If the ENTIRE archive (every JSON across every subfolder) is a
    single mutex set, return a flat list of (path, data, label) tuples
    suitable for import_multi_variant.

    Label format: ``"<folder> / <filename-stem>"`` so the cog can show
    the full hierarchy without needing a two-level picker.

    Returns None when:
      * scan_dir has fewer than 2 subfolders with JSONs (single-folder
        case is handled by detect_mutex_folder_jsons instead),
      * any pair of folders targets DISJOINT offsets (truly independent
        categories — user should get the multi-select checkbox dialog),
      * scan_dir itself is not a directory or contains no JSONs.
    """
    scan_dir = Path(scan_dir)
    if not scan_dir.is_dir():
        return None

    # Walk one level down. GildsGear-style: root/<category>/<json>*.
    folder_jsons: dict[str, list[Path]] = {}
    for entry in scan_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        jsons = sorted(
            p for p in entry.iterdir()
            if p.is_file() and p.suffix.lower() == ".json"
        )
        if jsons:
            folder_jsons[entry.name] = jsons

    if len(folder_jsons) < 2:
        return None

    # Build offset set per folder and confirm EVERY pair overlaps.
    per_folder_offsets: dict[str, set[tuple[str, int]]] = {}
    for folder_name, jsons in folder_jsons.items():
        union: set[tuple[str, int]] = set()
        for p in jsons:
            union |= json_offsets(p)
        if union:
            per_folder_offsets[folder_name] = union

    if len(per_folder_offsets) < 2:
        return None

    names = list(per_folder_offsets.keys())
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if not (per_folder_offsets[a] & per_folder_offsets[b]):
                # Found a disjoint pair — NOT a full archive mutex.
                return None

    # Every folder pair overlaps → archive-wide mutex. Flatten.
    result: list[tuple[Path, dict, str]] = []
    for folder_name in sorted(folder_jsons.keys()):
        for p in folder_jsons[folder_name]:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                logger.debug("archive mutex: skipping %s (%s)", p, e)
                continue
            if not isinstance(data, dict) or "patches" not in data:
                continue
            label = f"{folder_name} / {p.stem}"
            result.append((p, data, label))
    if len(result) < 2:
        return None
    return result
