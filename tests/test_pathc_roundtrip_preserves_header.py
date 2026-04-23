"""GH-regression: v3.0.0 broke every texture mod by stamping a hashlittle
self-hash at PATHC offset 4, where vanilla ships zeros.

Commit c6b0e39 ("v3.0.0: PATHC fix") added 7 lines to ``serialize_pathc``
that compute ``hashlittle(data[8:], 0)`` and write it over bytes 4..7,
based on the wrong assumption that PATHC follows PAPGT's self-hash layout.
It does not. JMM's ``ModManager.cs:5200-5252`` ``SerializePathc`` preserves
``Unknown1`` as parsed. Vanilla ``meta/0.pathc`` from a clean install has
``00 00 00 00`` at offset 4.

Every texture-mod apply since v3.0.0 wrote a non-zero stamp into that
field. With 218 preserved / 0 updated / 0 added — the case where the code
thinks it's being a no-op — the apply still corrupts this one 4-byte
region, which is enough to break in-game texture rendering
(minimap icons, etc. — see RoninWoof & AvariceHnt reports).

Round-trip invariant: read vanilla PATHC, re-serialize, bytes must match.
"""
from __future__ import annotations

import struct
from pathlib import Path

from cdumm.archive.pathc_handler import (
    PathcFile, PathcHeader, read_pathc, serialize_pathc,
)


def _synth_vanilla_pathc(tmp_path: Path) -> Path:
    """Build a minimal PATHC with offset-4 zeros, no collisions.

    Mirrors the vanilla-on-disk shape: 2 DDS records, 2 hash entries,
    2 map entries, 0 collisions. Offset 4 is explicitly ``00 00 00 00``.
    """
    dds_record_size = 128
    dds_record_count = 2
    hash_count = 2
    collision_path_count = 0
    collision_blob_size = 0

    unknown0 = 0xDEADBEEF
    unknown1 = 0  # vanilla PATHC has this as zero

    header = struct.pack(
        "<7I",
        unknown0, unknown1,
        dds_record_size, dds_record_count,
        hash_count, collision_path_count, collision_blob_size,
    )
    dds0 = b"DDS " + bytes(dds_record_size - 4)
    dds1 = b"DDS " + b"\x01" + bytes(dds_record_size - 5)
    hashes = struct.pack("<2I", 0x0000_1111, 0x0000_2222)
    map_entries = (
        struct.pack("<5I", 0xFFFF_0000, 100, 0, 0, 0)
        + struct.pack("<5I", 0xFFFF_0001, 200, 0, 0, 0)
    )
    blob = b""

    pathc_bytes = header + dds0 + dds1 + hashes + map_entries + blob

    p = tmp_path / "0.pathc"
    p.write_bytes(pathc_bytes)
    return p


def test_roundtrip_preserves_every_byte(tmp_path: Path) -> None:
    """read_pathc → serialize_pathc must produce byte-identical output.

    If this fails at offset 0x04 with ``orig=00``, the old v3.0.0
    "same pattern as PAPGT" hash stamp is back.
    """
    src = _synth_vanilla_pathc(tmp_path)
    original = src.read_bytes()

    pathc = read_pathc(src)
    reserialized = serialize_pathc(pathc)

    assert len(reserialized) == len(original), (
        f"Size mismatch: orig={len(original)} reserialized={len(reserialized)}")
    assert reserialized == original, (
        "PATHC round-trip corrupted the file. First diff: "
        + ", ".join(
            f"0x{i:04x}:{original[i]:02x}->{reserialized[i]:02x}"
            for i in range(min(len(original), len(reserialized)))
            if original[i] != reserialized[i]
        )[:400]
    )


def test_serialize_preserves_unknown1_as_zero(tmp_path: Path) -> None:
    """Vanilla PATHC has ``unknown1 == 0``. Serialization must keep it."""
    src = _synth_vanilla_pathc(tmp_path)
    pathc = read_pathc(src)
    assert pathc.header.unknown1 == 0

    reserialized = serialize_pathc(pathc)
    roundtripped_unknown1 = struct.unpack_from("<I", reserialized, 4)[0]
    assert roundtripped_unknown1 == 0, (
        f"serialize_pathc overwrote unknown1: expected 0x00000000, "
        f"got 0x{roundtripped_unknown1:08x} — this is the c6b0e39 bug that "
        f"corrupts PATHC on every texture apply")


def test_serialize_preserves_nonzero_unknown1(tmp_path: Path) -> None:
    """If a file happens to carry a non-zero ``unknown1``, keep it as-is.

    We never compute or overwrite that field. Round-trip is the contract.
    """
    src = _synth_vanilla_pathc(tmp_path)
    raw = bytearray(src.read_bytes())
    raw[4:8] = struct.pack("<I", 0xCAFEBABE)
    src.write_bytes(bytes(raw))

    pathc = read_pathc(src)
    reserialized = serialize_pathc(pathc)
    assert struct.unpack_from("<I", reserialized, 4)[0] == 0xCAFEBABE
