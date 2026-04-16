"""PATHC format handler for Crimson Desert texture mods.

Reads and writes meta/0.pathc — the game's texture path index that maps
virtual asset paths (hashed) to DDS template records.

Format:
    Header (7 uint32): unknowns, dds_record_size, dds_record_count,
                        hash_count, collision_path_count, collision_blob_size
    DDS records table (fixed-size records starting with "DDS " magic)
    Hash table (sorted uint32 array for binary search)
    Map entries (20 bytes each: selector + 4 metadata fields)
    Collision entries (24 bytes each) + collision blob (null-terminated strings)

Based on reference tools by 993499094 (NexusMods).
"""

from __future__ import annotations

import bisect
import io
import struct
from dataclasses import dataclass
from pathlib import Path

from cdumm.archive.hashlittle import hashlittle

HASH_INITVAL = 0x000C5EDE

# Block compression sizes by DDS FourCC
_BC_BLOCK_BYTES_BY_FOURCC = {
    b"DXT1": 8, b"ATI1": 8, b"BC4U": 8, b"BC4S": 8,
    b"DXT3": 16, b"DXT5": 16, b"ATI2": 16, b"BC5U": 16, b"BC5S": 16,
}
# Block compression sizes by DXGI format ID
_BC_BLOCK_BYTES_BY_DXGI = {
    70: 8, 71: 8, 72: 8, 73: 16, 74: 16, 75: 16, 76: 16, 77: 16, 78: 16,
    79: 8, 80: 8, 81: 8, 82: 16, 83: 16, 84: 16, 94: 16, 95: 16, 96: 16,
    97: 16, 98: 16, 99: 16,
}
_DXGI_BITS_PER_PIXEL = {10: 64, 24: 32, 28: 32, 61: 8}


@dataclass(slots=True)
class PathcHeader:
    unknown0: int
    unknown1: int
    dds_record_size: int
    dds_record_count: int
    hash_count: int
    collision_path_count: int
    collision_blob_size: int


@dataclass(slots=True)
class PathcMapEntry:
    selector: int
    m1: int
    m2: int
    m3: int
    m4: int


@dataclass(slots=True)
class PathcCollisionEntry:
    path_offset: int
    dds_index: int
    m1: int
    m2: int
    m3: int
    m4: int
    path: str = ""


@dataclass(slots=True)
class PathcFile:
    header: PathcHeader
    dds_records: list[bytes]
    key_hashes: list[int]
    map_entries: list[PathcMapEntry]
    collision_entries: list[PathcCollisionEntry]


def read_pathc(path: Path) -> PathcFile:
    """Parse a .pathc file into its component sections."""
    raw = path.read_bytes()
    if len(raw) < 0x1C:
        raise ValueError(f"{path} is too small to be a valid .pathc file.")

    header = PathcHeader(*struct.unpack_from("<7I", raw, 0))

    dds_table_off = 0x1C
    hash_table_off = dds_table_off + header.dds_record_size * header.dds_record_count
    map_table_off = hash_table_off + header.hash_count * 4
    collision_table_off = map_table_off + header.hash_count * 20
    collision_blob_off = collision_table_off + header.collision_path_count * 24
    collision_blob_end = collision_blob_off + header.collision_blob_size

    dds_records = []
    for i in range(header.dds_record_count):
        off = dds_table_off + i * header.dds_record_size
        dds_records.append(raw[off:off + header.dds_record_size])

    key_hashes = list(struct.unpack_from(f"<{header.hash_count}I", raw, hash_table_off))

    map_entries = []
    for i in range(header.hash_count):
        vals = struct.unpack_from("<IIIII", raw, map_table_off + i * 20)
        map_entries.append(PathcMapEntry(*vals))

    blob = raw[collision_blob_off:collision_blob_end]
    collision_entries = []
    for i in range(header.collision_path_count):
        poff, dds_idx, m1, m2, m3, m4 = struct.unpack_from(
            "<6I", raw, collision_table_off + i * 24)
        end = blob.find(b"\x00", poff)
        path_str = blob[poff:end].decode("utf-8", errors="replace") if end != -1 else ""
        collision_entries.append(
            PathcCollisionEntry(poff, dds_idx, m1, m2, m3, m4, path_str))

    return PathcFile(header, dds_records, key_hashes, map_entries, collision_entries)


def serialize_pathc(pathc: PathcFile) -> bytes:
    """Serialize a PathcFile back to binary."""
    # Rebuild collision blob with fresh offsets
    collision_blob = bytearray()
    collision_rows = []
    for entry in pathc.collision_entries:
        path_bytes = entry.path.encode("utf-8") + b"\x00"
        poff = len(collision_blob)
        collision_blob.extend(path_bytes)
        collision_rows.append(struct.pack(
            "<6I", poff, entry.dds_index, entry.m1, entry.m2, entry.m3, entry.m4))

    pathc.header.dds_record_count = len(pathc.dds_records)
    pathc.header.hash_count = len(pathc.key_hashes)
    pathc.header.collision_path_count = len(pathc.collision_entries)
    pathc.header.collision_blob_size = len(collision_blob)

    out = io.BytesIO()
    out.write(struct.pack(
        "<7I",
        pathc.header.unknown0, pathc.header.unknown1,
        pathc.header.dds_record_size, pathc.header.dds_record_count,
        pathc.header.hash_count, pathc.header.collision_path_count,
        pathc.header.collision_blob_size))

    for rec in pathc.dds_records:
        out.write(rec)

    if pathc.key_hashes:
        out.write(struct.pack(f"<{len(pathc.key_hashes)}I", *pathc.key_hashes))

    for entry in pathc.map_entries:
        out.write(struct.pack("<IIIII",
                              entry.selector, entry.m1, entry.m2, entry.m3, entry.m4))

    for row in collision_rows:
        out.write(row)

    out.write(collision_blob)

    # Recompute PATHC self-hash at offset 4.
    # The hash covers everything from offset 8 onwards (after the two header words).
    # This is the same pattern as PAPGT: hash = hashlittle(data[8:], 0).
    result = out.getvalue()
    from cdumm.archive.hashlittle import hashlittle as _hl
    content_hash = _hl(result[8:], 0)
    result = result[:4] + struct.pack("<I", content_hash) + result[8:]
    return result


