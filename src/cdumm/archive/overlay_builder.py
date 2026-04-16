"""Overlay PAZ builder for Crimson Desert.

Builds a fresh overlay PAZ + PAMT from ENTR delta entries. The overlay
directory replaces modifying original game files in-place. The game loads
entries from the overlay directory first, leaving vanilla files untouched.

Matches JSON Mod Manager's BuildMultiPamt format exactly.
"""

import struct
import logging
from dataclasses import dataclass

from cdumm.archive.hashlittle import hashlittle
from cdumm.archive.paz_repack import fix_dds_header
from cdumm.archive.paz_crypto import lz4_compress

logger = logging.getLogger(__name__)

HASH_SEED = 0xC5EDE
PAZ_ALIGNMENT = 16
PAMT_CONSTANT = 0x610E0232  # from JSON MM: 1628308018u

# Cache for full path maps (per pamt_dir)
_path_map_cache: dict[str, dict[str, str]] = {}

# Extension → compression type mapping (from CRIMSON_DESERT_MODDING_BIBLE.md §4)
_EXT_COMP_TYPE: dict[str, int] = {
    ".dds": 1,   # DDS texture: 128-byte header + LZ4 body
    ".bnk": 0,   # Wwise soundbank: raw uncompressed
}


def _infer_comp_type_from_extension(filename: str) -> int:
    """Infer PAZ compression type from file extension.

    Returns 1 for DDS textures, 0 for BNK soundbanks, 2 (LZ4) for everything else.
    """
    dot = filename.rfind(".")
    if dot >= 0:
        ext = filename[dot:].lower()
        return _EXT_COMP_TYPE.get(ext, 2)
    return 2


@dataclass
class OverlayEntry:
    dir_path: str       # folder path in PAMT (e.g. "gamedata", "ui")
    filename: str       # file basename (e.g. "inventory.pabgb")
    paz_offset: int     # offset in the overlay PAZ
    comp_size: int      # compressed size in PAZ
    decomp_size: int    # decompressed size
    flags: int          # ushort flags (compression_type, no encryption)


