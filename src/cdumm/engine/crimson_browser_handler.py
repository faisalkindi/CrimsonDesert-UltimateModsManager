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

    # Files without numbered directory prefix need PAMT lookup
    unresolved: list[tuple[str, Path]] = []

    for f in files_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(files_dir)
        parts = rel.parts
        if len(parts) >= 2 and parts[0].isdigit():
            dir_num = parts[0]
            inner_path = "/".join(parts[1:])
            files_by_dir.setdefault(dir_num, []).append((inner_path, f))
        else:
            # No numbered dir — use full relative path for PAMT lookup
            inner_path = "/".join(parts)
            unresolved.append((inner_path, f))

    # Resolve unresolved files by searching PAMTs for matching filenames
    if unresolved:
        dir_map = _resolve_files_to_directories(unresolved, game_dir)
        for dir_num, file_list in dir_map.items():
            files_by_dir.setdefault(dir_num, []).extend(file_list)

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

            # Detect if the original PAZ entry is actually encrypted
            # even when entry.encrypted says False (heuristic misses non-XML files)
            if not entry.encrypted and entry.compressed and entry.compression_type == 2:
                try:
                    with open(paz_src, 'rb') as probe_f:
                        probe_f.seek(entry.offset)
                        probe_raw = probe_f.read(entry.comp_size)
                    from cdumm.archive.paz_crypto import lz4_decompress
                    lz4_decompress(probe_raw, entry.orig_size)
                except Exception:
                    # Decompress failed → file is encrypted
                    entry._encrypted_override = True
                    logger.info("Detected encryption for %s (flag was False)", entry.path)

            # Repack into the PAZ copy (allow size change for larger/smaller files)
            try:
                payload, actual_comp, actual_orig = repack_entry_bytes(
                    plaintext, entry, allow_size_change=True)

                new_offset = entry.offset
                if actual_comp > entry.comp_size:
                    # Doesn't fit — append to end of PAZ
                    restore_ts = _save_timestamps(str(paz_dst))
                    with open(paz_dst, 'r+b') as fh:
                        fh.seek(0, 2)
                        new_offset = fh.tell()
                        fh.write(payload)
                    restore_ts()
                    logger.info("Appended %s to end of PAZ (offset %d->%d)",
                                inner_path, entry.offset, new_offset)
                else:
                    # Write at original offset
                    restore_ts = _save_timestamps(str(paz_dst))
                    with open(paz_dst, 'r+b') as fh:
                        fh.seek(entry.offset)
                        fh.write(payload)
                    restore_ts()

                # Track PAMT updates needed
                new_paz_size = None
                if new_offset != entry.offset:
                    new_paz_size = new_offset + actual_comp
                if (actual_comp != entry.comp_size or new_offset != entry.offset
                        or actual_orig != entry.orig_size):
                    pamt_updates.append((entry, actual_comp, new_offset,
                                         new_paz_size, actual_orig))

                logger.info("Repacked: %s (comp=%d->%d, orig=%d->%d, enc=%s)",
                            inner_path, entry.comp_size, actual_comp,
                            entry.orig_size, actual_orig, entry.encrypted)
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


def _resolve_files_to_directories(
    files: list[tuple[str, Path]], game_dir: Path
) -> dict[str, list[tuple[str, Path]]]:
    """Find which PAZ directory each file belongs to by searching all PAMTs.

    For CB mods that use virtual paths (character/hair.xml) without numbered
    directory prefixes, we search all PAMTs by basename to find the match.

    When the same file exists in multiple PAZ directories (e.g., 0009 and 0039),
    we prefer the highest-numbered directory — game updates add new directories
    with correct slot sizes for the latest version.

    Returns {dir_num: [(inner_path, source_file), ...]}
    """
    result: dict[str, list[tuple[str, Path]]] = {}
    if not files:
        return result

    # Build a lookup: basename -> [(inner_path, source_file)]
    by_basename: dict[str, list[tuple[str, Path]]] = {}
    for inner_path, source in files:
        bname = inner_path.rsplit("/", 1)[-1].lower()
        by_basename.setdefault(bname, []).append((inner_path, source))

    # Collect ALL matches across all directories, keyed by basename
    # {basename: [(dir_name, inner_path, source_file), ...]}
    all_matches: dict[str, list[tuple[str, str, Path]]] = {}

    for d in sorted(game_dir.iterdir()):
        if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
            continue
        pamt = d / "0.pamt"
        if not pamt.exists():
            continue
        try:
            entries = parse_pamt(str(pamt), paz_dir=str(d))
            for e in entries:
                bname = e.path.rsplit("/", 1)[-1].lower()
                if bname in by_basename:
                    for inner_path, source in by_basename[bname]:
                        all_matches.setdefault(bname, []).append(
                            (d.name, inner_path, source))
        except Exception:
            continue

    # For each basename, pick the highest-numbered directory
    for bname, candidates in all_matches.items():
        # Sort by directory number descending, pick highest
        candidates.sort(key=lambda x: int(x[0]), reverse=True)
        best_dir = candidates[0][0]
        for dir_name, inner_path, source in candidates:
            if dir_name == best_dir:
                result.setdefault(dir_name, []).append((inner_path, source))
                logger.info("CB resolved %s -> dir %s (preferred highest of %s)",
                            inner_path, dir_name,
                            sorted(set(c[0] for c in candidates)))
        del by_basename[bname]

    for bname, remaining in by_basename.items():
        for inner_path, _ in remaining:
            logger.warning("CB mod: could not resolve %s to any PAZ directory", inner_path)

    return result


def _update_pamt_entries(pamt_path: Path, updates: list[tuple[PazEntry, int, int, int | None, int]]) -> None:
    """Update comp_size, offset, orig_size, and PAZ size fields in a PAMT file.

    PAMT file records are 20 bytes: node_ref(4) + offset(4) + comp_size(4) + orig_size(4) + flags(4).
    """
    data = bytearray(pamt_path.read_bytes())

    for entry, new_comp_size, new_offset, new_paz_size, new_orig_size in updates:
        # Update PAZ size table if PAZ grew
        if new_paz_size is not None:
            paz_index = entry.paz_index
            paz_count = struct.unpack_from('<I', data, 4)[0]
            if paz_index < paz_count:
                table_off = 16
                for i in range(paz_index):
                    table_off += 8
                    if i < paz_count - 1:
                        table_off += 4
                size_off = table_off + 4
                struct.pack_into('<I', data, size_off, new_paz_size)
                logger.info("Updated PAMT PAZ[%d] size to %d", paz_index, new_paz_size)

        search = struct.pack('<IIII', entry.offset, entry.comp_size, entry.orig_size, entry.flags)
        idx = data.find(search)
        if idx < 0:
            logger.warning("Could not find PAMT record for %s", entry.path)
            continue
        struct.pack_into('<I', data, idx, new_offset)
        struct.pack_into('<I', data, idx + 4, new_comp_size)
        if new_orig_size != entry.orig_size:
            struct.pack_into('<I', data, idx + 8, new_orig_size)
        logger.info("Updated PAMT for %s: offset %d->%d, comp %d->%d, orig %d->%d",
                     entry.path, entry.offset, new_offset,
                     entry.comp_size, new_comp_size,
                     entry.orig_size, new_orig_size)

    # Recompute PAMT hash
    from cdumm.archive.hashlittle import compute_pamt_hash
    new_hash = compute_pamt_hash(bytes(data))
    struct.pack_into('<I', data, 0, new_hash)

    pamt_path.write_bytes(bytes(data))
