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

import lz4.block

from cdumm.archive.paz_parse import PazEntry
from cdumm.archive.paz_crypto import encrypt, lz4_compress


# ── DDS header constants ──────────────────────────────────────────────

# Block compression sizes by FourCC
_BC_BLOCK_BYTES = {
    b"DXT1": 8, b"ATI1": 8, b"BC4U": 8, b"BC4S": 8,
    b"DXT3": 16, b"DXT5": 16, b"ATI2": 16, b"BC5U": 16, b"BC5S": 16,
    b"DXT2": 16, b"DXT4": 16,
}

# "Last4" format identifier at DDS header byte 124 (game-specific)
_DDS_LAST4_BY_FOURCC = {
    b"DXT1": 12, b"DXT2": 15, b"DXT3": 15, b"DXT4": 15, b"DXT5": 15,
    b"ATI1": 4, b"ATI2": 4, b"BC4U": 4, b"BC4S": 4, b"BC5U": 4, b"BC5S": 4,
}

# DXGI format -> last4
_DDS_LAST4_BY_DXGI = {
    71: 12, 72: 12,  # BC1
    74: 15, 75: 15,  # BC2
    77: 15, 78: 15,  # BC3
    80: 4, 81: 4,    # BC4
    83: 4, 84: 4,    # BC5
    95: 4, 96: 4,    # BC6H
    98: 15, 99: 15,  # BC7
}


def fix_dds_header(header: bytearray, compressed_body_size: int = 0) -> bytearray:
    """Fix a DDS header to match what the game engine expects.

    Mod tools (GIMP, Paint.NET) export DDS with non-standard header values.
    The game's PAZ DDS loader uses specific fields:
    - Flags must include 0x20000
    - Depth must be >= 1
    - Reserved1 offsets 32-47: [compressed_body_size, decompressed_body_size,
      mip1_size, mip2_size] for single-mip-chunk textures
    - Byte 124-127: format-specific "last4" identifier

    Args:
        header: 128-byte DDS header to fix (modified in place)
        compressed_body_size: LZ4 compressed size of mip0 body (set after compression)
    """
    if len(header) < 128 or header[:4] != b"DDS ":
        return header

    h = bytearray(header)

    # Fix flags: ensure 0x20000 is set
    flags = struct.unpack_from("<I", h, 8)[0]
    flags |= 0x20000
    struct.pack_into("<I", h, 8, flags)

    # Fix depth: must be >= 1
    depth = struct.unpack_from("<I", h, 24)[0]
    if depth == 0:
        struct.pack_into("<I", h, 24, 1)

    # Compute mip chain sizes for reserved1 area
    height = struct.unpack_from("<I", h, 12)[0]
    width = struct.unpack_from("<I", h, 16)[0]
    mips = max(1, struct.unpack_from("<I", h, 28)[0])
    fourcc = bytes(h[84:88])

    dxgi = None
    if fourcc == b"DX10":
        dxgi = struct.unpack_from("<I", h, 128)[0] if len(h) >= 132 else None

    block_bytes = _BC_BLOCK_BYTES.get(fourcc)
    if block_bytes is None and dxgi is not None:
        if dxgi in (70, 71, 72, 79, 80, 81):
            block_bytes = 8
        elif dxgi in (73, 74, 75, 76, 77, 78, 82, 83, 84, 94, 95, 96, 97, 98, 99):
            block_bytes = 16

    if block_bytes:
        # Compute decompressed mip sizes
        mip_sizes = []
        cw, ch = max(1, width), max(1, height)
        for i in range(min(4, mips)):
            size = max(1, (cw + 3) // 4) * max(1, (ch + 3) // 4) * block_bytes
            mip_sizes.append(size)
            cw, ch = max(1, cw // 2), max(1, ch // 2)
        while len(mip_sizes) < 4:
            mip_sizes.append(0)

        # Reserved1 layout: [comp_body_size, decomp_body_size, mip1_size, mip2_size]
        struct.pack_into("<I", h, 32, compressed_body_size)
        struct.pack_into("<I", h, 36, mip_sizes[0])  # decompressed mip0
        struct.pack_into("<I", h, 40, mip_sizes[1] if len(mip_sizes) > 1 else 0)
        struct.pack_into("<I", h, 44, mip_sizes[2] if len(mip_sizes) > 2 else 0)
        # Zero remaining reserved1 (offsets 48-75)
        for off in range(48, 76, 4):
            struct.pack_into("<I", h, off, 0)

    # Fix "last4" format identifier at byte 124
    last4 = _DDS_LAST4_BY_FOURCC.get(fourcc)
    if last4 is None and dxgi is not None:
        last4 = _DDS_LAST4_BY_DXGI.get(dxgi)
    if last4 is not None:
        struct.pack_into("<I", h, 124, last4)

    return h


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
    is_dds_split = entry.compression_type == 1  # 128-byte header + LZ4 body
    # DDS type 0x01 always uses inner LZ4 even when comp_size == orig_size
    # (the padded payload matches orig_size, actual LZ4 size is in header[32])
    is_compressed = is_dds_split or (entry.compressed and entry.compression_type == 2)
    DDS_HEADER_SIZE = 128
    actual_comp_size = entry.comp_size
    actual_orig_size = entry.orig_size

    if is_compressed:
        if is_dds_split:
            # Type 0x01: 128-byte DDS header (raw) + LZ4 compressed body
            header = bytearray(plaintext[:DDS_HEADER_SIZE])
            body = plaintext[DDS_HEADER_SIZE:]
            body_orig = entry.orig_size - DDS_HEADER_SIZE
            body_comp_budget = entry.comp_size - DDS_HEADER_SIZE

            if allow_size_change:
                if len(body) > body_orig:
                    padded_body = body
                    actual_orig_size = DDS_HEADER_SIZE + len(body)
                else:
                    padded_body = _pad_to_orig_size(body, body_orig)
                compressed_body = lz4.block.compress(padded_body, store_size=False)

                # Fix DDS header with the now-known compressed body size
                if header[:4] == b"DDS ":
                    header = fix_dds_header(header, len(compressed_body))

                # DDS type 0x01: game reads compressed body size from header[32:36],
                # not from PAMT comp_size. Set comp_size = orig_size (padded to full
                # DDS size with zeros) so the PAMT entry looks "uncompressed" to the
                # game's outer reader. The inner LZ4 decompression uses header[32:36].
                full_size = DDS_HEADER_SIZE + len(padded_body)
                payload_core = header + compressed_body
                if len(payload_core) < full_size:
                    payload = payload_core + b'\x00' * (full_size - len(payload_core))
                else:
                    payload = payload_core
                actual_comp_size = full_size
                actual_orig_size = full_size
            else:
                adjusted_body = _match_compressed_size(
                    body, body_comp_budget, body_orig)
                compressed_body = lz4.block.compress(adjusted_body, store_size=False)
                if len(compressed_body) != body_comp_budget:
                    raise ValueError(
                        f"DDS body size mismatch: {len(compressed_body)} != {body_comp_budget}")
                payload = header + compressed_body
        elif allow_size_change:
            # Type 0x02: fully LZ4 compressed
            # Always use the actual content size — padding with nulls causes
            # crashes for XML/CSS files whose parsers choke on null bytes.
            actual_orig_size = len(plaintext)
            compressed = lz4.block.compress(plaintext, store_size=False)
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
            compressed = lz4.block.compress(adjusted, store_size=False)
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
