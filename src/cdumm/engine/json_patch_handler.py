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

    basename = os.path.basename(entry.path)

    if entry.compressed and entry.compression_type == 2:
        # Try decompress first (no decrypt). If the PAMT encrypted flag
        # is wrong, this will fail and we fall back to decrypt+decompress.
        try:
            return lz4_decompress(raw, entry.orig_size)
        except Exception:
            # Decryption needed — decrypt then decompress
            decrypted = decrypt(raw, basename)
            return lz4_decompress(decrypted, entry.orig_size)

    # Not compressed — try raw first, then decrypted
    if entry.encrypted:
        raw = decrypt(raw, basename)

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
            raise RuntimeError(f"Failed to extract {game_file}: {e}") from e

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
            payload, actual_comp, actual_orig = repack_entry_bytes(
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

        new_offset = entry.offset
        if actual_comp > entry.comp_size:
            # Data doesn't fit in the original slot — append to end of PAZ
            # and update offset in PAMT
            restore_ts = _save_timestamps(str(paz_dst))
            with open(paz_dst, "r+b") as fh:
                fh.seek(0, 2)  # seek to end
                new_offset = fh.tell()
                fh.write(payload)
            restore_ts()
            logger.info("Appended %s to end of PAZ at offset %d (was %d, grew %d->%d)",
                        game_file, new_offset, entry.offset, entry.comp_size, actual_comp)
        else:
            # Write patched payload at the original offset
            restore_ts = _save_timestamps(str(paz_dst))
            with open(paz_dst, "r+b") as fh:
                fh.seek(entry.offset)
                fh.write(payload)
            restore_ts()

        # Copy PAMT and update comp_size/offset if they changed
        pamt_src = paz_src.parent / "0.pamt"
        pamt_dst = work_dir / dir_name / "0.pamt"
        if pamt_src.exists() and not pamt_dst.exists():
            pamt_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pamt_src, pamt_dst)

        if (actual_comp != entry.comp_size or new_offset != entry.offset
                or actual_orig != entry.orig_size) and pamt_dst.exists():
            # If we appended to PAZ, pass the new file size so PAMT PAZ table is updated
            new_paz_size = None
            if new_offset != entry.offset:
                new_paz_size = new_offset + actual_comp  # end of appended data = new PAZ size
            _update_pamt_record(pamt_dst, entry, actual_comp, new_offset,
                                new_paz_size=new_paz_size)
            logger.info("Updated PAMT for %s: comp %d->%d, offset %d->%d%s",
                        game_file, entry.comp_size, actual_comp,
                        entry.offset, new_offset,
                        f", paz_size={new_paz_size}" if new_paz_size else "")

    return work_dir


def _update_pamt_record(pamt_path: Path, entry: PazEntry,
                        new_comp_size: int, new_offset: int,
                        new_paz_size: int | None = None) -> None:
    """Update a file record's comp_size and/or offset in a PAMT binary file.

    PAMT file records are 20 bytes: node_ref(4) + offset(4) + comp_size(4) + orig_size(4) + flags(4).
    Also updates the PAZ size table if new_paz_size is provided.
    """
    data = bytearray(pamt_path.read_bytes())

    # Update PAZ size table if the PAZ file grew (data appended to end)
    if new_paz_size is not None:
        paz_index = entry.paz_index
        paz_count = struct.unpack_from('<I', data, 4)[0]
        if paz_index < paz_count:
            # PAZ table starts at offset 16: [hash(4) + size(4)] per entry,
            # with 4-byte separator between entries (except after the last)
            table_off = 16
            for i in range(paz_index):
                table_off += 8  # hash + size
                if i < paz_count - 1:
                    table_off += 4  # separator
            # table_off now points to hash(4) + size(4) for this PAZ
            size_off = table_off + 4  # skip hash, point to size
            old_size = struct.unpack_from('<I', data, size_off)[0]
            struct.pack_into('<I', data, size_off, new_paz_size)
            logger.debug("Updated PAMT PAZ[%d] size: %d -> %d",
                         paz_index, old_size, new_paz_size)

    # Search for the 16-byte pattern: offset + comp_size + orig_size + flags
    search = struct.pack('<IIII', entry.offset, entry.comp_size, entry.orig_size, entry.flags)

    pos = 0
    found = False
    while pos <= len(data) - 20:
        idx = data.find(search, pos)
        if idx < 0:
            break
        record_start = idx - 4
        if record_start >= 0:
            struct.pack_into('<I', data, idx, new_offset)
            struct.pack_into('<I', data, idx + 4, new_comp_size)
            found = True
            logger.debug("Patched PAMT record at byte %d: offset %d->%d, comp %d->%d",
                         record_start, entry.offset, new_offset,
                         entry.comp_size, new_comp_size)
            break
        pos = idx + 1

    if not found:
        logger.warning("Could not find PAMT record for %s (offset=0x%X, comp=%d)",
                       entry.path, entry.offset, entry.comp_size)
        return

    # Recompute PAMT hash
    from cdumm.archive.hashlittle import compute_pamt_hash
    new_hash = compute_pamt_hash(bytes(data))
    struct.pack_into('<I', data, 0, new_hash)

    pamt_path.write_bytes(bytes(data))


def _find_pamt_entry(game_file: str, game_dir: Path) -> PazEntry | None:
    """Search all PAMT indices for a specific game file path.

    Tries exact match, suffix match, and basename match (PAMT flattens
    directory structure, so mod paths may be deeper than PAMT paths).
    """
    game_file_lower = game_file.lower().replace("\\", "/")
    game_basename = game_file_lower.rsplit("/", 1)[-1]

    basename_match = None

    for d in sorted(game_dir.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue
        pamt = d / "0.pamt"
        if not pamt.exists():
            continue
        try:
            entries = parse_pamt(str(pamt), paz_dir=str(d))
            for e in entries:
                ep = e.path.lower().replace("\\", "/")
                # Exact match
                if ep == game_file_lower:
                    return e
                # PAMT path is suffix of game_file (mod uses deeper path)
                if game_file_lower.endswith("/" + ep) or game_file_lower.endswith(ep):
                    return e
                # game_file is suffix of PAMT path
                if ep.endswith("/" + game_file_lower):
                    return e
                # Basename match (last resort — only if unique)
                if ep.rsplit("/", 1)[-1] == game_basename:
                    if basename_match is None:
                        basename_match = e
                    else:
                        basename_match = False  # ambiguous
        except Exception:
            continue

    if basename_match and basename_match is not False:
        logger.info("Matched '%s' to '%s' by basename", game_file, basename_match.path)
        return basename_match
    return None
