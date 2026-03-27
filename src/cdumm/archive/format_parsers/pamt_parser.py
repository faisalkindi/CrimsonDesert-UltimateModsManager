"""Thin parser for .pamt (PAMT index) files.

Format:
  [0:4] = file hash
  [4:8] = PAZ count
  [8:12] = version/magic
  [12:16] = zero padding
  [16:] = PAZ table entries

This parser identifies which region of the PAMT a byte range falls within.
"""
import struct


def identify_pamt_records(data: bytes, byte_start: int, byte_end: int) -> str | None:
    """Identify which pamt region a byte range overlaps."""
    if len(data) < 16:
        return None

    try:
        paz_count = struct.unpack_from("<I", data, 4)[0]

        if byte_start < 4:
            return "pamt file hash"
        if byte_start < 8:
            return "pamt PAZ count field"
        if byte_start < 12:
            return "pamt version/magic"
        if byte_start < 16:
            return "pamt header padding"

        # PAZ table region starts at offset 16
        # Each entry: first is [hash:4][size:4], subsequent are [separator:4][hash:4][size:4]
        if paz_count > 0 and paz_count < 1000:
            # Estimate which PAZ entry the byte falls in
            # First entry: 8 bytes at offset 16
            # Subsequent: 12 bytes each
            if byte_start < 24:
                return "pamt PAZ entry 0 (hash + size)"

            remaining_offset = byte_start - 24
            entry_idx = 1 + remaining_offset // 12
            if entry_idx < paz_count:
                return f"pamt PAZ entry {entry_idx}"

        # Beyond PAZ table — likely the prefix trie / file records
        return f"pamt file records (offset 0x{byte_start:X})"

    except (struct.error, ValueError):
        return None