def _build_full_path_map(pamt_dir: str, game_dir) -> dict[str, str]:
    """Build a map of flattened_path -> full_folder_path from the vanilla PAMT.

    The PAMT stores files with full hierarchical folder paths in its folder
    records (via folder tree references). parse_pamt flattens these to
    top-level-folder/filename, but the game uses the full path for lookups.

    Returns: {flattened_entry_path: full_folder_path}
    """
    from pathlib import Path
    from cdumm.archive.paz_parse import parse_pamt

    game_dir = Path(game_dir)
    result = {}

    for base in [game_dir / "CDMods" / "vanilla", game_dir]:
        pamt_path = base / pamt_dir / "0.pamt"
        if not pamt_path.exists():
            continue

        data = pamt_path.read_bytes()
        if len(data) < 24:
            continue

        try:
            # Skip header: hash(4) + paz_count(4) + hash2(4) + zero(4)
            off = 16
            # Skip PAZ table entries
            pc = struct.unpack_from('<I', data, 4)[0]
            for i in range(pc):
                off += 8  # hash + size
                if i < pc - 1:
                    off += 4  # separator

            # Folder section — build hierarchical folder tree
            folder_len = struct.unpack_from('<I', data, off)[0]; off += 4
            folders = {}
            foff = off
            while foff < off + folder_len:
                rel = foff - off
                parent = struct.unpack_from('<I', data, foff)[0]
                slen = data[foff + 4]
                name = data[foff + 5:foff + 5 + slen].decode('utf-8', errors='replace')
                folders[rel] = (parent, name)
                foff += 5 + slen
            off += folder_len

            def build_folder_path(ref):
                parts = []
                cur = ref
                while cur != 0xFFFFFFFF and len(parts) < 20:
                    if cur not in folders:
                        break
                    p, n = folders[cur]
                    parts.append(n)
                    cur = p
                return ''.join(reversed(parts))

            # Node section — trie of filenames
            node_len = struct.unpack_from('<I', data, off)[0]; off += 4
            nodes = {}
            noff = off
            while noff < off + node_len:
                rel = noff - off
                parent = struct.unpack_from('<I', data, noff)[0]
                slen = data[noff + 4]
                name = data[noff + 5:noff + 5 + slen].decode('utf-8', errors='replace')
                nodes[rel] = (parent, name)
                noff += 5 + slen
            off += node_len

            def build_node_path(ref):
                parts = []
                cur = ref
                while cur != 0xFFFFFFFF and len(parts) < 64:
                    if cur not in nodes:
                        break
                    p, n = nodes[cur]
                    parts.append(n)
                    cur = p
                return ''.join(reversed(parts))

            # Folder records — map each folder to its file range
            folder_count = struct.unpack_from('<I', data, off)[0]; off += 4
            folder_recs = []
            for i in range(folder_count):
                ph, fr, fi, fc = struct.unpack_from('<IIII', data, off)
                folder_recs.append((build_folder_path(fr), fi, fc))
                off += 16

            # File records — build the map
            file_count = struct.unpack_from('<I', data, off)[0]; off += 4
            # Find root folder name (for building flattened path)
            root_folder = ""
            for _, (p, n) in folders.items():
                if p == 0xFFFFFFFF:
                    root_folder = n
                    break

            # Build file_index → folder_path lookup (O(1) per file instead of O(folders))
            file_to_folder: dict[int, str] = {}
            for fp, fi, fc in folder_recs:
                for idx in range(fi, fi + fc):
                    file_to_folder[idx] = fp

            for i in range(file_count):
                nr = struct.unpack_from('<I', data, off)[0]
                off += 20
                filename = build_node_path(nr)
                fp = file_to_folder.get(i)
                if fp is not None:
                    flattened = f"{root_folder}/{filename}" if root_folder else filename
                    result[flattened] = fp

            return result
        except Exception as e:
            logger.debug("Failed to build path map for %s: %s", pamt_dir, e)
            continue

    return result


def _load_overlay_cache(game_dir) -> dict:
    """Load the overlay cache manifest from the previous Apply.

    Returns {entry_path: {offset, comp_size, decomp_size, flags, delta_hash, overlay_dir}}
    and the cached overlay PAZ bytes (or None).
    """
    import json
    from pathlib import Path
    cache_path = Path(game_dir) / "CDMods" / ".overlay_cache.json"
    if not cache_path.exists():
        return {}, None, None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        overlay_dir = manifest.get("_overlay_dir")
        if not overlay_dir:
            return {}, None, None
        paz_path = Path(game_dir) / overlay_dir / "0.paz"
        if not paz_path.exists():
            return {}, None, None
        paz_data = paz_path.read_bytes()
        entries = {k: v for k, v in manifest.items() if not k.startswith("_")}
        return entries, paz_data, overlay_dir
    except Exception as e:
        logger.debug("Overlay cache load failed: %s", e)
        return {}, None, None


def _save_overlay_cache(game_dir, overlay_dir, cache_entries: dict):
    """Save overlay cache manifest for incremental rebuild."""
    import json
    from pathlib import Path
    cache_path = Path(game_dir) / "CDMods" / ".overlay_cache.json"
    manifest = dict(cache_entries)
    manifest["_overlay_dir"] = overlay_dir
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
    except Exception as e:
        logger.debug("Overlay cache save failed: %s", e)


