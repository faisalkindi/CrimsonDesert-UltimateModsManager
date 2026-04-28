"""BC7 single-mip DDS pack: must use raw passthrough, not split-header.

Bug from BANDU on Nexus 2026-04-28: BC7 textures from mods like
Crimson Desert ID 1458 produce rainbow noise in-game after CDUMM
applies them.

Root cause traced by inspecting the user's actual mod source folder:
  * BC7 DDS files use a 148-byte header (128-byte standard +
    20-byte DX10 extension carrying the DXGI format).
  * `_build_dds_partial_payload` in `overlay_builder.py:128`
    correctly writes the 148-byte header, then an LZ4-compressed
    first mip.
  * But the resulting PAMT entry is flagged `comp_type=1`, which
    the game's PAZ loader reads as "128-byte header + LZ4 body".
  * 20-byte mismatch → bytes 128-147 (DX10 extension) get
    interpreted by the loader as the start of the body. Result:
    rainbow-noise pixel decoding offset by 20 bytes.

Vanilla 0012/0.pamt's BC7 entries (e.g. `material_noise_common.dds`
with 11 mips) take the raw-passthrough branch in `paz_repack.py:264`
and work correctly because the on-disk bytes ARE the DDS file
unchanged — no inner-LZ4 split.

That branch's guard `mip_count > 1` is wrong: it excludes single-
mip BC7 (UI elements like `cd_hud_stamina_00.dds` in the user's
mod). Single-mip BC7 must take the same raw passthrough.
"""
from __future__ import annotations

import struct

from cdumm.archive.paz_parse import PazEntry
from cdumm.archive.paz_repack import repack_entry_bytes


