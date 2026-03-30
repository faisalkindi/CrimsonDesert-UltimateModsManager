"""PAZ asset repacker for Crimson Desert.

Patches modified files back into PAZ archives. Handles encryption and
compression to produce output the game will accept.

Pipeline: modified file -> LZ4 compress -> ChaCha20 encrypt -> write to PAZ

Library usage:
    from cdumm.archive.paz_repack import repack_entry
    from cdumm.archive.paz_parse import parse_pamt, PazEntry

    entries = parse_pamt("0.pamt", paz_dir="./0003")
    entry = next(e for e in entries if "rendererconfiguration" in e.path)
    repack_entry("modified.xml", entry)
"""

import ctypes
import os
import sys

import lz4.block

from cdumm.archive.paz_parse import PazEntry
from cdumm.archive.paz_crypto import encrypt, lz4_compress


# ── Timestamp preservation (Windows) ────────────────────────────────

def _save_timestamps(path: str):
    """Capture NTFS timestamps. Returns a callable to restore them."""
    if sys.platform != 'win32':
        return lambda: None

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]

    OPEN_EXISTING = 3
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_ATTR = 0x80 | 0x02000000

    h = kernel32.CreateFileW(path, GENERIC_READ, 1, None, OPEN_EXISTING, FILE_ATTR, None)
    if h == -1:
        return lambda: None

    ct, at, mt = FILETIME(), FILETIME(), FILETIME()
    kernel32.GetFileTime(h, ctypes.byref(ct), ctypes.byref(at), ctypes.byref(mt))
    kernel32.CloseHandle(h)

    def restore():
        h2 = kernel32.CreateFileW(path, GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_ATTR, None)
        if h2 != -1:
            kernel32.SetFileTime(h2, ctypes.byref(ct), ctypes.byref(at), ctypes.byref(mt))
            kernel32.CloseHandle(h2)

    return restore


# ── Size matching ────────────────────────────────────────────────────

def _pad_to_orig_size(data: bytes, orig_size: int) -> bytes:
    """Pad data to exactly orig_size bytes with zero bytes."""
    if len(data) >= orig_size:
        return data[:orig_size]
    return data + b'\x00' * (orig_size - len(data))


