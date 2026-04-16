"""hashlittle implementation for PAMT/PAPGT integrity chain.

This is the Bob Jenkins hashlittle hash used by Crimson Desert for:
- PAMT hash: hashlittle(pamt[12:], 0xC5EDE)
- PAPGT hash: hashlittle(papgt[12:], 0xC5EDE)

Uses Rust cdumm_native.compute_hashlittle when available (260x faster),
falls back to pure Python.
"""
import struct

try:
    from cdumm_native import compute_hashlittle as _native_hashlittle
except ImportError:
    _native_hashlittle = None


def hashlittle(data: bytes, initval: int = 0) -> int:
    """Bob Jenkins hashlittle hash function."""
    if _native_hashlittle is not None:
        return _native_hashlittle(data, initval)
    length = len(data)
    a = b = c = (0xDEADBEEF + length + initval) & 0xFFFFFFFF

    offset = 0
    while length > 12:
        a = (a + struct.unpack_from("<I", data, offset)[0]) & 0xFFFFFFFF
        b = (b + struct.unpack_from("<I", data, offset + 4)[0]) & 0xFFFFFFFF
        c = (c + struct.unpack_from("<I", data, offset + 8)[0]) & 0xFFFFFFFF

        a = (a - c) & 0xFFFFFFFF; a ^= ((c << 4) | (c >> 28)) & 0xFFFFFFFF; c = (c + b) & 0xFFFFFFFF
        b = (b - a) & 0xFFFFFFFF; b ^= ((a << 6) | (a >> 26)) & 0xFFFFFFFF; a = (a + c) & 0xFFFFFFFF
        c = (c - b) & 0xFFFFFFFF; c ^= ((b << 8) | (b >> 24)) & 0xFFFFFFFF; b = (b + a) & 0xFFFFFFFF
        a = (a - c) & 0xFFFFFFFF; a ^= ((c << 16) | (c >> 16)) & 0xFFFFFFFF; c = (c + b) & 0xFFFFFFFF
        b = (b - a) & 0xFFFFFFFF; b ^= ((a << 19) | (a >> 13)) & 0xFFFFFFFF; a = (a + c) & 0xFFFFFFFF
        c = (c - b) & 0xFFFFFFFF; c ^= ((b << 4) | (b >> 28)) & 0xFFFFFFFF; b = (b + a) & 0xFFFFFFFF

        offset += 12
        length -= 12

    # Handle remaining bytes
    remaining = data[offset:]
    if length > 0:
        # Pad remaining bytes into a, b, c
        padded = remaining + b"\x00" * (12 - len(remaining))
        if length >= 1: a = (a + padded[0]) & 0xFFFFFFFF
        if length >= 2: a = (a + (padded[1] << 8)) & 0xFFFFFFFF
        if length >= 3: a = (a + (padded[2] << 16)) & 0xFFFFFFFF
        if length >= 4: a = (a + (padded[3] << 24)) & 0xFFFFFFFF
        if length >= 5: b = (b + padded[4]) & 0xFFFFFFFF
        if length >= 6: b = (b + (padded[5] << 8)) & 0xFFFFFFFF
        if length >= 7: b = (b + (padded[6] << 16)) & 0xFFFFFFFF
        if length >= 8: b = (b + (padded[7] << 24)) & 0xFFFFFFFF
        if length >= 9: c = (c + padded[8]) & 0xFFFFFFFF
        if length >= 10: c = (c + (padded[9] << 8)) & 0xFFFFFFFF
        if length >= 11: c = (c + (padded[10] << 16)) & 0xFFFFFFFF
        if length >= 12: c = (c + (padded[11] << 24)) & 0xFFFFFFFF

        # Final mixing
        c ^= b; c = (c - ((b << 14) | (b >> 18))) & 0xFFFFFFFF
        a ^= c; a = (a - ((c << 11) | (c >> 21))) & 0xFFFFFFFF
        b ^= a; b = (b - ((a << 25) | (a >> 7))) & 0xFFFFFFFF
        c ^= b; c = (c - ((b << 16) | (b >> 16))) & 0xFFFFFFFF
        a ^= c; a = (a - ((c << 4) | (c >> 28))) & 0xFFFFFFFF
        b ^= a; b = (b - ((a << 14) | (a >> 18))) & 0xFFFFFFFF
        c ^= b; c = (c - ((b << 24) | (b >> 8))) & 0xFFFFFFFF

    return c


INTEGRITY_SEED = 0xC5EDE


def compute_pamt_hash(pamt_data: bytes) -> int:
    """Compute PAMT integrity hash: hashlittle(pamt[12:], 0xC5EDE)."""
    return hashlittle(pamt_data[12:], INTEGRITY_SEED)


def compute_papgt_hash(papgt_data: bytes) -> int:
    """Compute PAPGT integrity hash: hashlittle(papgt[12:], 0xC5EDE)."""
    return hashlittle(papgt_data[12:], INTEGRITY_SEED)