def _get_vanilla_pabgh(pamt_dir: str, pabgb_entry_path: str, game_dir) -> bytes | None:
    """Extract the vanilla PABGH file that corresponds to a PABGB entry.

    Looks up the sibling .pabgh in the same PAMT directory and extracts it
    from the vanilla PAZ file.

    Returns raw (decompressed) PABGH bytes, or None if not found.
    """
    from pathlib import Path
    from cdumm.archive.paz_parse import parse_pamt, PazEntry

    if not pamt_dir or not game_dir:
        return None

    # Derive PABGH path from PABGB path
    pabgh_path = pabgb_entry_path.replace(".pabgb", ".pabgh")
    pabgh_filename = pabgh_path.rsplit("/", 1)[-1] if "/" in pabgh_path else pabgh_path

    game_dir = Path(game_dir)

    # Search in vanilla backup first, then game dir
    for base in [game_dir / "CDMods" / "vanilla", game_dir]:
        pamt_path = base / pamt_dir / "0.pamt"
        if not pamt_path.exists():
            continue

        try:
            entries = parse_pamt(str(pamt_path), paz_dir=str(base / pamt_dir))
            for e in entries:
                if e.path.lower().endswith(pabgh_filename.lower()):
                    # Found the PABGH entry — extract from PAZ
                    paz_path = Path(e.paz_file)
                    if not paz_path.exists():
                        continue
                    with open(paz_path, "rb") as f:
                        f.seek(e.offset)
                        raw = f.read(e.comp_size)

                    if e.comp_size != e.orig_size and e.orig_size > 0:
                        # Compressed — decompress
                        from cdumm.archive.paz_crypto import lz4_decompress
                        return lz4_decompress(raw, e.orig_size)
                    else:
                        # Uncompressed
                        return raw
        except Exception as ex:
            logger.debug("PABGH extraction failed for %s: %s", pabgh_filename, ex)
            continue

    return None


