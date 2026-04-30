"""Round-trip test: parse a vanilla dropsetinfo record, re-serialize,
verify byte-equal output.

Reference: NattKh's CrimsonGameMods/dropset_editor.py
(github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS).
NattKh's tool generates the Format 3 mods we want to apply, so its
decoder + serializer are the canonical reference for the drop-entry
binary layout.

If this test fails after a change to dropset_writer, we've drifted
from the upstream layout and the mods won't apply correctly.
"""
from __future__ import annotations
import struct
from pathlib import Path

import pytest


_VANILLA_PABGB = Path(r"C:\Users\faisa\AppData\Local\Temp\dropsetinfo.pabgb")
_VANILLA_PABGH = Path(r"C:\Users\faisa\AppData\Local\Temp\dropsetinfo.pabgh")


def _have_vanilla() -> bool:
    return _VANILLA_PABGB.exists() and _VANILLA_PABGH.exists()


@pytest.mark.skipif(not _have_vanilla(),
                    reason="vanilla dropsetinfo extract not present")
def test_parse_dropset_record_byte_roundtrip():
    """Parse one vanilla DropSet record, serialize back, expect byte
    equality."""
    from cdumm.engine.dropset_writer import (
        parse_dropset_record, serialize_dropset_record,
    )

    pabgb = _VANILLA_PABGB.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()

    # PABGH: 2-byte count, then count*(4-byte key, 4-byte offset).
    # NattKh's loader uses count_size=2 for dropsetinfo specifically.
    count = struct.unpack_from("<H", pabgh, 0)[0]
    records = []
    for i in range(count):
        off = 2 + i * 8
        key, body_off = struct.unpack_from("<II", pabgh, off)
        records.append((key, body_off))

    # Sort by body offset to compute end boundaries.
    sorted_records = sorted(records, key=lambda r: r[1])
    boundaries = {}
    for i, (k, o) in enumerate(sorted_records):
        end = sorted_records[i + 1][1] if i + 1 < len(sorted_records) else len(pabgb)
        boundaries[k] = (o, end)

    # Pick DropSet_Faction_Graymane (key 175001) as the target.
    key = 175001
    body_off, body_end = boundaries[key]
    record_bytes = pabgb[body_off:body_end]

    parsed = parse_dropset_record(record_bytes)
    assert parsed.key == key, f"Expected key {key}, got {parsed.key}"
    assert parsed.name == "DropSet_Faction_Graymane", parsed.name
    assert len(parsed.drops) > 0, "Record must have at least one drop"

    re_encoded = serialize_dropset_record(parsed)
    assert re_encoded == record_bytes, (
        f"Round-trip byte mismatch on key {key}.\n"
        f"Original:  {len(record_bytes)} bytes\n"
        f"Re-encoded: {len(re_encoded)} bytes\n"
        f"First diff index: "
        f"{next((i for i, (a, b) in enumerate(zip(record_bytes, re_encoded)) if a != b), -1)}"
    )


@pytest.mark.skipif(not _have_vanilla(),
                    reason="vanilla dropsetinfo extract not present")
def test_parse_first_50_records_byte_roundtrip():
    """Sweep the first 50 records to catch layout edge cases (different
    `unk4` tagged variants, empty drops lists, etc)."""
    from cdumm.engine.dropset_writer import (
        parse_dropset_record, serialize_dropset_record,
    )

    pabgb = _VANILLA_PABGB.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    count = struct.unpack_from("<H", pabgh, 0)[0]
    records = []
    for i in range(count):
        off = 2 + i * 8
        key, body_off = struct.unpack_from("<II", pabgh, off)
        records.append((key, body_off))

    sorted_records = sorted(records, key=lambda r: r[1])
    failures: list[tuple[int, str]] = []
    for i, (k, o) in enumerate(sorted_records[:50]):
        end = sorted_records[i + 1][1] if i + 1 < len(sorted_records) else len(pabgb)
        rec = pabgb[o:end]
        try:
            parsed = parse_dropset_record(rec)
            re_enc = serialize_dropset_record(parsed)
            if re_enc != rec:
                failures.append((k,
                    f"size_orig={len(rec)} size_new={len(re_enc)} "
                    f"first_diff={next((j for j,(a,b) in enumerate(zip(rec, re_enc)) if a != b), -1)}"))
        except Exception as e:
            failures.append((k, f"exception: {e}"))
    assert not failures, (
        f"{len(failures)} of 50 records failed round-trip:\n"
        + "\n".join(f"  key={k}: {msg}" for k, msg in failures[:10])
    )
