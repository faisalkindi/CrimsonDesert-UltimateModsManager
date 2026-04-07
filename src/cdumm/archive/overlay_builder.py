"""Overlay PAZ builder for Crimson Desert.

Builds a fresh overlay PAZ + PAMT from ENTR delta entries. The overlay
directory replaces modifying original game files in-place. The game loads
entries from the overlay directory first, leaving vanilla files untouched.

This matches the approach used by JSON Mod Manager's overlay system.
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
PAMT_MAGIC_CONST = 0x6121A532  # constant from JSON MM's BuildMultiPamt


@dataclass
class OverlayEntry:
    dir_path: str       # folder path in PAMT (e.g. "gamedata", "ui")
    filename: str       # file basename (e.g. "inventory.pabgb")
    paz_offset: int     # offset in the overlay PAZ
    comp_size: int      # compressed size in PAZ
    decomp_size: int    # decompressed size
    flags: int          # ushort flags (compression_type, no encryption)


def build_overlay(
    entries: list[tuple[bytes, dict]],
) -> tuple[bytes, bytes]:
    """Build overlay PAZ + PAMT from ENTR delta entries.

    Args:
        entries: list of (decompressed_content, entr_metadata) tuples.
            metadata keys: entry_path, compression_type, flags,
            vanilla_comp_size, vanilla_orig_size, encrypted

    Returns:
        (paz_bytes, pamt_bytes) ready to write to overlay directory.
    """
    paz_buf = bytearray()
    overlay_entries: list[OverlayEntry] = []

    for content, metadata in entries:
        entry_path = metadata["entry_path"]
        comp_type = metadata.get("compression_type", 2)

        # Split entry_path into dir_path and filename
        if "/" in entry_path:
            dir_path, filename = entry_path.rsplit("/", 1)
        else:
            dir_path = ""
            filename = entry_path

        # Compress based on type — NO encryption for overlay
        paz_offset = len(paz_buf)

        if comp_type == 1:
            # DDS split: 128-byte header + LZ4 body
            DDS_HEADER_SIZE = 128
            header = bytearray(content[:DDS_HEADER_SIZE])
            body = content[DDS_HEADER_SIZE:]

            compressed_body = lz4.block.compress(body, store_size=False)

            # Fix DDS header with compressed body size
            if header[:4] == b"DDS ":
                header = fix_dds_header(header, len(compressed_body))

            # Pad to full DDS size (comp_size == orig_size for type 0x01)
            full_size = len(content)
            payload_core = bytes(header) + compressed_body
            if len(payload_core) < full_size:
                payload = payload_core + b'\x00' * (full_size - len(payload_core))
            else:
                payload = payload_core

            comp_size = full_size
            decomp_size = full_size
            flags = 1  # DDS split, no encryption

        elif comp_type == 2:
            # LZ4 compressed
            compressed = lz4.block.compress(content, store_size=False)
            payload = compressed
            comp_size = len(compressed)
            decomp_size = len(content)
            flags = 2  # LZ4, no encryption

        else:
            # Uncompressed
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
            dir_path=dir_path,
            filename=filename,
            paz_offset=paz_offset,
            comp_size=comp_size,
            decomp_size=decomp_size,
            flags=flags,
        ))

        logger.info("Overlay entry: %s/%s (comp=%d, decomp=%d, type=%d)",
                     dir_path, filename, comp_size, decomp_size, comp_type)

    paz_bytes = bytes(paz_buf)
    pamt_bytes = _build_multi_pamt(overlay_entries, len(paz_bytes))

    # Patch PAZ CRC into PAMT
    paz_crc = hashlittle(paz_bytes, HASH_SEED)
    pamt_buf = bytearray(pamt_bytes)
    struct.pack_into("<I", pamt_buf, 20, paz_crc)
    # Recompute outer PAMT hash (hash of pamt[12:])
    outer_hash = hashlittle(bytes(pamt_buf[12:]), HASH_SEED)
    struct.pack_into("<I", pamt_buf, 0, outer_hash)
    pamt_bytes = bytes(pamt_buf)

    logger.info("Overlay built: %d entries, PAZ=%d bytes, PAMT=%d bytes",
                len(overlay_entries), len(paz_bytes), len(pamt_bytes))

    return paz_bytes, pamt_bytes


def _build_multi_pamt(entries: list[OverlayEntry], paz_data_len: int) -> bytes:
    """Build a PAMT file for the overlay PAZ.

    Follows JSON MM's BuildMultiPamt format:
    - Header: outer_hash(4) + paz_count(4) + magic(4) + zero(4) + paz_crc(4) + paz_size(4)
    - Folder section: hierarchical folder tree
    - Node section: filenames
    - Folder records: per-directory metadata (hash, folder_ref, first_file, count)
    - File records: per-file metadata (node_ref, offset, comp, decomp, zero16, flags16)
    """
    # Collect unique directory paths, sorted
    unique_dirs = sorted(set(e.dir_path for e in entries))

    # ── Folder section ──
    # Build folder tree from directory paths
    folder_bytes = bytearray()
    folder_offsets: dict[str, int] = {}  # full_path -> byte offset in folder section

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

    # ── Node section ──
    # Filenames as flat entries (parent = 0xFFFFFFFF)
    node_bytes = bytearray()
    node_offsets: dict[int, int] = {}  # entry index -> byte offset in node section

    # Group entries by dir, sort within each dir
    dir_entries: dict[str, list[tuple[int, OverlayEntry]]] = {}
    for i, e in enumerate(entries):
        dir_entries.setdefault(e.dir_path, []).append((i, e))
    for d in dir_entries:
        dir_entries[d].sort(key=lambda x: x[1].filename)

    # Build node section in the same order as file records
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
    dir_file_ranges: dict[str, tuple[int, int]] = {}  # dir -> (first_index, count)

    for dir_path in unique_dirs:
        count = len(dir_entries.get(dir_path, []))
        dir_file_ranges[dir_path] = (file_index, count)

        path_hash = hashlittle(dir_path.encode("utf-8"), HASH_SEED)
        folder_ref = folder_offsets.get(dir_path, 0)

        folder_records += struct.pack("<IIII",
                                       path_hash, folder_ref, file_index, count)
        file_index += count

    # ── File records (22 bytes each) ──
    file_records = bytearray()
    for dir_path in unique_dirs:
        for idx, entry in dir_entries.get(dir_path, []):
            node_ref = node_offsets[idx]
            file_records += struct.pack("<IIIIIH",
                                         node_ref,
                                         entry.paz_offset,
                                         entry.comp_size,
                                         entry.decomp_size,
                                         0,  # ushort zero
                                         entry.flags)

    # ── Assemble PAMT ──
    pamt = bytearray()

    # Header: outer_hash(4) + paz_count(4) + magic(4) + zero(4) + paz_crc(4) + paz_size(4)
    pamt += struct.pack("<I", 0)  # [0:4] outer hash placeholder
    pamt += struct.pack("<I", 1)  # [4:8] paz_count = 1
    pamt += struct.pack("<I", PAMT_MAGIC_CONST)  # [8:12]
    pamt += struct.pack("<I", 0)  # [12:16] zero
    pamt += struct.pack("<I", 0)  # [16:20] PAZ CRC placeholder
    pamt += struct.pack("<I", paz_data_len)  # [20:24] PAZ size

    # Folder section (length-prefixed)
    pamt += struct.pack("<I", len(folder_bytes))
    pamt += folder_bytes

    # Node section (length-prefixed)
    pamt += struct.pack("<I", len(node_bytes))
    pamt += node_bytes

    # Folder records (count-prefixed)
    num_folders = len(unique_dirs)
    pamt += struct.pack("<I", num_folders)
    # Folder hash (hashlittle of all folder record bytes)
    folder_hash = hashlittle(bytes(folder_records), HASH_SEED) if folder_records else 0
    pamt += struct.pack("<I", folder_hash)
    pamt += folder_records

    # File records (count-prefixed)
    # Note: JSON MM writes file count then records directly
    # But looking at parse_pamt, after folder records there's no count prefix
    # for file records — they're the remaining bytes as 20-byte records.
    # Actually, JSON MM's BuildMultiPamt writes file count then records.
    pamt += struct.pack("<I", file_index)  # total file count
    pamt += file_records

    return bytes(pamt)
