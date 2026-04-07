"""Overlay PAZ builder for Crimson Desert.

Builds a fresh overlay PAZ + PAMT from ENTR delta entries. The overlay
directory replaces modifying original game files in-place. The game loads
entries from the overlay directory first, leaving vanilla files untouched.

Matches JSON Mod Manager's BuildMultiPamt format exactly.
"""

import struct
import logging
from dataclasses import dataclass

import lz4.block

from cdumm.archive.hashlittle import hashlittle
from cdumm.archive.paz_repack import fix_dds_header

logger = logging.getLogger(__name__)

HASH_SEED = 0xC5EDE
PAZ_ALIGNMENT = 16
PAMT_CONSTANT = 0x610E0232  # from JSON MM: 1628308018u

# Cache for full path maps (per pamt_dir)
_path_map_cache: dict[str, dict[str, str]] = {}


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

            for i in range(file_count):
                nr = struct.unpack_from('<I', data, off)[0]
                off += 20
                filename = build_node_path(nr)
                # Find which folder this file belongs to
                for fp, fi, fc in folder_recs:
                    if fi <= i < fi + fc:
                        # Build flattened key: root_folder/filename
                        flattened = f"{root_folder}/{filename}" if root_folder else filename
                        result[flattened] = fp
                        break

            return result
        except Exception as e:
            logger.debug("Failed to build path map for %s: %s", pamt_dir, e)
            continue

    return result


def build_overlay(
    entries: list[tuple[bytes, dict]],
    game_dir=None,
) -> tuple[bytes, bytes]:
    """Build overlay PAZ + PAMT from ENTR delta entries.

    Args:
        entries: list of (decompressed_content, entr_metadata) tuples.
        game_dir: game installation directory (for vanilla PAMT lookup).

    Returns:
        (paz_bytes, pamt_bytes) ready to write to overlay directory.
    """
    paz_buf = bytearray()
    overlay_entries: list[OverlayEntry] = []

    for content, metadata in entries:
        entry_path = metadata["entry_path"]
        comp_type = metadata.get("compression_type", 2)
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

        if comp_type == 1:
            # DDS split: 128-byte header + LZ4 body
            DDS_HEADER_SIZE = 128
            header = bytearray(content[:DDS_HEADER_SIZE])
            body = content[DDS_HEADER_SIZE:]

            compressed_body = lz4.block.compress(body, store_size=False)

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
            compressed = lz4.block.compress(content, store_size=False)
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

        logger.info("Overlay entry: %s/%s (comp=%d, decomp=%d, type=%d)",
                     dir_path, filename, comp_size, decomp_size, comp_type)

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
