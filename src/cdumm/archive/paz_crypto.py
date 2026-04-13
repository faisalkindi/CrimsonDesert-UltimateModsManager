"""PAZ crypto and compression library.

Provides ChaCha20 encryption/decryption with deterministic key derivation,
and LZ4 block compression/decompression for Crimson Desert PAZ archives.

Keys are derived from the filename alone — no key database needed.

Uses Rust cdumm_native for performance-critical operations when available,
with Python fallback for development/testing.

Usage:
    from cdumm.archive.paz_crypto import derive_key_iv, encrypt, decrypt, lz4_compress
"""

import os
import struct

try:
    import cdumm_native as _native
    _HAS_NATIVE = True
except ImportError:
    _native = None
    _HAS_NATIVE = False

# Use the canonical hashlittle implementation (single source of truth)
from cdumm.archive.hashlittle import hashlittle

# ── Key derivation constants ─────────────────────────────────────────

HASH_INITVAL = 0x000C5EDE
IV_XOR = 0x60616263
XOR_DELTAS = [
    0x00000000, 0x0A0A0A0A, 0x0C0C0C0C, 0x06060606,
    0x0E0E0E0E, 0x0A0A0A0A, 0x06060606, 0x02020202,
]


# ── Key derivation ───────────────────────────────────────────────────

def derive_key_iv(filename: str) -> tuple[bytes, bytes]:
    """Derive 32-byte ChaCha20 key and 16-byte IV from a filename."""
    if _HAS_NATIVE:
        return _native.derive_key_iv(filename)
    basename = os.path.basename(filename).lower()
    seed = hashlittle(basename.encode('utf-8'), HASH_INITVAL)

    iv = struct.pack('<I', seed) * 4
    key_base = seed ^ IV_XOR
    key = b''.join(struct.pack('<I', key_base ^ d) for d in XOR_DELTAS)
    return key, iv


# ── ChaCha20 encrypt/decrypt ────────────────────────────────────────

def decrypt(data: bytes, filename: str) -> bytes:
    """Decrypt data using a key derived from the filename."""
    if _HAS_NATIVE:
        return _native.chacha20_decrypt(data, filename)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    key, iv = derive_key_iv(filename)
    cipher = Cipher(algorithms.ChaCha20(key, iv), mode=None)
    return cipher.encryptor().update(data)


def encrypt(data: bytes, filename: str) -> bytes:
    """Encrypt data using a key derived from the filename (same as decrypt)."""
    return decrypt(data, filename)


# ── LZ4 compression ─────────────────────────────────────────────────

def lz4_decompress(data: bytes, original_size: int) -> bytes:
    """LZ4 block decompression (no frame header)."""
    if _HAS_NATIVE:
        return _native.lz4_decompress(data, original_size)
    import lz4.block
    return lz4.block.decompress(data, uncompressed_size=original_size)


def lz4_compress(data: bytes) -> bytes:
    """LZ4 block compression (no frame header, matching game format)."""
    if _HAS_NATIVE:
        return _native.lz4_compress(data)
    import lz4.block
    return lz4.block.compress(data, store_size=False)
