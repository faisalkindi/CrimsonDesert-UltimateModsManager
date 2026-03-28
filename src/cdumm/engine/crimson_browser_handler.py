"""Crimson Browser mod format handler.

Detects mods in the Crimson Browser format (manifest.json + loose files)
and converts them to standard PAZ modifications that CDUMM can import.

Crimson Browser format:
    manifest.json  -> {"format": "crimson_browser_mod_v1", "id": "...", "files_dir": "files"}
    files/NNNN/path/to/file.css  -> loose file to repack into PAZ

The handler:
1. Reads manifest.json to find the files directory
2. Maps each loose file to its PAMT entry (determines PAZ location, compression, encryption)
3. Copies the vanilla PAZ, repacks each file into the copy
4. Returns the modified PAZ directory for standard CDUMM delta import
"""

import json
import logging
import shutil
from pathlib import Path

import struct

from cdumm.archive.paz_parse import parse_pamt, PazEntry
from cdumm.archive.paz_repack import repack_entry_bytes, _save_timestamps

logger = logging.getLogger(__name__)


def detect_crimson_browser(path: Path) -> dict | None:
    """Check if path contains a Crimson Browser format mod.

    Args:
        path: directory to check (extracted zip or dropped folder)

    Returns:
        Parsed manifest dict if CB format, None otherwise.
    """
    # Check root and one level deep
    for candidate in [path / "manifest.json", *path.glob("*/manifest.json")]:
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            if isinstance(manifest, dict) and manifest.get("format", "").startswith("crimson_browser_mod"):
                manifest["_manifest_path"] = candidate
                manifest["_base_dir"] = candidate.parent
                return manifest
        except Exception:
            continue
    return None


