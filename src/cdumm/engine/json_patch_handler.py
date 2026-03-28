"""JSON byte-patch mod format handler.

Detects mods distributed as JSON files containing byte-level patches
against specific game files inside PAZ archives.

Format:
    {
        "name": "...",
        "version": "...",
        "description": "...",
        "author": "...",
        "patches": [
            {
                "game_file": "gamedata/iteminfo.pabgb",
                "changes": [
                    {"offset": 24, "label": "...", "original": "64000000", "patched": "3f420f00"},
                    ...
                ]
            }
        ]
    }

Offsets are into the DECOMPRESSED file content. The handler:
1. Finds each target file in the game's PAMT index
2. Extracts and decompresses it from the PAZ
3. Applies all byte patches
4. Recompresses and repacks into a PAZ copy
5. Returns modified PAZ files for standard CDUMM delta import
"""

import json
import logging
import os
import shutil
import struct
from pathlib import Path

import lz4.block

from cdumm.archive.paz_parse import parse_pamt, PazEntry
from cdumm.archive.paz_crypto import decrypt, encrypt, lz4_decompress, lz4_compress
from cdumm.archive.paz_repack import repack_entry_bytes, _save_timestamps

logger = logging.getLogger(__name__)


def detect_json_patch(path: Path) -> dict | None:
    """Check if path contains a JSON byte-patch mod.

    Checks the path itself (if a .json file) or searches one level deep
    in a directory.

    Returns parsed JSON dict if valid, None otherwise.
    """
    candidates = []
    if path.is_file() and path.suffix.lower() == ".json":
        candidates = [path]
    elif path.is_dir():
        candidates = list(path.glob("*.json"))
        if not candidates:
            candidates = list(path.glob("*/*.json"))

    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict)
                    and "patches" in data
                    and isinstance(data["patches"], list)
                    and len(data["patches"]) > 0
                    and "game_file" in data["patches"][0]
                    and "changes" in data["patches"][0]):
                data["_json_path"] = candidate
                return data
        except Exception:
            continue
    return None


def _extract_from_paz(entry: PazEntry) -> bytes:
    """Read a file entry from its PAZ archive and return decompressed plaintext."""
    with open(entry.paz_file, "rb") as f:
        f.seek(entry.offset)
        raw = f.read(entry.comp_size)

    # Decrypt if needed (XML files)
    if entry.encrypted:
        raw = decrypt(raw, os.path.basename(entry.path))

    # Decompress if needed
    if entry.compressed and entry.compression_type == 2:
        raw = lz4_decompress(raw, entry.orig_size)

    return raw


def _apply_byte_patches(data: bytearray, changes: list[dict]) -> int:
    """Apply byte patches to decompressed file data.

    Returns number of patches applied.
    """
    applied = 0
    for change in changes:
        offset = change["offset"]
        patched_hex = change["patched"]
        patched_bytes = bytes.fromhex(patched_hex)

        if offset + len(patched_bytes) > len(data):
            logger.warning("Patch at offset %d exceeds file size %d, skipping",
                           offset, len(data))
            continue

        # Optionally verify original bytes match
        if "original" in change:
            original_bytes = bytes.fromhex(change["original"])
            actual = data[offset:offset + len(original_bytes)]
            if actual != original_bytes:
                logger.debug("Original mismatch at %d: expected %s, got %s (applying anyway)",
                             offset, change["original"], actual.hex())

        data[offset:offset + len(patched_bytes)] = patched_bytes
        applied += 1

    return applied


