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

Signature-based dynamic offsets (optional):
    If a patch entry has a "signature" field, the handler searches the
    decompressed file for that hex byte pattern. Change offsets are then
    relative to the END of the signature match instead of absolute.
    This survives game updates that shift data around.

    {
        "game_file": "gamedata/inventory.pabgb",
        "signature": "090000004368617261637465720001",
        "changes": [
            {"offset": 0, "label": "...", "original": "3200", "patched": "b400"},
            {"offset": 2, "label": "...", "original": "f000", "patched": "bc02"}
        ]
    }

Offsets are into the DECOMPRESSED file content. The handler:
1. Finds each target file in the game's PAMT index
2. Extracts and decompresses it from the PAZ
3. Applies all byte patches (absolute or signature-relative)
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


def decompress_entry(raw: bytes, entry: PazEntry) -> bytes:
    """Decompress raw PAZ entry bytes based on the entry's compression type.

    Handles type 0x01 (DDS split: 128-byte header + LZ4 body),
    type 0x02 (fully LZ4 compressed), and uncompressed entries.
    Detects encryption automatically and corrects entry._encrypted_override.
    """
    basename = os.path.basename(entry.path)

    if entry.compression_type == 1:
        DDS_HEADER_SIZE = 128
        header = raw[:DDS_HEADER_SIZE]
        compressed_body = raw[DDS_HEADER_SIZE:]
        body_orig_size = entry.orig_size - DDS_HEADER_SIZE

        # The DDS header may store the inner LZ4 compressed size at offset 32.
        # When comp_size == orig_size (padded DDS), the full body includes
        # LZ4 data + zero padding. Use the header field to read only the
        # actual LZ4 bytes, falling back to the full body if not available.
        inner_comp_size = 0
        if len(header) >= 36:
            inner_comp_size = struct.unpack_from("<I", header, 32)[0]
        if inner_comp_size > 0 and inner_comp_size < len(compressed_body):
            lz4_input = compressed_body[:inner_comp_size]
        else:
            lz4_input = compressed_body

        try:
            body = lz4_decompress(lz4_input, body_orig_size)
        except Exception:
            # Retry with full body (vanilla entries without header field)
            if lz4_input is not compressed_body:
                try:
                    body = lz4_decompress(compressed_body, body_orig_size)
                except Exception:
                    try:
                        decrypted = decrypt(compressed_body, basename)
                        body = lz4_decompress(decrypted, body_orig_size)
                        if not entry._encrypted_override:
                            logger.info("Corrected encrypted flag for %s (DDS split, actually encrypted)",
                                        entry.path)
                            entry._encrypted_override = True
                    except Exception:
                        # All decompression failed — DX10 multi-mip raw passthrough
                        logger.info("DDS %s: returning raw (DX10 multi-mip)", entry.path)
                        return raw
            else:
                try:
                    decrypted = decrypt(compressed_body, basename)
                    body = lz4_decompress(decrypted, body_orig_size)
                    if not entry._encrypted_override:
                        logger.info("Corrected encrypted flag for %s (DDS split, actually encrypted)",
                                    entry.path)
                        entry._encrypted_override = True
                except Exception:
                    # All decompression failed — DX10 multi-mip raw passthrough
                    logger.info("DDS %s: returning raw (DX10 multi-mip)", entry.path)
                    return raw
        return header + body

    if entry.compressed and entry.compression_type == 2:
        try:
            return lz4_decompress(raw, entry.orig_size)
        except Exception:
            decrypted = decrypt(raw, basename)
            result = lz4_decompress(decrypted, entry.orig_size)
            if not entry._encrypted_override:
                logger.info("Corrected encrypted flag for %s (was False, actually encrypted)",
                            entry.path)
                entry._encrypted_override = True
            return result

    if entry.encrypted:
        return decrypt(raw, basename)

    return raw