def normalize_path(path_str: str) -> str:
    """Normalize a virtual path for hashing: forward slashes, leading /."""
    path = path_str.replace("\\", "/").strip().lstrip("/").strip("/")
    return "/" + path


def get_path_hash(path_str: str) -> int:
    """Hash a virtual path for PATHC lookup."""
    return hashlittle(normalize_path(path_str).lower().encode("utf-8"), HASH_INITVAL)


def get_dds_metadata(data: bytes) -> tuple[int, int, int, int]:
    """Extract mipmap size metadata from a DDS file header.

    Returns (mip0_size, mip1_size, mip2_size, mip3_size).
    """
    if len(data) < 128 or data[:4] != b"DDS ":
        return (0, 0, 0, 0)

    _hsize, _flags, height, width, pitch, _depth, mips = struct.unpack_from("<7I", data, 4)
    mips = max(1, mips)
    _pf_size, pf_flags, pf_fourcc, pf_rgb_bits = struct.unpack_from("<4I", data, 76)
    fourcc = struct.pack("<I", pf_fourcc)

    dxgi = None
    if fourcc == b"DX10" and len(data) >= 148:
        dxgi = struct.unpack_from("<I", data, 128)[0]

    block_bytes = _BC_BLOCK_BYTES_BY_FOURCC.get(fourcc)
    if block_bytes is None and dxgi is not None:
        block_bytes = _BC_BLOCK_BYTES_BY_DXGI.get(dxgi)

    bpp = 0
    if block_bytes is None:
        if dxgi is not None:
            bpp = _DXGI_BITS_PER_PIXEL.get(dxgi, 0)
        if bpp == 0 and (pf_flags & 0x40):
            bpp = pf_rgb_bits

    sizes = []
    curr_w, curr_h = max(1, width), max(1, height)
    for i in range(min(4, mips)):
        if block_bytes:
            size = max(1, (curr_w + 3) // 4) * max(1, (curr_h + 3) // 4) * block_bytes
        elif bpp > 0:
            size = ((curr_w * bpp + 7) // 8) * curr_h
        elif i == 0 and pitch > 0:
            size = pitch
        else:
            size = 0
        sizes.append(size & 0xFFFFFFFF)
        curr_w, curr_h = max(1, curr_w // 2), max(1, curr_h // 2)

    while len(sizes) < 4:
        sizes.append(0)
    return tuple(sizes)


def create_dds_record(dds_path: Path, record_size: int) -> bytes:
    """Create a DDS template record from a DDS file.

    Copies the first record_size bytes of the DDS header.
    """
    data = dds_path.read_bytes()
    if not data.startswith(b"DDS "):
        raise ValueError(f"Not a valid DDS file: {dds_path}")
    record = bytearray(record_size)
    to_copy = min(len(data), record_size)
    record[:to_copy] = data[:to_copy]
    return bytes(record)


def update_entry(pathc: PathcFile, virtual_path: str, dds_index: int,
                 m: tuple[int, int, int, int] = (0, 0, 0, 0)) -> None:
    """Add or update a hash table entry for a virtual path."""
    target_hash = get_path_hash(virtual_path)
    idx = bisect.bisect_left(pathc.key_hashes, target_hash)

    selector = 0xFFFF0000 | (dds_index & 0xFFFF)

    if idx < len(pathc.key_hashes) and pathc.key_hashes[idx] == target_hash:
        # Update existing entry
        pathc.map_entries[idx].selector = selector
        pathc.map_entries[idx].m1 = m[0]
        pathc.map_entries[idx].m2 = m[1]
        pathc.map_entries[idx].m3 = m[2]
        pathc.map_entries[idx].m4 = m[3]
    else:
        # Insert new entry at sorted position
        pathc.key_hashes.insert(idx, target_hash)
        pathc.map_entries.insert(idx, PathcMapEntry(selector, *m))


def add_dds_file(pathc: PathcFile, dds_path: Path, virtual_path: str) -> int:
    """Add a single DDS file to the PATHC index.

    Creates or deduplicates the DDS record, computes metadata,
    and updates the hash table entry.

    Returns the DDS record index used.
    """
    dds_rec = create_dds_record(dds_path, pathc.header.dds_record_size)
    dds_data = dds_path.read_bytes()
    m = get_dds_metadata(dds_data)

    # Deduplicate: reuse existing record if identical
    try:
        dds_idx = pathc.dds_records.index(dds_rec)
    except ValueError:
        pathc.dds_records.append(dds_rec)
        dds_idx = len(pathc.dds_records) - 1

    update_entry(pathc, virtual_path, dds_idx, m)
    return dds_idx


def add_folder_recursive(pathc: PathcFile, folder_path: Path) -> list[str]:
    """Add all .dds files from a folder to the PATHC index.

    Virtual paths are derived from relative paths within the folder.
    Returns list of virtual paths that were added.
    """
    added = []
    for dds_file in sorted(folder_path.rglob("*.dds")):
        rel = dds_file.relative_to(folder_path)
        vpath = "/" + rel.as_posix()
        try:
            add_dds_file(pathc, dds_file, vpath)
            added.append(vpath)
        except Exception:
            continue
    return added