def _make_bc7_dx10_dds(width: int, height: int, mip_count: int) -> bytes:
    """Synthesize a minimal valid BC7 (DXGI 98 = BC7_UNORM) DDS file.

    Layout: 128-byte standard DDS header + 20-byte DX10 extension
    + first-mip pixel data (BC7 = 16 bytes per 4x4 block).
    """
    header = bytearray(148)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)  # dwSize
    struct.pack_into("<I", header, 8, 0x000A_1007)  # flags
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 28, mip_count)
    # FourCC at offset 84 = "DX10" forces the 148-byte extended header.
    header[84:88] = b"DX10"
    # DX10 extension at offset 128: dxgiFormat=98 (BC7_UNORM)
    struct.pack_into("<I", header, 128, 98)  # DXGI_FORMAT_BC7_UNORM
    struct.pack_into("<I", header, 132, 3)   # resourceDimension = TEXTURE2D
    struct.pack_into("<I", header, 136, 0)   # miscFlag
    struct.pack_into("<I", header, 140, 1)   # arraySize
    struct.pack_into("<I", header, 144, 0)   # miscFlags2

    # First-mip BC7 size = ceil(w/4) * ceil(h/4) * 16
    block_w = max(1, (width + 3) // 4)
    block_h = max(1, (height + 3) // 4)
    first_mip_size = block_w * block_h * 16

    # Use a recognisable byte pattern so we can verify it survives.
    pixels = bytes(((i * 7 + 13) & 0xFF) for i in range(first_mip_size))
    return bytes(header) + pixels


def _make_entry(comp_size: int, orig_size: int) -> PazEntry:
    """Build a PazEntry that flags as comp_type=1 (DDS split)."""
    return PazEntry(
        path="ui/test_bc7_single_mip.dds",
        paz_file="x.paz",
        offset=0,
        comp_size=comp_size,
        orig_size=orig_size,
        flags=0x0001_0000,  # comp_type nibble = 1
        paz_index=0,
    )


def test_bc7_single_mip_dx10_uses_raw_passthrough():
    """Single-mip BC7 DDS must repack as raw bytes (passthrough),
    not as a 128-byte-header + LZ4-body layout. That layout
    mismatches the game's loader and produces rainbow noise."""
    dds = _make_bc7_dx10_dds(width=64, height=64, mip_count=1)
    entry = _make_entry(comp_size=len(dds), orig_size=len(dds))

    payload, comp, orig = repack_entry_bytes(
        dds, entry, allow_size_change=True)

    # The output must equal the input bytes exactly. This proves:
    #  (a) the 148-byte DX10 header survived intact (no truncation
    #      to 128 bytes), and
    #  (b) the body wasn't LZ4-compressed and re-padded — the game
    #      will read the bytes raw, see the correct DX10 extension
    #      at offset 128, and decode pixels starting at offset 148.
    assert payload == dds, (
        f"Single-mip BC7 must round-trip raw. Got payload of "
        f"{len(payload)} bytes vs input {len(dds)}. First 16 bytes "
        f"in: {dds[:16].hex()} out: {payload[:16].hex()}.")
    assert comp == len(dds)
    assert orig == len(dds)


def test_bc7_multi_mip_still_works():
    """Regression guard: the existing multi-mip BC7 raw-passthrough
    case must keep working. mod_1555/material_noise_common.dds
    (11 mips, 1024x1024) is the reference."""
    dds = _make_bc7_dx10_dds(width=64, height=64, mip_count=4)
    entry = _make_entry(comp_size=len(dds), orig_size=len(dds))

    payload, comp, orig = repack_entry_bytes(
        dds, entry, allow_size_change=True)

    assert payload == dds


def test_dx10_dxgi97_bc7_typeless_also_passthrough():
    """BC7_TYPELESS (DXGI=97) — same raw-passthrough requirement."""
    dds = bytearray(_make_bc7_dx10_dds(width=32, height=32, mip_count=1))
    struct.pack_into("<I", dds, 128, 97)  # BC7_TYPELESS
    dds_bytes = bytes(dds)
    entry = _make_entry(comp_size=len(dds_bytes), orig_size=len(dds_bytes))

    payload, _, _ = repack_entry_bytes(
        dds_bytes, entry, allow_size_change=True)
    assert payload == dds_bytes


def test_dx10_dxgi99_bc7_unorm_srgb_also_passthrough():
    """BC7_UNORM_SRGB (DXGI=99) — same raw-passthrough requirement."""
    dds = bytearray(_make_bc7_dx10_dds(width=32, height=32, mip_count=1))
    struct.pack_into("<I", dds, 128, 99)  # BC7_UNORM_SRGB
    dds_bytes = bytes(dds)
    entry = _make_entry(comp_size=len(dds_bytes), orig_size=len(dds_bytes))

    payload, _, _ = repack_entry_bytes(
        dds_bytes, entry, allow_size_change=True)
    assert payload == dds_bytes


def test_non_dx10_dds_still_uses_partial_payload():
    """Regression guard: standard non-DX10 DDS (e.g. DXT5) keeps
    going through the inner-LZ4 partial payload path. We don't want
    to regress small DXT5 textures by accidentally widening the
    raw-passthrough branch."""
    # Synthesize a minimal DXT5 DDS (128-byte header, fourcc DXT5).
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<I", header, 8, 0x000A_1007)
    struct.pack_into("<I", header, 12, 64)
    struct.pack_into("<I", header, 16, 64)
    struct.pack_into("<I", header, 28, 1)
    header[84:88] = b"DXT5"  # NOT DX10 — must take partial-payload branch
    pixels = bytes(((i * 7 + 13) & 0xFF) for i in range(16 * 16 * 16))
    dds = bytes(header) + pixels
    entry = _make_entry(comp_size=len(dds), orig_size=len(dds))

    payload, _, _ = repack_entry_bytes(dds, entry, allow_size_change=True)
    # DXT5 with allow_size_change goes through _build_dds_partial_payload,
    # which writes a `last4` value at offset 124 (15 for DXT5). Raw
    # passthrough wouldn't touch byte 124. So payload[124:128] should
    # be the patched last4, NOT the original header bytes.
    assert payload[124:128] == bytes([15, 0, 0, 0]), (
        "DXT5 must still go through partial-payload (which patches "
        "last4 at offset 124). Raw passthrough would skip that step.")
