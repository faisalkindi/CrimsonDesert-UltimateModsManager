"""Binary delta engine — generates and applies file patches.

Uses two strategies:
- Sparse patch: for same-size files with few byte changes (fast, O(n) scan)
- bsdiff4: for files that change size or have extensive modifications

Sparse patch format:
  b"SPRS" + u32 patch_count + (u64 offset, u32 length, bytes data) * N
"""
import logging
import struct
from pathlib import Path

import bsdiff4

logger = logging.getLogger(__name__)

SPARSE_MAGIC = b"SPRS"
# Use sparse patch if files are same size and changed bytes < 1% of file
SPARSE_THRESHOLD_RATIO = 0.01
# Always use sparse for files > 500 MB (bsdiff would use too much RAM)
BSDIFF_SIZE_LIMIT = 500 * 1024 * 1024


def generate_delta(vanilla_bytes: bytes, modified_bytes: bytes) -> bytes:
    """Generate a binary delta from vanilla to modified.

    Automatically chooses sparse patch (fast) or bsdiff4 (smaller output)
    based on file characteristics.
    """
    import time
    size_mb = len(vanilla_bytes) / 1048576
    t0 = time.perf_counter()

    if len(vanilla_bytes) == len(modified_bytes):
        # Same size — check how many bytes differ
        ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)
        dt = time.perf_counter() - t0
        total_changed = sum(end - start for start, end in ranges)
        logger.info("Byte scan: %.1f MB in %.1fs, %d bytes changed in %d ranges",
                    size_mb, dt, total_changed, len(ranges))

        if total_changed == 0:
            # Identical files
            return _make_sparse_patch([])

        # Use sparse if changes are small or file is too large for bsdiff
        if (total_changed / len(vanilla_bytes) < SPARSE_THRESHOLD_RATIO
                or len(vanilla_bytes) > BSDIFF_SIZE_LIMIT):
            return _make_sparse_patch_from_ranges(vanilla_bytes, modified_bytes, ranges)

    # Different sizes or large changes — use bsdiff4
    # But skip if file is too large (would use 4x RAM)
    if len(vanilla_bytes) > BSDIFF_SIZE_LIMIT:
        # Force sparse even for different sizes — store the changed regions
        ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)
        return _make_sparse_patch_from_ranges(vanilla_bytes, modified_bytes, ranges)

    return bsdiff4.diff(vanilla_bytes, modified_bytes)


def apply_delta(vanilla_bytes: bytes, delta_bytes: bytes) -> bytes:
    """Apply a delta to vanilla bytes, returning modified bytes."""
    if delta_bytes[:4] == SPARSE_MAGIC:
        return _apply_sparse_patch(vanilla_bytes, delta_bytes)
    return bsdiff4.patch(vanilla_bytes, delta_bytes)


def _make_sparse_patch(patches: list[tuple[int, bytes]]) -> bytes:
    """Create a sparse patch: SPRS + count + (offset, length, data) entries."""
    buf = bytearray(SPARSE_MAGIC)
    buf += struct.pack("<I", len(patches))
    for offset, data in patches:
        buf += struct.pack("<QI", offset, len(data))
        buf += data
    return bytes(buf)


def _make_sparse_patch_from_ranges(
    vanilla: bytes, modified: bytes, ranges: list[tuple[int, int]]
) -> bytes:
    """Create sparse patch from changed byte ranges."""
    patches: list[tuple[int, bytes]] = []
    for start, end in ranges:
        if end <= len(modified):
            patches.append((start, modified[start:end]))
    logger.debug("Sparse patch: %d regions, %d bytes changed",
                 len(patches), sum(len(d) for _, d in patches))
    return _make_sparse_patch(patches)


def _apply_sparse_patch(vanilla_bytes: bytes, patch_bytes: bytes) -> bytes:
    """Apply a sparse patch to vanilla bytes."""
    result = bytearray(vanilla_bytes)
    offset = 4  # skip magic
    count = struct.unpack_from("<I", patch_bytes, offset)[0]
    offset += 4

    for _ in range(count):
        file_offset = struct.unpack_from("<Q", patch_bytes, offset)[0]
        offset += 8
        length = struct.unpack_from("<I", patch_bytes, offset)[0]
        offset += 4
        data = patch_bytes[offset:offset + length]
        offset += length

        # Apply patch
        end = file_offset + length
        if end > len(result):
            result.extend(b"\x00" * (end - len(result)))
        result[file_offset:end] = data

    return bytes(result)


def get_changed_byte_ranges(vanilla_bytes: bytes, modified_bytes: bytes) -> list[tuple[int, int]]:
    """Identify contiguous byte ranges that differ between vanilla and modified.

    Returns list of (start, end) tuples where end is exclusive.
    Uses numpy-style chunked comparison for large files (100x faster than Python loop).
    """
    min_len = min(len(vanilla_bytes), len(modified_bytes))

    if min_len == 0:
        if len(modified_bytes) > 0:
            return [(0, len(modified_bytes))]
        if len(vanilla_bytes) > 0:
            return [(0, len(vanilla_bytes))]
        return []

    # XOR the two byte strings — non-zero bytes are differences
    # Process in chunks to avoid creating a 2GB bytearray
    CHUNK = 64 * 1024  # 64 KB chunks
    ranges: list[tuple[int, int]] = []
    in_diff = False
    start = 0

    for chunk_start in range(0, min_len, CHUNK):
        chunk_end = min(chunk_start + CHUNK, min_len)
        v = vanilla_bytes[chunk_start:chunk_end]
        m = modified_bytes[chunk_start:chunk_end]

        # Fast comparison — find first and last difference in chunk
        if v == m:
            # Entire chunk identical
            if in_diff:
                ranges.append((start, chunk_start))
                in_diff = False
            continue

        # Chunk has differences — find exact boundaries
        for i in range(len(v)):
            if v[i] != m[i]:
                if not in_diff:
                    start = chunk_start + i
                    in_diff = True
            else:
                if in_diff:
                    ranges.append((start, chunk_start + i))
                    in_diff = False

    if in_diff:
        ranges.append((start, min_len))

    # Handle size difference
    if len(modified_bytes) > len(vanilla_bytes):
        ranges.append((len(vanilla_bytes), len(modified_bytes)))
    elif len(modified_bytes) < len(vanilla_bytes):
        ranges.append((len(modified_bytes), len(vanilla_bytes)))

    # Coalesce if too many ranges — prevents 100M+ DB rows from PAZ shifts.
    # Merge ranges within a gap that grows until count is manageable.
    MAX_RANGES = 50_000
    if len(ranges) > MAX_RANGES:
        gap = 64
        while len(ranges) > MAX_RANGES and gap < 1_000_000:
            merged: list[tuple[int, int]] = []
            for s, e in ranges:
                if merged and s - merged[-1][1] <= gap:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
            ranges = merged
            gap *= 4

    return ranges


def save_delta(delta_bytes: bytes, delta_path: Path) -> None:
    """Save delta bytes to disk."""
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    delta_path.write_bytes(delta_bytes)
    fmt = "sparse" if delta_bytes[:4] == SPARSE_MAGIC else "bsdiff4"
    logger.debug("Delta saved: %s (%d bytes, %s)", delta_path, len(delta_bytes), fmt)


def load_delta(delta_path: Path) -> bytes:
    """Load delta bytes from disk."""
    return delta_path.read_bytes()
