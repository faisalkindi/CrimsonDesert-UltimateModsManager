"""Surgical offset rewrite for .pabgh index files.

A .pabgh maps record keys to byte offsets inside its companion
.pabgb. When a whole-table writer grows or shrinks records, every
offset after the first size change goes stale, and the game reads
garbage entry headers (audit finding A, 2026-06-10: the iteminfo
and skill whole-table writers shipped grown tables with vanilla
indexes; the storeinfo / equipslotinfo / multichangeinfo writers
already rebuilt theirs).

Rather than synthesizing a fresh index (whose padding or entry
order might not match what the game shipped), this rewrites ONLY
the 4-byte offset fields inside a copy of the vanilla header,
leaving the count, key order, key bytes, and any trailing padding
byte-for-byte intact. Identity property: rewriting with the
offsets obtained from serializing the UNMODIFIED table must return
the input unchanged, callers use that as a pre-flight gate.

Layout (mirrors ``semantic.parser.parse_pabgh_index``):
count (u16, or u32 for UINT_COUNT_TABLES) + count x (key + u32
offset), key width derived from the byte budget.
"""
from __future__ import annotations

import logging
import struct

from cdumm.semantic.parser import UINT_COUNT_TABLES

logger = logging.getLogger(__name__)


def rewrite_pabgh_offsets(
    header: bytes,
    table_name: str,
    new_offsets: dict[int, int],
) -> bytes | None:
    """Return a copy of ``header`` with each entry's offset replaced
    by ``new_offsets[key]``.

    Returns None when the header doesn't parse with the expected
    layout or when any key in the header is missing from
    ``new_offsets``, a partial rewrite would ship a half-stale
    index, which is exactly the corruption this exists to prevent.
    """
    name_lower = (table_name or "").lower()
    count_size = 4 if name_lower in UINT_COUNT_TABLES else 2

    if len(header) < count_size:
        logger.warning("pabgh rewrite: header too short (%d bytes)",
                       len(header))
        return None

    if count_size == 4:
        count = struct.unpack_from("<I", header, 0)[0]
    else:
        count = struct.unpack_from("<H", header, 0)[0]
    if count == 0:
        return bytes(header)

    total_key_bytes = len(header) - count_size - count * 4
    if total_key_bytes <= 0 or total_key_bytes % count != 0:
        logger.warning(
            "pabgh rewrite: key-size derivation failed for %s "
            "(header=%d, count=%d)", table_name, len(header), count)
        return None
    key_size = total_key_bytes // count
    if key_size not in (2, 4, 8):
        logger.warning(
            "pabgh rewrite: implausible key size %d for %s",
            key_size, table_name)
        return None

    arr = bytearray(header)
    pos = count_size
    for _ in range(count):
        if pos + key_size + 4 > len(arr):
            logger.warning(
                "pabgh rewrite: truncated entry table in %s", table_name)
            return None
        key = int.from_bytes(arr[pos:pos + key_size], "little")
        if key not in new_offsets:
            logger.warning(
                "pabgh rewrite: key %d present in %s.pabgh but absent "
                "from the rebuilt table; refusing partial rewrite",
                key, table_name)
            return None
        struct.pack_into("<I", arr, pos + key_size, new_offsets[key])
        pos += key_size + 4

    return bytes(arr)