def _extract_from_paz(entry: PazEntry, paz_path: str | None = None) -> bytes:
    """Read a file entry from its PAZ archive and return decompressed plaintext.

    Args:
        entry: PAMT entry describing the file location and format
        paz_path: override PAZ file path (default: entry.paz_file).
                  Use when reading from a mod's PAZ copy instead of the game file.

    If the PAMT encrypted flag is wrong (file is actually encrypted),
    corrects entry.encrypted so repack_entry_bytes will re-encrypt.

    Handles compression type 0x01 (128-byte DDS header + LZ4 body)
    and type 0x02 (fully LZ4 compressed).
    """
    with open(paz_path or entry.paz_file, "rb") as f:
        f.seek(entry.offset)
        raw = f.read(entry.comp_size)
    return decompress_entry(raw, entry)


def _apply_byte_patches(data: bytearray, changes: list[dict],
                        signature: str | None = None) -> int:
    """Apply byte patches to decompressed file data.

    If signature is provided, find it in data and treat change offsets
    as relative to the end of the signature match. Otherwise offsets
    are absolute.

    Returns number of patches applied.
    """
    base_offset = 0
    if signature:
        sig_bytes = bytes.fromhex(signature)
        idx = bytes(data).find(sig_bytes)
        if idx < 0:
            logger.error("Signature %s not found in data (%d bytes)",
                         signature[:40] + "..." if len(signature) > 40 else signature,
                         len(data))
            return 0
        base_offset = idx + len(sig_bytes)
        logger.info("Signature found at offset %d, patches relative to %d",
                     idx, base_offset)

    applied = 0
    for change in changes:
        offset = base_offset + change["offset"]
        patched_hex = change["patched"]
        patched_bytes = bytes.fromhex(patched_hex)

        if offset + len(patched_bytes) > len(data):
            logger.warning("Patch at offset %d exceeds file size %d, skipping",
                           offset, len(data))
            continue

        # Verify original bytes match — skip patch if they don't.
        # This prevents silent corruption when an older CDUMM version
        # ignores the "signature" field and treats offsets as absolute.
        # Exception: if the patched bytes already match, the mod is already
        # applied (e.g. reimporting on modded game files). Count it as applied.
        if "original" in change:
            original_bytes = bytes.fromhex(change["original"])
            actual = data[offset:offset + len(original_bytes)]
            if actual != original_bytes:
                # Check if already patched
                actual_at_patch = data[offset:offset + len(patched_bytes)]
                if actual_at_patch == patched_bytes:
                    logger.debug("Already patched at %d, keeping as-is", offset)
                    applied += 1
                    continue
                logger.warning("Original mismatch at %d: expected %s, got %s — skipping patch",
                               offset, change["original"], actual.hex())
                continue

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

        # Extract and decompress the file.
        # If the vanilla PAZ backup doesn't exist, fall back to game dir
        # AND re-lookup the entry using the game PAMT (correct offsets for
        # the current game PAZ state, which may have other mods applied).
        try:
            if not os.path.exists(entry.paz_file):
                game_entry = _find_pamt_entry(game_file, game_dir)
                if game_entry:
                    logger.info("Vanilla PAZ not found, using game dir for %s", game_file)
                    entry = game_entry
                    entry_cache[game_file.lower()] = entry
            plaintext = _extract_from_paz(entry)
        except Exception as e:
            # If extraction fails (e.g., offsets wrong from modded PAZ),
            # try game dir with fresh PAMT lookup as last resort
            try:
                game_entry = _find_pamt_entry(game_file, game_dir)
                if game_entry:
                    logger.info("Retrying extraction from game dir for %s", game_file)
                    plaintext = _extract_from_paz(game_entry)
                    entry = game_entry
                    entry_cache[game_file.lower()] = entry
                else:
                    raise
            except Exception:
                logger.error("Failed to extract %s: %s", game_file, e, exc_info=True)
                raise RuntimeError(f"Failed to extract {game_file}: {e}") from e

        # Apply byte patches
        modified = bytearray(plaintext)
        signature = patch.get("signature")
        applied = _apply_byte_patches(modified, changes, signature=signature)
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


