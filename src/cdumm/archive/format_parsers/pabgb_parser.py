"""Thin parser for .pabgb (PA Binary Game Body) files.

Format:
  Header (.pabgh): u16 count + N x (u32 hash + u32 offset)
  Body (.pabgb): variable-length records at offsets from header

This parser identifies which record a byte range falls within by
scanning the offset table to find record boundaries.
"""
import struct


def identify_pabgb_records(data: bytes, byte_start: int, byte_end: int) -> str | None:
    """Identify which pabgb record(s) a byte range overlaps.

    Uses a simple heuristic: scan for the header pattern (u16 count followed by
    hash+offset pairs) and map byte ranges to record indices.
    """
    if len(data) < 10:
        return None

    # Try to parse as header+body structure
    # pabgh header: u16 count, then N x (u32 hash, u32 offset)
    try:
        count = struct.unpack_from("<H", data, 0)[0]
        if count == 0 or count > 50000:
            return None

        header_size = 2 + count * 8
        if header_size > len(data):
            return None

        # Extract offsets
        offsets: list[tuple[int, int]] = []  # (record_index, offset)
        for i in range(count):
            base = 2 + i * 8
            record_hash = struct.unpack_from("<I", data, base)[0]
            record_offset = struct.unpack_from("<I", data, base + 4)[0]
            offsets.append((i, record_offset))

        # Sort by offset to determine boundaries
        offsets.sort(key=lambda x: x[1])

        # Find which record(s) the byte range overlaps
        overlapping: list[int] = []
        for idx, (record_idx, offset) in enumerate(offsets):
            # Determine record end
            if idx + 1 < len(offsets):
                record_end = offsets[idx + 1][1]
            else:
                record_end = len(data)

            # Check overlap
            if byte_start < record_end and byte_end > offset:
                overlapping.append(record_idx)

        if not overlapping:
            return None

        if len(overlapping) == 1:
            return f"pabgb record {overlapping[0]}"
        return f"pabgb records {', '.join(str(r) for r in overlapping)}"

    except (struct.error, ValueError):
        return None