def convert_json_patch_to_paz(patch_data: dict, game_dir: Path, work_dir: Path) -> Path | None:
    """Convert a JSON patch mod to modified PAZ files.

    IMPORTANT: Always uses VANILLA files as the base, not the current game
    files which may have other mods applied (shifted offsets, changed sizes).

    For each patched game_file:
    1. Find it in vanilla PAMT, extract from vanilla PAZ
    2. Apply byte patches to decompressed content
    3. Recompress/encrypt and write to vanilla PAZ copy in work_dir

    Returns work_dir containing modified PAZ files, or None on failure.
    """
    patches = patch_data["patches"]
    mod_name = patch_data.get("name", "unknown")

    # Use vanilla backups if available, fall back to game dir
    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir
        logger.warning("No vanilla backup dir, using game dir (may have shifted offsets)")
    else:
        logger.info("Using vanilla backups for JSON patch base")

    logger.info("JSON patch mod '%s': %d file(s) to patch", mod_name, len(patches))

    entry_cache: dict[str, PazEntry] = {}

    for patch in patches:
        game_file = patch["game_file"]
        changes = patch["changes"]

        if not changes:
            continue

        # Find the PAMT entry using VANILLA PAMT (correct offsets)
        if game_file.lower() not in entry_cache:
            entry = _find_pamt_entry(game_file, vanilla_dir)
            if entry is None:
                # Fallback to game dir if vanilla doesn't have this directory
                entry = _find_pamt_entry(game_file, game_dir)
            if entry:
                entry_cache[game_file.lower()] = entry

        entry = entry_cache.get(game_file.lower())
        if entry is None:
            logger.error("Could not find '%s' in any PAMT index", game_file)
            return None

        logger.info("Patching %s: %d changes (paz=%s, comp=%d, orig=%d)",
                     game_file, len(changes),
                     os.path.basename(entry.paz_file),
                     entry.comp_size, entry.orig_size)

        # Extract and decompress the file
        try:
            plaintext = _extract_from_paz(entry)
        except Exception as e:
            logger.error("Failed to extract %s: %s", game_file, e, exc_info=True)
            return None

        # Apply byte patches
        modified = bytearray(plaintext)
        applied = _apply_byte_patches(modified, changes)
        logger.info("Applied %d/%d patches to %s", applied, len(changes), game_file)

        if bytes(modified) == plaintext:
            logger.info("No actual changes after patching %s, skipping", game_file)
            continue

        # Repack: compress + encrypt back to PAZ format
        # Use allow_size_change=True because byte patches change the LZ4
        # compression ratio slightly — we'll update PAMT to match.
        try:
            payload, actual_comp = repack_entry_bytes(
                bytes(modified), entry, allow_size_change=True)
        except Exception as e:
            logger.error("Failed to repack %s: %s", game_file, e, exc_info=True)
            return None

        # Copy the PAZ file and write the patched payload
        paz_src = Path(entry.paz_file)
        dir_name = paz_src.parent.name
        paz_dst = work_dir / dir_name / paz_src.name
        if not paz_dst.exists():
            paz_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(paz_src, paz_dst)
            logger.info("Copied PAZ: %s -> %s", paz_src.name, paz_dst)

        # Write patched payload at the correct offset
        restore_ts = _save_timestamps(str(paz_dst))
        with open(paz_dst, "r+b") as fh:
            fh.seek(entry.offset)
            fh.write(payload)
        restore_ts()

        # Copy PAMT and update comp_size if it changed
        pamt_src = paz_src.parent / "0.pamt"
        pamt_dst = work_dir / dir_name / "0.pamt"
        if pamt_src.exists() and not pamt_dst.exists():
            pamt_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pamt_src, pamt_dst)

        if actual_comp != entry.comp_size and pamt_dst.exists():
            _update_pamt_comp_size(pamt_dst, entry, actual_comp)
            logger.info("Updated PAMT comp_size for %s: %d -> %d",
                        game_file, entry.comp_size, actual_comp)

    return work_dir


def _update_pamt_comp_size(pamt_path: Path, entry: PazEntry, new_comp_size: int) -> None:
    """Update a file record's comp_size in a PAMT binary file.

    PAMT file records are 20 bytes: node_ref(4) + offset(4) + comp_size(4) + orig_size(4) + flags(4).
    We find the record matching entry's (offset, comp_size, orig_size, flags) and patch comp_size.
    """
    data = bytearray(pamt_path.read_bytes())

    # Search for the 20-byte record matching this entry
    target = struct.pack('<IIIII', 0, entry.offset, entry.comp_size, entry.orig_size, entry.flags)
    # We don't know node_ref, so search for the last 16 bytes (offset+comp+orig+flags)
    search = struct.pack('<IIII', entry.offset, entry.comp_size, entry.orig_size, entry.flags)

    pos = 0
    found = False
    while pos <= len(data) - 20:
        idx = data.find(search, pos)
        if idx < 0:
            break
        # The record starts 4 bytes before (node_ref precedes offset)
        record_start = idx - 4
        if record_start >= 0:
            # Verify this looks like a valid record position
            comp_offset = record_start + 8  # offset of comp_size within record
            struct.pack_into('<I', data, comp_offset, new_comp_size)
            found = True
            logger.debug("Patched PAMT comp_size at byte %d: %d -> %d",
                         comp_offset, entry.comp_size, new_comp_size)
            break
        pos = idx + 1

    if not found:
        logger.warning("Could not find PAMT record for %s (offset=0x%X, comp=%d)",
                       entry.path, entry.offset, entry.comp_size)
        return

    # Recompute PAMT hash (first 4 bytes = hashlittle(data[12:], 0xC5EDE))
    from cdumm.archive.hashlittle import compute_pamt_hash
    new_hash = compute_pamt_hash(bytes(data))
    struct.pack_into('<I', data, 0, new_hash)

    pamt_path.write_bytes(bytes(data))


def _find_pamt_entry(game_file: str, game_dir: Path) -> PazEntry | None:
    """Search all PAMT indices for a specific game file path."""
    game_file_lower = game_file.lower().replace("\\", "/")

    for d in sorted(game_dir.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue
        pamt = d / "0.pamt"
        if not pamt.exists():
            continue
        try:
            entries = parse_pamt(str(pamt), paz_dir=str(d))
            for e in entries:
                if e.path.lower().replace("\\", "/") == game_file_lower:
                    return e
                # Also try matching just the suffix
                if e.path.lower().replace("\\", "/").endswith("/" + game_file_lower):
                    return e
        except Exception:
            continue
    return None