def build_overlay(
    entries: list[tuple[bytes, dict]],
    game_dir=None,
    progress_cb=None,
    preloaded_cache=None,
) -> tuple[bytes, bytes]:
    """Build overlay PAZ + PAMT from ENTR delta entries.

    Uses incremental rebuild: entries unchanged since last Apply are copied
    from the cached overlay PAZ (no re-compression). Only new/changed entries
    are compressed from scratch.

    Args:
        entries: list of (decompressed_content, entr_metadata) tuples.
        game_dir: game installation directory (for vanilla PAMT lookup).
        progress_cb: optional (idx, total, entry_name) callback.
        preloaded_cache: optional pre-loaded (manifest, paz_data, dir) tuple
            from _load_overlay_cache. Used when the previous overlay dir
            gets cleaned up before build runs.

    Returns:
        (paz_bytes, pamt_bytes) ready to write to overlay directory.
    """
    if preloaded_cache:
        cache_manifest, cached_paz, _cached_dir = preloaded_cache
    elif game_dir:
        cache_manifest, cached_paz, _cached_dir = _load_overlay_cache(game_dir)
    else:
        cache_manifest, cached_paz, _cached_dir = {}, None, None
    cache_hits = 0
    new_cache: dict[str, dict] = {}

    paz_buf = bytearray()
    overlay_entries: list[OverlayEntry] = []
    _added_pabgh: set[str] = set()  # track which PABGH files we've added
    total_entries = len(entries)

    for entry_idx, (content, metadata) in enumerate(entries):
        if progress_cb and total_entries > 0:
            ename = metadata.get("entry_path", "").rsplit("/", 1)[-1] if metadata.get("entry_path") else ""
            progress_cb(entry_idx, total_entries, ename)
        entry_path = metadata["entry_path"]
        comp_type = metadata.get("compression_type")
        if comp_type is None:
            comp_type = _infer_comp_type_from_extension(
                entry_path.rsplit("/", 1)[-1] if "/" in entry_path else entry_path)
        pamt_dir = metadata.get("pamt_dir", "")

        # Resolve full folder path from vanilla PAMT.
        # The game uses full hierarchical paths for VFS lookups, not the
        # flattened top-level-folder/filename from parse_pamt.
        if "/" in entry_path:
            _, filename = entry_path.rsplit("/", 1)
        else:
            filename = entry_path

        dir_path = ""
        if game_dir and pamt_dir:
            # Build path map for this PAMT directory (cached per pamt_dir)
            cache_key = pamt_dir
            if cache_key not in _path_map_cache:
                _path_map_cache[cache_key] = _build_full_path_map(pamt_dir, game_dir)
            path_map = _path_map_cache[cache_key]
            dir_path = path_map.get(entry_path, "")

        if not dir_path and "/" in entry_path:
            dir_path = entry_path.rsplit("/", 1)[0]

        paz_offset = len(paz_buf)

        # Check overlay cache: if this entry is unchanged, copy compressed
        # bytes directly from the previous overlay PAZ (skip decompression/recompression)
        delta_hash = metadata.get("delta_hash", "")
        if not delta_hash and metadata.get("delta_path"):
            # Use delta file mtime as a cheap change indicator
            import os
            try:
                delta_hash = str(os.path.getmtime(metadata["delta_path"]))
            except OSError:
                delta_hash = ""

        cached = cache_manifest.get(entry_path)
        if (cached and cached_paz and delta_hash
                and cached.get("delta_hash") == delta_hash):
            # Cache hit — copy pre-compressed bytes from previous overlay
            c_off = cached["offset"]
            c_size = cached["comp_size"]
            c_end = c_off + c_size
            # Include padding to alignment
            pad = (PAZ_ALIGNMENT - (c_end % PAZ_ALIGNMENT)) % PAZ_ALIGNMENT
            c_padded = c_end + pad
            # Safety: verify alignment and bounds
            if (c_padded <= len(cached_paz)
                    and paz_offset % PAZ_ALIGNMENT == 0  # current position aligned
                    and c_size > 0):
                paz_buf.extend(cached_paz[c_off:c_padded])
                overlay_entries.append(OverlayEntry(
                    dir_path=dir_path, filename=filename,
                    paz_offset=paz_offset, comp_size=cached["comp_size"],
                    decomp_size=cached["decomp_size"], flags=cached["flags"],
                ))
                new_cache[entry_path] = {
                    "offset": paz_offset, "comp_size": cached["comp_size"],
                    "decomp_size": cached["decomp_size"], "flags": cached["flags"],
                    "delta_hash": delta_hash,
                }
                cache_hits += 1
                continue

        if comp_type == 1:
            # DDS type 0x01: check if DX10 multi-mip (raw passthrough)
            # or standard DDS (inner LZ4 compression).
            # DX10 multi-mip textures must be written raw — inner LZ4
            # breaks them (confirmed broken in JSON MM 9.8.3 too).
            fourcc = content[84:88] if len(content) >= 88 else b""
            is_dx10 = fourcc == b"DX10" and len(content) >= 148
            mip_count = max(1, struct.unpack_from("<I", content, 28)[0]) if len(content) >= 32 else 1

            if is_dx10 and mip_count > 1:
                # DX10 multi-mip: raw passthrough, no compression
                payload = content
                comp_size = len(payload)
                decomp_size = len(payload)
            else:
                # Standard DDS: inner LZ4 compression
                DDS_HEADER_SIZE = 128
                header = bytearray(content[:DDS_HEADER_SIZE])
                body = content[DDS_HEADER_SIZE:]

                compressed_body = lz4_compress(body)

                if header[:4] == b"DDS ":
                    header = fix_dds_header(header, len(compressed_body))

                full_size = len(content)
                payload_core = bytes(header) + compressed_body
                if len(payload_core) < full_size:
                    payload = payload_core + b'\x00' * (full_size - len(payload_core))
                else:
                    payload = payload_core

                comp_size = full_size
                decomp_size = full_size
            flags = 1

        elif comp_type == 2:
            compressed = lz4_compress(content)
            payload = compressed
            comp_size = len(compressed)
            decomp_size = len(content)
            flags = 2

        else:
            payload = content
            comp_size = len(content)
            decomp_size = len(content)
            flags = 0

        paz_buf.extend(payload)

        # Align to 16 bytes
        pad = PAZ_ALIGNMENT - (len(paz_buf) % PAZ_ALIGNMENT)
        if pad < PAZ_ALIGNMENT:
            paz_buf.extend(b'\x00' * pad)

        overlay_entries.append(OverlayEntry(
            dir_path=dir_path, filename=filename,
            paz_offset=paz_offset, comp_size=comp_size,
            decomp_size=decomp_size, flags=flags,
        ))

        # Cache this entry for future incremental rebuilds
        new_cache[entry_path] = {
            "offset": paz_offset, "comp_size": comp_size,
            "decomp_size": decomp_size, "flags": flags,
            "delta_hash": delta_hash,
        }

        logger.info("Overlay entry: %s/%s (comp=%d, decomp=%d, type=%d)",
                     dir_path, filename, comp_size, decomp_size, comp_type)

        # Auto-include matching PABGH alongside PABGB entries.
        # The game needs the index file to read the data file. Without it,
        # the game uses the vanilla PABGH which has stale offsets if the
        # PABGB structure changed.
        if filename.endswith(".pabgb") and game_dir:
            pabgh_name = filename.replace(".pabgb", ".pabgh")
            pabgh_key = f"{dir_path}/{pabgh_name}" if dir_path else pabgh_name
            # Only add if we haven't already added this PABGH
            if pabgh_key not in _added_pabgh:
                pabgh_data = _get_vanilla_pabgh(pamt_dir, entry_path, game_dir)
                if pabgh_data:
                    _added_pabgh.add(pabgh_key)
                    pabgh_offset = len(paz_buf)
                    paz_buf.extend(pabgh_data)
                    # Align
                    pad = PAZ_ALIGNMENT - (len(paz_buf) % PAZ_ALIGNMENT)
                    if pad < PAZ_ALIGNMENT:
                        paz_buf.extend(b'\x00' * pad)
                    overlay_entries.append(OverlayEntry(
                        dir_path=dir_path, filename=pabgh_name,
                        paz_offset=pabgh_offset,
                        comp_size=len(pabgh_data),
                        decomp_size=len(pabgh_data),
                        flags=0,  # uncompressed, matching DMM
                    ))
                    logger.info("Overlay entry (auto PABGH): %s/%s (%d bytes, uncompressed)",
                                 dir_path, pabgh_name, len(pabgh_data))

    if cache_hits > 0:
        logger.info("Overlay cache: %d/%d entries reused (skipped recompression)",
                     cache_hits, len(entries))

    paz_bytes = bytes(paz_buf)
    pamt_bytes = _build_multi_pamt(overlay_entries, len(paz_bytes))

    # Patch PAZ CRC into PAMT at offset 16, then recompute outer hash
    paz_crc = hashlittle(paz_bytes, HASH_SEED)
    pamt_buf = bytearray(pamt_bytes)
    struct.pack_into("<I", pamt_buf, 16, paz_crc)
    outer_hash = hashlittle(bytes(pamt_buf[12:]), HASH_SEED)
    struct.pack_into("<I", pamt_buf, 0, outer_hash)
    pamt_bytes = bytes(pamt_buf)

    logger.info("Overlay built: %d entries, PAZ=%d bytes, PAMT=%d bytes",
                len(overlay_entries), len(paz_bytes), len(pamt_bytes))

    # Store cache for caller to save after overlay dir is allocated
    build_overlay._last_cache = new_cache

    return paz_bytes, pamt_bytes


