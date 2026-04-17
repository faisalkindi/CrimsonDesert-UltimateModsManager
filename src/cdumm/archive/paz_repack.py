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
import struct
import sys

from cdumm.archive.paz_parse import PazEntry
from cdumm.archive.paz_crypto import encrypt, lz4_compress


# The legacy ``fix_dds_header`` helper and its FourCC / DXGI lookup tables
# used to live here. They are superseded by the JMM-parity ``BuildPartial-
# DdsPayload`` port inside ``overlay_builder.py`` (which is also what
# ``repack_entry_bytes`` below delegates to for DDS bodies). Removing the
# dead code prevents parity drift from accidentally re-introducing the
# wrong whole-body-LZ4 layout in the Crimson Browser in-place path.


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

    comp = lz4_compress(padded)
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
            c = lz4_compress(trial)
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
            c = lz4_compress(trial)
            if len(c) == target_comp_size:
                return trial

    if len(comp) > target_comp_size:
        raise ValueError(
            f"Compressed size {len(comp)} exceeds target {target_comp_size}. "
            f"Reduce file content.")

    raise ValueError(
        f"Cannot match target comp_size {target_comp_size} "
        f"(best: {len(lz4_compress(padded))})")


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
    comp = lz4_compress(padded)
    if len(comp) <= target_comp:
        return padded

    # More aggressive: collapse multiple spaces/newlines
    import re
    stripped = re.sub(r'[ \t]+', ' ', stripped)
    stripped = re.sub(r'\n{3,}', '\n\n', stripped)
    candidate = stripped.encode('utf-8')
    padded = _pad_to_orig_size(candidate, target_orig)
    comp = lz4_compress(padded)
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
        compressed = lz4_compress(adjusted)
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
    is_dds_split = entry.compression_type == 1  # 128-byte header + LZ4 body
    # DDS type 0x01 always uses inner LZ4 even when comp_size == orig_size
    # (the padded payload matches orig_size, actual LZ4 size is in header[32])
    is_compressed = is_dds_split or (entry.compressed and entry.compression_type == 2)
    DDS_HEADER_SIZE = 128
    actual_comp_size = entry.comp_size
    actual_orig_size = entry.orig_size

    if is_compressed:
        if is_dds_split:
            # Check if DX10 multi-mip (raw passthrough) or standard (inner LZ4)
            fourcc = plaintext[84:88] if len(plaintext) >= 88 else b""
            is_dx10 = fourcc == b"DX10" and len(plaintext) >= 148
            mip_count = max(1, struct.unpack_from("<I", plaintext, 28)[0]) if len(plaintext) >= 32 else 1

            if allow_size_change and is_dx10 and mip_count > 1:
                # DX10 multi-mip: raw passthrough, no compression
                payload = plaintext
                actual_comp_size = len(payload)
                actual_orig_size = len(payload)
            elif allow_size_change:
                # Standard DDS: build payload using JMM's BuildPartialDdsPayload
                # (inner-LZ4 first mip or multi-chunk raw), then pad to the
                # original orig_size so the entry slot stays the same size.
                # Importing lazily to avoid a circular import between
                # paz_repack -> overlay_builder -> paz_repack.
                from cdumm.archive.overlay_builder import (
                    _build_dds_partial_payload, _get_dds_format_last4,
                )
                partial, _m = _build_dds_partial_payload(plaintext)
                target_len = max(len(partial), entry.orig_size)
                buf = bytearray(target_len)
                buf[:len(partial)] = partial
                # last4 from format lookup only (no vanilla PATHC handle here).
                last4 = _get_dds_format_last4(plaintext)
                if last4 and len(buf) >= 128:
                    struct.pack_into("<I", buf, 124, last4)
                payload = bytes(buf)
                actual_comp_size = len(payload)
                actual_orig_size = len(payload)
            else:
                # DDS split without size change: recompress to exact original comp_size
                header = bytearray(plaintext[:DDS_HEADER_SIZE])
                body = plaintext[DDS_HEADER_SIZE:]
                body_orig = entry.orig_size - DDS_HEADER_SIZE
                body_comp_budget = entry.comp_size - DDS_HEADER_SIZE
                adjusted_body = _match_compressed_size(
                    body, body_comp_budget, body_orig)
                compressed_body = lz4_compress(adjusted_body)
                if len(compressed_body) != body_comp_budget:
                    raise ValueError(
                        f"DDS body size mismatch: {len(compressed_body)} != {body_comp_budget}")
                payload = bytes(header) + compressed_body
        elif allow_size_change:
            # Type 0x02: fully LZ4 compressed
            # Always use the actual content size — padding with nulls causes
            # crashes for XML/CSS files whose parsers choke on null bytes.
            actual_orig_size = len(plaintext)
            compressed = lz4_compress(plaintext)
            actual_comp_size = len(compressed)
            if actual_comp_size > entry.comp_size:
                payload = compressed
            elif actual_comp_size < entry.comp_size:
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
            compressed = lz4_compress(adjusted)
            if len(compressed) != entry.comp_size:
                raise ValueError(
                    f"Size mismatch after compression: {len(compressed)} != {entry.comp_size}")
            payload = compressed
    else:
        if allow_size_change:
            # Use actual content size — no null padding that could corrupt text files
            actual_comp_size = len(plaintext)
            actual_orig_size = len(plaintext)
            if len(plaintext) <= entry.comp_size:
                # Fits in existing slot — pad to fill but set actual sizes correctly
                payload = plaintext + b'\x00' * (entry.comp_size - len(plaintext))
            else:
                # Larger than slot — caller must append to PAZ
                payload = plaintext
        elif len(plaintext) > entry.comp_size:
            raise ValueError(
                f"Modified file ({len(plaintext)} bytes) exceeds budget "
                f"({entry.comp_size} bytes)")
        else:
            payload = plaintext + b'\x00' * (entry.comp_size - len(plaintext))

    if entry.encrypted:
        payload = encrypt(payload, basename)

    return payload, actual_comp_size, actual_orig_size
