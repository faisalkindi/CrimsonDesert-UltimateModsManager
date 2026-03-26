"""Thin parser for .paac (PA Action Chart) files.

Format:
  Header: 68 bytes — node_count (u32 at offset 0), speed (f32 at offset 4)
  State records: marked by M0%D magic (0x4D302544)
  String table: u16 count + null-terminated strings
  Condition section: 260-byte blocks marked by M0%D

This parser identifies which state record a byte range falls within.
"""
import struct

M0PD_MAGIC = b"M0%D"


def identify_paac_records(data: bytes, byte_start: int, byte_end: int) -> str | None:
    """Identify which paac state record a byte range overlaps."""
    if len(data) < 68:
        return None

    try:
        node_count = struct.unpack_from("<I", data, 0)[0]
        if node_count == 0 or node_count > 10000:
            return None

        # Header region
        if byte_start < 68:
            return "paac header"

        # Scan for M0%D markers to find record boundaries
        markers: list[int] = []
        pos = 68
        while pos < len(data) - 4:
            if data[pos:pos + 4] == M0PD_MAGIC:
                markers.append(pos)
            pos += 1
            # Optimization: after finding a marker, skip ahead
            if markers and pos == markers[-1] + 1:
                pos = markers[-1] + 16  # minimum record size

        if not markers:
            return f"paac data (byte offset {byte_start})"

        # Find which marker region the byte range falls in
        for idx, marker_pos in enumerate(markers):
            if idx + 1 < len(markers):
                region_end = markers[idx + 1]
            else:
                region_end = len(data)

            if byte_start < region_end and byte_end > marker_pos:
                return f"paac state record {idx} (offset 0x{marker_pos:X})"

        return f"paac data (byte offset {byte_start})"

    except (struct.error, ValueError):
        return None
