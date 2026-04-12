"""Thin parser for .pamt (PAMT index) files.

Format:
  [0:4] = file hash
  [4:8] = PAZ count
  [8:12] = version/magic
  [12:16] = zero padding
  [16:] = PAZ table entries

This parser identifies which region(s) of the PAMT a byte range overlaps.
"""
import struct


# Header region boundaries: (end_offset, label)
_HEADER_REGIONS = [
    (4, "pamt file hash"),
    (8, "pamt PAZ count field"),
    (12, "pamt version/magic"),
    (16, "pamt header padding"),
]


def identify_pamt_records(data: bytes, byte_start: int, byte_end: int) -> str | None:
    """Identify which pamt region(s) a byte range overlaps.

    Args:
        data: raw PAMT file bytes
        byte_start: start of range (inclusive)
        byte_end: end of range (exclusive)
    """
    if len(data) < 16:
        return None

    try:
        paz_count = struct.unpack_from("<I", data, 4)[0]
        regions: list[str] = []

        # Check header regions
        for end, label in _HEADER_REGIONS:
            if byte_start < end and byte_end > (end - 4 if end > 4 else 0):
                regions.append(label)

        # PAZ table region starts at offset 16
        # First entry: 8 bytes [hash:4][size:4], subsequent: 12 bytes [sep:4][hash:4][size:4]
        if paz_count > 0 and paz_count < 1000:
            first_entry_end = 24
            if byte_start < first_entry_end and byte_end > 16:
                regions.append("pamt PAZ entry 0 (hash + size)")

            for i in range(1, paz_count):
                entry_start = 24 + (i - 1) * 12
                entry_end = entry_start + 12
                if byte_start < entry_end and byte_end > entry_start:
                    regions.append(f"pamt PAZ entry {i}")

        # Beyond PAZ table — file records area
        table_end = 24 + max(0, paz_count - 1) * 12 if paz_count > 0 else 16
        if byte_end > table_end and byte_start >= table_end:
            regions.append(f"pamt file records (offset 0x{byte_start:X})")

        return " + ".join(regions) if regions else None

    except (struct.error, ValueError):
        return None