def _match_compressed_size(plaintext: bytes, target_comp_size: int,
                           target_orig_size: int) -> bytes:
    """Adjust plaintext so it compresses to exactly target_comp_size.

    Returns adjusted plaintext (exactly target_orig_size bytes).
    Raises ValueError if size matching fails.
    """
    padded = _pad_to_orig_size(plaintext, target_orig_size)

    comp = lz4.block.compress(padded, store_size=False)
    if len(comp) == target_comp_size:
        return padded

    filler = bytes(range(33, 127))  # printable ASCII

    if len(comp) < target_comp_size:
        lo, hi = 0, target_orig_size - len(plaintext)
        best = padded
        for _ in range(64):
            mid = (lo + hi) // 2
            if mid <= 0:
                break
            fill = (filler * (mid // len(filler) + 1))[:mid]
            trial = plaintext + fill
            trial = _pad_to_orig_size(trial, target_orig_size)
            c = lz4.block.compress(trial, store_size=False)
            if len(c) == target_comp_size:
                return trial
            elif len(c) < target_comp_size:
                lo = mid + 1
                best = trial
            else:
                hi = mid - 1

        for n in range(max(0, lo - 5), min(hi + 5, target_orig_size - len(plaintext))):
            fill = (filler * (n // len(filler) + 1))[:n] if n > 0 else b''
            trial = plaintext + fill
            trial = _pad_to_orig_size(trial, target_orig_size)
            c = lz4.block.compress(trial, store_size=False)
            if len(c) == target_comp_size:
                return trial

    if len(comp) > target_comp_size:
        raise ValueError(
            f"Compressed size {len(comp)} exceeds target {target_comp_size}. "
            f"Reduce file content.")

    raise ValueError(
        f"Cannot match target comp_size {target_comp_size} "
        f"(best: {len(lz4.block.compress(padded, store_size=False))})")


def _strip_whitespace_to_fit(plaintext: bytes, target_comp: int, target_orig: int) -> bytes | None:
    """Strip trailing whitespace from text content to reduce compressed size.

    Returns padded plaintext that compresses within target, or None if impossible.
    """
    # Strip trailing whitespace from each line
    try:
        text = plaintext.decode('utf-8', errors='replace')
    except Exception:
        return None

    # Progressive stripping: first trailing spaces, then blank lines, then comments
    stripped = '\r\n'.join(line.rstrip() for line in text.splitlines())
    candidate = stripped.encode('utf-8')
    padded = _pad_to_orig_size(candidate, target_orig)
    comp = lz4.block.compress(padded, store_size=False)
    if len(comp) <= target_comp:
        return padded

    # More aggressive: collapse multiple spaces/newlines
    import re
    stripped = re.sub(r'[ \t]+', ' ', stripped)
    stripped = re.sub(r'\n{3,}', '\n\n', stripped)
    candidate = stripped.encode('utf-8')
    padded = _pad_to_orig_size(candidate, target_orig)
    comp = lz4.block.compress(padded, store_size=False)
    if len(comp) <= target_comp:
        return padded

    return None


# ── Core repack ──────────────────────────────────────────────────────

def repack_entry(modified_path: str, entry: PazEntry,
                 output_path: str = None, dry_run: bool = False) -> dict:
    """Repack a modified file and patch it into the PAZ archive.

    Args:
        modified_path: path to the modified plaintext file
        entry: PAMT entry for the file being replaced
        output_path: if set, write to this file instead of patching the PAZ
        dry_run: if True, compute sizes but don't write anything

    Returns:
        dict with repack stats
    """
    with open(modified_path, 'rb') as f:
        plaintext = f.read()

    basename = os.path.basename(entry.path)
    is_compressed = entry.compressed and entry.compression_type == 2

    if is_compressed:
        adjusted = _match_compressed_size(plaintext, entry.comp_size, entry.orig_size)
        compressed = lz4.block.compress(adjusted, store_size=False)
        assert len(compressed) == entry.comp_size, \
            f"Size mismatch: {len(compressed)} != {entry.comp_size}"
        payload = compressed
    else:
        if len(plaintext) > entry.comp_size:
            raise ValueError(
                f"Modified file ({len(plaintext)} bytes) exceeds budget "
                f"({entry.comp_size} bytes). Reduce content.")
        payload = plaintext + b'\x00' * (entry.comp_size - len(plaintext))

    if entry.encrypted:
        payload = encrypt(payload, basename)

    result = {
        "entry_path": entry.path,
        "modified_size": len(plaintext),
        "comp_size": entry.comp_size,
        "orig_size": entry.orig_size,
        "compressed": is_compressed,
        "encrypted": entry.encrypted,
    }

    if dry_run:
        result["action"] = "dry_run"
        return result

    if output_path:
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'wb') as f:
            f.write(payload)
        result["action"] = "written"
        result["output"] = output_path
    else:
        restore_ts = _save_timestamps(entry.paz_file)

        with open(entry.paz_file, 'r+b') as f:
            f.seek(entry.offset)
            f.write(payload)

        restore_ts()
        result["action"] = "patched"
        result["paz_file"] = entry.paz_file
        result["offset"] = f"0x{entry.offset:08X}"

    return result


def repack_entry_bytes(plaintext: bytes, entry: PazEntry,
                       allow_size_change: bool = False) -> tuple[bytes, int, int]:
    """Repack modified file content into the encrypted/compressed payload.

    Args:
        plaintext: decompressed file content
        entry: PAMT entry describing the file slot
        allow_size_change: if True, don't try to match exact comp_size —
            compress as-is and return the actual size. Caller must update PAMT.

    Returns:
        (payload_bytes, actual_comp_size, actual_orig_size) — payload padded
        to entry.comp_size, actual_comp_size is the real compressed data length,
        actual_orig_size is the decompressed content size (may differ from
        entry.orig_size if content grew).
    """
    basename = os.path.basename(entry.path)
    is_compressed = entry.compressed and entry.compression_type == 2
    actual_comp_size = entry.comp_size
    actual_orig_size = entry.orig_size

    if is_compressed:
        if allow_size_change:
            # Compress as-is — never modify or truncate content. If the mod
            # file is larger than orig_size, use the actual size (caller
            # must update orig_size in PAMT).
            if len(plaintext) > entry.orig_size:
                padded = plaintext
                actual_orig_size = len(plaintext)
            else:
                padded = _pad_to_orig_size(plaintext, entry.orig_size)
            compressed = lz4.block.compress(padded, store_size=False)
            actual_comp_size = len(compressed)
            if actual_comp_size > entry.comp_size:
                # Data doesn't fit in the original slot — return it unsized.
                # Caller must append to end of PAZ and update PAMT offset.
                payload = compressed
            elif actual_comp_size < entry.comp_size:
                # Pad with original PAZ tail bytes to fill the slot
                pad_size = entry.comp_size - actual_comp_size
                try:
                    with open(entry.paz_file, 'rb') as f:
                        f.seek(entry.offset + actual_comp_size)
                        original_tail = f.read(pad_size)
                    payload = compressed + original_tail
                except Exception:
                    payload = compressed + b'\x00' * pad_size
            else:
                payload = compressed
        else:
            adjusted = _match_compressed_size(plaintext, entry.comp_size, entry.orig_size)
            compressed = lz4.block.compress(adjusted, store_size=False)
            if len(compressed) != entry.comp_size:
                raise ValueError(
                    f"Size mismatch after compression: {len(compressed)} != {entry.comp_size}")
            payload = compressed
    else:
        if len(plaintext) > entry.comp_size:
            if allow_size_change:
                # Uncompressed file larger than slot — caller must append to PAZ
                actual_comp_size = len(plaintext)
                actual_orig_size = len(plaintext)
                payload = plaintext
            else:
                raise ValueError(
                    f"Modified file ({len(plaintext)} bytes) exceeds budget "
                    f"({entry.comp_size} bytes)")
        else:
            payload = plaintext + b'\x00' * (entry.comp_size - len(plaintext))

    if entry.encrypted:
        payload = encrypt(payload, basename)

    return payload, actual_comp_size, actual_orig_size