def _build_multi_pamt(entries: list[OverlayEntry], paz_data_len: int) -> bytes:
    """Build a PAMT file matching JSON MM's BuildMultiPamt format exactly.

    Layout:
        [0:4]   outer_hash (hashlittle(pamt[12:], 0xC5EDE))
        [4:8]   paz_count (1)
        [8:12]  constant (0x610E0232)
        [12:16] zero (0)
        [16:20] PAZ CRC (filled by caller)
        [20:24] PAZ data length
        folder_section_len(4) + folder_bytes
        node_section_len(4) + node_bytes
        folder_count(4) + folder_records (16 bytes each, NO hash prefix)
        file_count(4) + file_records (20 bytes each)
    """
    unique_dirs = sorted(set(e.dir_path for e in entries))

    # ── Folder section (directory tree) ──
    folder_bytes = bytearray()
    folder_offsets: dict[str, int] = {}

    for dir_path in unique_dirs:
        parts = dir_path.split("/") if dir_path else [""]
        for depth in range(len(parts)):
            key = "/".join(parts[:depth + 1])
            if key in folder_offsets:
                continue
            offset = len(folder_bytes)
            folder_offsets[key] = offset

            if depth == 0:
                parent = 0xFFFFFFFF
                name = parts[0]
            else:
                parent_key = "/".join(parts[:depth])
                parent = folder_offsets[parent_key]
                name = "/" + parts[depth]

            name_bytes = name.encode("utf-8")
            folder_bytes += struct.pack("<I", parent)
            folder_bytes += bytes([len(name_bytes)])
            folder_bytes += name_bytes

    # ── Node section (filenames) ──
    node_bytes = bytearray()
    node_offsets: dict[int, int] = {}

    # Group and sort entries by dir
    dir_entries: dict[str, list[tuple[int, OverlayEntry]]] = {}
    for i, e in enumerate(entries):
        dir_entries.setdefault(e.dir_path, []).append((i, e))
    for d in dir_entries:
        dir_entries[d].sort(key=lambda x: x[1].filename)

    for dir_path in unique_dirs:
        for idx, entry in dir_entries.get(dir_path, []):
            node_offsets[idx] = len(node_bytes)
            name_bytes = entry.filename.encode("utf-8")
            node_bytes += struct.pack("<I", 0xFFFFFFFF)
            node_bytes += bytes([len(name_bytes)])
            node_bytes += name_bytes

    # ── Folder records (16 bytes each) ──
    folder_records = bytearray()
    file_index = 0

    for dir_path in unique_dirs:
        count = len(dir_entries.get(dir_path, []))
        path_hash = hashlittle(dir_path.encode("utf-8"), HASH_SEED)
        folder_ref = folder_offsets.get(dir_path, 0)

        folder_records += struct.pack("<IIII",
                                       path_hash, folder_ref, file_index, count)
        file_index += count

    # ── File records (20 bytes each: node_ref(4) + offset(4) + comp(4) + decomp(4) + zero(2) + flags(2)) ──
    file_records = bytearray()
    for dir_path in unique_dirs:
        for idx, entry in dir_entries.get(dir_path, []):
            node_ref = node_offsets[idx]
            file_records += struct.pack("<IIIIHH",
                                         node_ref,
                                         entry.paz_offset,
                                         entry.comp_size,
                                         entry.decomp_size,
                                         0,
                                         entry.flags)

    # ── Assemble PAMT body (without outer hash) ──
    body = bytearray()
    body += struct.pack("<I", 1)                  # paz_count
    body += struct.pack("<I", PAMT_CONSTANT)      # constant
    body += struct.pack("<I", 0)                  # zero
    body += struct.pack("<I", 0)                  # PAZ CRC placeholder
    body += struct.pack("<I", paz_data_len)       # PAZ size

    body += struct.pack("<I", len(folder_bytes))
    body += folder_bytes

    body += struct.pack("<I", len(node_bytes))
    body += node_bytes

    body += struct.pack("<I", len(unique_dirs))
    body += folder_records

    body += struct.pack("<I", file_index)  # file_count prefix
    body += file_records

    # Prepend outer hash placeholder
    pamt = bytearray(4) + body  # [0:4] = 0 (filled by caller)
    return bytes(pamt)