def convert_to_paz_mod(manifest: dict, game_dir: Path, work_dir: Path) -> Path | None:
    """Convert a Crimson Browser mod to a standard PAZ mod directory.

    Copies vanilla PAZ files, repacks each loose file into the copy,
    and returns the work_dir containing the modified PAZ/PAMT files
    ready for standard CDUMM delta import.

    Args:
        manifest: parsed manifest dict (from detect_crimson_browser)
        game_dir: path to game installation root
        work_dir: temporary directory for output

    Returns:
        Path to directory containing modified PAZ files, or None on failure.
    """
    base_dir = manifest["_base_dir"]
    files_dir_name = manifest.get("files_dir", "files")
    files_dir = base_dir / files_dir_name

    if not files_dir.exists():
        logger.error("CB mod files_dir not found: %s", files_dir)
        return None

    # Collect all loose files, grouped by PAZ directory number
    # Structure: files/NNNN/path/to/file.ext -> maps to directory NNNN
    files_by_dir: dict[str, list[tuple[str, Path]]] = {}

    for f in files_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(files_dir)
        parts = rel.parts
        if len(parts) < 2 or not parts[0].isdigit():
            logger.warning("CB mod: skipping file with unexpected path: %s", rel)
            continue

        dir_num = parts[0]
        # The file path within the PAZ archive (everything after the dir number)
        inner_path = "/".join(parts[1:])
        files_by_dir.setdefault(dir_num, []).append((inner_path, f))

    if not files_by_dir:
        logger.error("CB mod: no files found in %s", files_dir)
        return None

    logger.info("CB mod '%s': %d files across directories %s",
                manifest.get("id", "unknown"),
                sum(len(v) for v in files_by_dir.values()),
                list(files_by_dir.keys()))

    # For each directory, parse PAMT, find entries, repack into PAZ copy
    for dir_num, file_list in files_by_dir.items():
        dir_name = f"{int(dir_num):04d}"
        game_paz_dir = game_dir / dir_name
        pamt_path = game_paz_dir / "0.pamt"

        if not pamt_path.exists():
            logger.error("CB mod: vanilla PAMT not found: %s", pamt_path)
            return None

        # Parse PAMT to find all entries
        entries = parse_pamt(str(pamt_path), paz_dir=str(game_paz_dir))
        entry_map: dict[str, PazEntry] = {}
        for e in entries:
            # Normalize: strip leading folder prefix for matching
            # PAMT paths look like "ui/xml/gamemain/play/minimaphudview2.css"
            # or "ui/cdcommon_font_eng.css" etc.
            entry_map[e.path.lower()] = e

        # Track which PAZ files need copying
        paz_copies: dict[str, Path] = {}  # paz_file_path -> work_dir copy

        # Also build a basename lookup for fallback matching
        # PAMT flattens paths (e.g., "ui/minimaphudview2.css") while mods
        # may use full filesystem paths ("ui/xml/gamemain/play/minimaphudview2.css")
        basename_map: dict[str, PazEntry] = {}
        for e in entries:
            bname = e.path.rsplit("/", 1)[-1].lower()
            # Only use basename if it's unique — ambiguous names skip this fallback
            if bname in basename_map:
                basename_map[bname] = None  # mark as ambiguous
            else:
                basename_map[bname] = e

        pamt_updates: list[tuple[PazEntry, int]] = []  # (entry, new_comp_size)

        for inner_path, source_file in file_list:
            # Find matching PAMT entry
            entry = entry_map.get(inner_path.lower())
            if entry is None:
                # Try with directory prefix (some PAMTs include a root prefix)
                for key, e in entry_map.items():
                    if key.endswith("/" + inner_path.lower()) or key == inner_path.lower():
                        entry = e
                        break
            if entry is None:
                # Fallback: match by filename only (PAMT flattens directory structure)
                bname = inner_path.rsplit("/", 1)[-1].lower()
                entry = basename_map.get(bname)

            if entry is None:
                logger.warning("CB mod: no PAMT entry for '%s' in dir %s, skipping",
                               inner_path, dir_name)
                continue

            # Ensure we have a copy of the PAZ file in work_dir
            paz_src = Path(entry.paz_file)
            if str(paz_src) not in paz_copies:
                paz_dst = work_dir / dir_name / paz_src.name
                paz_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(paz_src, paz_dst)
                paz_copies[str(paz_src)] = paz_dst
                logger.info("Copied PAZ: %s -> %s", paz_src.name, paz_dst)

            paz_dst = paz_copies[str(paz_src)]

            # Read the modified file
            plaintext = source_file.read_bytes()

            # Repack into the PAZ copy (allow size change for larger/smaller files)
            try:
                payload, actual_comp = repack_entry_bytes(
                    plaintext, entry, allow_size_change=True)

                # Write payload into the PAZ copy at the correct offset
                restore_ts = _save_timestamps(str(paz_dst))
                with open(paz_dst, 'r+b') as fh:
                    fh.seek(entry.offset)
                    fh.write(payload)
                restore_ts()

                # Track PAMT updates needed
                if actual_comp != entry.comp_size:
                    pamt_updates.append((entry, actual_comp))

                logger.info("Repacked: %s (comp=%d->%d, orig=%d, enc=%s)",
                            inner_path, entry.comp_size, actual_comp,
                            entry.orig_size, entry.encrypted)
            except Exception as e:
                logger.error("Failed to repack '%s': %s", inner_path, e, exc_info=True)
                return None

        # Copy PAMT and apply any comp_size updates
        pamt_dst = work_dir / dir_name / "0.pamt"
        if not pamt_dst.exists():
            pamt_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pamt_path, pamt_dst)

        if pamt_updates:
            _update_pamt_entries(pamt_dst, pamt_updates)

    return work_dir


def _update_pamt_entries(pamt_path: Path, updates: list[tuple[PazEntry, int]]) -> None:
    """Update comp_size fields in a PAMT file for entries whose size changed.

    PAMT file records are 20 bytes: node_ref(4) + offset(4) + comp_size(4) + orig_size(4) + flags(4).
    We find each record by matching (offset, old_comp_size, orig_size, flags) and patch comp_size.
    """
    data = bytearray(pamt_path.read_bytes())

    for entry, new_comp_size in updates:
        # Search for the 16-byte pattern: offset + comp_size + orig_size + flags
        search = struct.pack('<IIII', entry.offset, entry.comp_size, entry.orig_size, entry.flags)
        idx = data.find(search)
        if idx < 0:
            logger.warning("Could not find PAMT record for %s", entry.path)
            continue
        # comp_size is at idx + 4 (after the offset field)
        struct.pack_into('<I', data, idx + 4, new_comp_size)
        logger.info("Updated PAMT comp_size for %s: %d -> %d",
                     entry.path, entry.comp_size, new_comp_size)

    # Recompute PAMT hash
    from cdumm.archive.hashlittle import compute_pamt_hash
    new_hash = compute_pamt_hash(bytes(data))
    struct.pack_into('<I', data, 0, new_hash)

    pamt_path.write_bytes(bytes(data))
