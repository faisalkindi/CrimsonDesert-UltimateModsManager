"""PAZ crypto and compression library.

Provides ChaCha20 encryption/decryption with deterministic key derivation,
and LZ4 block compression/decompression for Crimson Desert PAZ archives.

Keys are derived from the filename alone — no key database needed.

Usage:
    from cdumm.archive.paz_crypto import derive_key_iv, encrypt, decrypt, lz4_compress
"""

import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
import lz4.block

# ── Key derivation constants ─────────────────────────────────────────

HASH_INITVAL = 0x000C5EDE
IV_XOR = 0x60616263
XOR_DELTAS = [
    0x00000000, 0x0A0A0A0A, 0x0C0C0C0C, 0x06060606,
    0x0E0E0E0E, 0x0A0A0A0A, 0x06060606, 0x02020202,
]


# ── Bob Jenkins' lookup3 hashlittle ──────────────────────────────────

def _rot(v, k):
    return ((v << k) | (v >> (32 - k))) & 0xFFFFFFFF

def _add(a, b):
    return (a + b) & 0xFFFFFFFF

def _sub(a, b):
    return (a - b) & 0xFFFFFFFF


def hashlittle(data: bytes, initval: int = 0) -> int:
    """Bob Jenkins' lookup3 hashlittle — returns the primary hash (c)."""
    length = len(data)
    a = b = c = _add(0xDEADBEEF + length, initval)
    off = 0

    while length > 12:
        a = _add(a, struct.unpack_from('<I', data, off)[0])
        b = _add(b, struct.unpack_from('<I', data, off + 4)[0])
        c = _add(c, struct.unpack_from('<I', data, off + 8)[0])
        # mix
        a = _sub(a, c); a ^= _rot(c, 4);  c = _add(c, b)
        b = _sub(b, a); b ^= _rot(a, 6);  a = _add(a, c)
        c = _sub(c, b); c ^= _rot(b, 8);  b = _add(b, a)
        a = _sub(a, c); a ^= _rot(c, 16); c = _add(c, b)
        b = _sub(b, a); b ^= _rot(a, 19); a = _add(a, c)
        c = _sub(c, b); c ^= _rot(b, 4);  b = _add(b, a)
        off += 12
        length -= 12

    # Handle remaining bytes (zero-padded to 12)
    tail = data[off:] + b'\x00' * 12
    if length >= 12:
        c = _add(c, struct.unpack_from('<I', tail, 8)[0])
    elif length >= 9:
        v = struct.unpack_from('<I', tail, 8)[0]
        c = _add(c, v & (0xFFFFFFFF >> (8 * (12 - length))))
    if length >= 8:
        b = _add(b, struct.unpack_from('<I', tail, 4)[0])
    elif length >= 5:
        v = struct.unpack_from('<I', tail, 4)[0]
        b = _add(b, v & (0xFFFFFFFF >> (8 * (8 - length))))
    if length >= 4:
        a = _add(a, struct.unpack_from('<I', tail, 0)[0])
    elif length >= 1:
        v = struct.unpack_from('<I', tail, 0)[0]
        a = _add(a, v & (0xFFFFFFFF >> (8 * (4 - length))))
    elif length == 0:
        return c

    # final
    c ^= b; c = _sub(c, _rot(b, 14))
    a ^= c; a = _sub(a, _rot(c, 11))
    b ^= a; b = _sub(b, _rot(a, 25))
    c ^= b; c = _sub(c, _rot(b, 16))
    a ^= c; a = _sub(a, _rot(c, 4))
    b ^= a; b = _sub(b, _rot(a, 14))
    c ^= b; c = _sub(c, _rot(b, 24))
    return c


# ── Key derivation ───────────────────────────────────────────────────

def derive_key_iv(filename: str) -> tuple[bytes, bytes]:
    """Derive 32-byte ChaCha20 key and 16-byte IV from a filename."""
    basename = os.path.basename(filename).lower()
    seed = hashlittle(basename.encode('utf-8'), HASH_INITVAL)

    iv = struct.pack('<I', seed) * 4
    key_base = seed ^ IV_XOR
    key = b''.join(struct.pack('<I', key_base ^ d) for d in XOR_DELTAS)
    return key, iv


# ── ChaCha20 encrypt/decrypt ────────────────────────────────────────

def chacha20(data: bytes, key: bytes, iv: bytes) -> bytes:
    """ChaCha20 encrypt or decrypt (symmetric)."""
    cipher = Cipher(algorithms.ChaCha20(key, iv), mode=None)
    return cipher.encryptor().update(data)


def decrypt(data: bytes, filename: str) -> bytes:
    """Decrypt data using a key derived from the filename."""
    key, iv = derive_key_iv(filename)
    return chacha20(data, key, iv)


def encrypt(data: bytes, filename: str) -> bytes:
    """Encrypt data using a key derived from the filename (same as decrypt)."""
    return decrypt(data, filename)


# ── LZ4 compression ─────────────────────────────────────────────────

def lz4_decompress(data: bytes, original_size: int) -> bytes:
    """LZ4 block decompression (no frame header)."""
    return lz4.block.decompress(data, uncompressed_size=original_size)


def lz4_compress(data: bytes) -> bytes:
    """LZ4 block compression (no frame header, matching game format)."""
    return lz4.block.compress(data, store_size=False)