def import_json_as_entr(patch_data: dict, game_dir: Path, db, deltas_dir: Path,
                        mod_name: str, existing_mod_id: int | None = None,
                        modinfo: dict | None = None) -> dict | None:
    """Import a JSON patch mod as ENTR deltas instead of FULL_COPY PAZ deltas.

    This produces entry-level deltas that compose correctly when multiple
    mods modify different entries in the same PAZ file.

    Returns a result dict with mod_id and changed_files, or None on failure.
    """
    from cdumm.engine.delta_engine import save_entry_delta

    patches = patch_data["patches"]
    logger.info("import_json_as_entr: starting '%s' (%d patches)", mod_name, len(patches))

    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir
        logger.info("import_json_as_entr: no vanilla dir, using game dir")

    # Create mod entry in DB
    priority = db.connection.execute(
        "SELECT COALESCE(MAX(priority), 0) + 1 FROM mods").fetchone()[0]
    author = modinfo.get("author") if modinfo else patch_data.get("author")
    version = modinfo.get("version") if modinfo else patch_data.get("version")
    description = modinfo.get("description") if modinfo else patch_data.get("description")

    if existing_mod_id:
        mod_id = existing_mod_id
        # Clear existing deltas for re-import
        db.connection.execute("DELETE FROM mod_deltas WHERE mod_id = ?", (mod_id,))
        import shutil
        old_delta_dir = deltas_dir / str(mod_id)
        if old_delta_dir.exists():
            shutil.rmtree(old_delta_dir)
    else:
        cursor = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mod_name, "paz", priority, author, version, description))
        mod_id = cursor.lastrowid

    changed_files = []
    entry_cache: dict[str, PazEntry] = {}

    for patch in patches:
        game_file = patch["game_file"]
        changes = patch["changes"]
        if not changes:
            continue

        # Find PAMT entry
        logger.info("import_json_as_entr: looking up '%s' in PAMTs", game_file)
        if game_file.lower() not in entry_cache:
            entry = _find_pamt_entry(game_file, vanilla_dir)
            if entry is None:
                entry = _find_pamt_entry(game_file, game_dir)
            if entry:
                entry_cache[game_file.lower()] = entry
                logger.info("import_json_as_entr: found '%s' in %s (offset=%d, comp=%d)",
                            game_file, Path(entry.paz_file).parent.name,
                            entry.offset, entry.comp_size)

        entry = entry_cache.get(game_file.lower())
        if entry is None:
            logger.error("Could not find '%s' in any PAMT index", game_file)
            # Rollback
            db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
            db.connection.commit()
            return None

        # Extract and decompress
        logger.info("import_json_as_entr: extracting '%s' from %s", game_file, entry.paz_file)
        try:
            if not os.path.exists(entry.paz_file):
                logger.info("import_json_as_entr: PAZ not found at %s, trying game dir", entry.paz_file)
                game_entry = _find_pamt_entry(game_file, game_dir)
                if game_entry:
                    entry = game_entry
                    entry_cache[game_file.lower()] = entry
            plaintext = _extract_from_paz(entry)
            logger.info("import_json_as_entr: extracted %d bytes", len(plaintext))
        except Exception as e:
            try:
                game_entry = _find_pamt_entry(game_file, game_dir)
                if game_entry:
                    plaintext = _extract_from_paz(game_entry)
                    entry = game_entry
                    entry_cache[game_file.lower()] = entry
                else:
                    raise
            except Exception:
                logger.error("Failed to extract %s: %s", game_file, e)
                db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
                db.connection.commit()
                return None

        # Apply byte patches
        modified = bytearray(plaintext)
        signature = patch.get("signature")
        applied = _apply_byte_patches(modified, changes, signature=signature)
        logger.info("Applied %d/%d patches to %s", applied, len(changes), game_file)

        if bytes(modified) == plaintext:
            # Content unchanged. Could mean: (a) patches had no effect, or
            # (b) game file already has the patched values (mod already applied).
            # For case (b), try to get vanilla content to compare against.
            # If modified differs from vanilla, the mod IS doing something.
            vanilla_content = None
            if applied > 0:
                try:
                    van_entry = _find_pamt_entry(game_file, vanilla_dir)
                    if van_entry and os.path.exists(van_entry.paz_file):
                        vanilla_content = _extract_from_paz(van_entry)
                except Exception:
                    pass
            if vanilla_content is not None and bytes(modified) != vanilla_content:
                logger.info("Mod already applied to %s, using current content as delta", game_file)
                # Content is correct (already patched), proceed to save delta
            elif vanilla_content is None and applied > 0:
                # No vanilla backup to compare against, but patches were
                # detected as already applied. Trust the "already applied"
                # detection and create the delta anyway.
                logger.info("Mod likely already applied to %s (no vanilla to verify), creating delta", game_file)
            else:
                logger.info("No changes after patching %s, skipping", game_file)
                continue

        # Determine PAZ file path for this entry
        pamt_dir = Path(entry.paz_file).parent.name
        paz_file_path = f"{pamt_dir}/{entry.paz_index}.paz"

        # Save as ENTR delta
        metadata = {
            "pamt_dir": pamt_dir,
            "entry_path": entry.path,
            "paz_index": entry.paz_index,
            "compression_type": entry.compression_type,
            "flags": entry.flags,
            "vanilla_offset": entry.offset,
            "vanilla_comp_size": entry.comp_size,
            "vanilla_orig_size": entry.orig_size,
            "encrypted": entry.encrypted,
        }

        safe_name = entry.path.replace("/", "_") + ".entr"
        delta_path = deltas_dir / str(mod_id) / safe_name
        save_entry_delta(bytes(modified), metadata, delta_path)

        # DB entry
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, entry_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mod_id, paz_file_path, str(delta_path),
             entry.offset, entry.offset + entry.comp_size, entry.path))

        changed_files.append({
            "file_path": paz_file_path,
            "entry_path": entry.path,
            "delta_path": str(delta_path),
        })

        logger.info("ENTR delta: %s in %s (comp=%d, orig=%d)",
                     entry.path, paz_file_path, entry.comp_size, entry.orig_size)

    # Archive JSON source for auto-reimport after game updates
    sources_dir = deltas_dir.parent / "sources" / str(mod_id)
    try:
        import shutil
        if sources_dir.exists():
            shutil.rmtree(sources_dir)
        sources_dir.mkdir(parents=True, exist_ok=True)
        # Copy the original JSON file and any sibling JSONs (for multi-preset mods)
        # Prefer _original_source (set by toggle picker) over _json_path (may be temp file)
        json_path = patch_data.get("_original_source") or patch_data.get("_json_path")
        if json_path and Path(json_path).exists():
            src = Path(json_path)
            if src.is_file():
                # Copy all sibling JSON files so Configure can show all presets
                parent = src.parent
                copied = False
                for sibling in parent.glob("*.json"):
                    shutil.copy2(sibling, sources_dir / sibling.name)
                    copied = True
                if not copied:
                    shutil.copy2(src, sources_dir / src.name)
            elif src.is_dir():
                shutil.copytree(src, sources_dir, dirs_exist_ok=True)
        db.connection.execute(
            "UPDATE mods SET source_path = ? WHERE id = ?",
            (str(sources_dir), mod_id))
        logger.info("Archived JSON source: %s -> %s", mod_name, sources_dir)
    except Exception as e:
        logger.warning("Failed to archive JSON source: %s", e)

    db.connection.commit()
    return {"mod_id": mod_id, "changed_files": changed_files, "name": mod_name}
