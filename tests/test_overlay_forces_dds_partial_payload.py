"""RoninWoof/AvariceHnt/Caites regression (GH-minimap-tweaks):

Texture mods that replace DDS entries whose vanilla representation was
stored uncompressed (``comp_size == decomp_size``) were importing with
``compression_type: 0`` in the ENTR metadata. CDUMM's overlay builder
routed them through the raw-passthrough branch (flags=0, no reserved1
stamp, no last4 patch), which the game cannot render.

JMM ``ModManager.cs:1769`` forces DDS-by-extension to flags=1 at
extraction time, guaranteeing the partial-DDS payload path in the
overlay builder. CDUMM must match: the overlay encoding for any ``.dds``
entry must always be the partial-DDS format (comp_type=1, flags=1,
reserved1 stamped with m-values, reserved2/last4 patched).

Validates: building an overlay PAZ with a synthetic DDS entry whose
metadata says ``compression_type: 0`` (the vanilla-no-outer-compression
case) still produces an OverlayEntry with flags=1 and populated
dds_m_values — proving the partial-payload branch ran regardless of the
metadata's compression_type hint.
"""
from __future__ import annotations

import struct
from pathlib import Path

from cdumm.archive.overlay_builder import build_overlay


def _make_dds_bc1(width: int = 32, height: int = 32) -> bytes:
    """Synthesize a small DXT1/BC1 DDS file. 32x32 BC1 = 8 blocks of 8x8
    groups of 4x4 = 64 blocks * 8 bytes = 512 bytes of data plus one mip.
    """
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    flags = 0x0002_1007
    struct.pack_into("<I", header, 8, flags)
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 20, max(width, 1) * max(height, 1) // 2)
    struct.pack_into("<I", header, 28, 1)
    struct.pack_into("<I", header, 76, 32)
    struct.pack_into("<I", header, 80, 0x04)
    header[84:88] = b"DXT1"
    mip0 = bytes([0xAB, 0xCD] * 256)
    return bytes(header) + mip0


def test_overlay_builder_forces_dds_partial_payload_on_vanilla_comp_type_zero(
    tmp_path: Path,
) -> None:
    content = _make_dds_bc1()
    metadata = {
        "entry_path": "ui/cd_icon_map_enemy_die_4.dds",
        "pamt_dir": "0012",
        "compression_type": 0,
        "vanilla_flags": 3,
        "encrypted": False,
    }
    entries = [(content, metadata)]

    paz_bytes, pamt_bytes, overlay_entries = build_overlay(
        entries,
        game_dir=None,
        vanilla_pathc_path=None,
    )

    assert len(overlay_entries) == 1
    oe = overlay_entries[0]

    assert oe.flags == 1, (
        f"DDS overlay entry must have flags=1 (partial-DDS payload), "
        f"got flags=0x{oe.flags:02X}. metadata said compression_type=0 "
        f"but JMM ModManager.cs:1769 forces flags=1 for any .dds file.")
    assert oe.dds_m_values is not None, (
        "dds_m_values must be populated — that's the proof that "
        "_build_dds_partial_payload ran and stamped reserved1 at offset 32")
    assert any(m != 0 for m in oe.dds_m_values), (
        f"m-values are all zero ({oe.dds_m_values}) — partial payload did "
        "not run, or ran but on a DDS the BC helper doesn't recognize")


def test_overlay_non_dds_still_infers_from_extension(tmp_path: Path) -> None:
    """Regression guard: forcing DDS to comp_type=1 must not break the
    existing path for non-DDS entries — they should still use whatever
    the metadata says, or fall back to extension-based inference.

    Test a .html entry (minimap UI overlay file): default inference is
    comp_type=2 (LZ4). Metadata says 2. Overlay should compress.
    """
    html = b"<html><body>hi</body></html>" * 100
    metadata = {
        "entry_path": "ui/minimaphudview2.html",
        "pamt_dir": "0012",
        "compression_type": 2,
        "encrypted": False,
    }
    entries = [(html, metadata)]

    _, _, overlay_entries = build_overlay(
        entries,
        game_dir=None,
        vanilla_pathc_path=None,
    )

    assert len(overlay_entries) == 1
    oe = overlay_entries[0]
    assert oe.flags == 2, f"html should be LZ4-compressed (flags=2), got {oe.flags}"
