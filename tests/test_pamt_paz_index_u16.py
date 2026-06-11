"""PAZ chunk id is a u16, not a u8 (audit finding 1).

The vendored RE defines the PAMT file-record chunk_id as a u16 in the
low 16 bits of the flags dword. parse_pamt masked it with 0xFF, so any
directory with more than 256 PAZ files resolved entries 256+ to the
wrong archive (index 300 silently became 44).
"""
from __future__ import annotations

from pathlib import Path

from tests.pamt_synth import build_pamt

from cdumm.archive.paz_parse import parse_pamt


def _write_pamt(tmp_path: Path, entries: list[dict]) -> Path:
    pamt = tmp_path / "0.pamt"
    pamt.write_bytes(build_pamt(entries))
    return pamt


def test_paz_index_300_not_truncated(tmp_path: Path):
    pamt = _write_pamt(tmp_path, [{
        "name": "big.bin", "offset": 0, "comp_size": 4,
        "orig_size": 4, "flags": 300,
    }])
    entries = parse_pamt(str(pamt), paz_dir=str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.paz_index == 300, (
        f"paz_index truncated to {e.paz_index}; flags low 16 bits are "
        f"the u16 chunk id, masking with 0xFF resolves the wrong PAZ")
    # pamt stem is 0, so entry must point at 300.paz, not 44.paz
    assert Path(e.paz_file).name == "300.paz"


def test_paz_index_u16_preserves_compression_type(tmp_path: Path):
    flags = 300 | (2 << 16)  # chunk 300, LZ4
    pamt = _write_pamt(tmp_path, [{
        "name": "data.bin", "offset": 16, "comp_size": 8,
        "orig_size": 32, "flags": flags,
    }])
    e = parse_pamt(str(pamt), paz_dir=str(tmp_path))[0]
    assert e.paz_index == 300
    assert e.compression_type == 2
    assert e.compressed is True


def test_small_paz_index_unchanged(tmp_path: Path):
    pamt = _write_pamt(tmp_path, [{
        "name": "small.bin", "offset": 0, "comp_size": 4,
        "orig_size": 4, "flags": 7,
    }])
    e = parse_pamt(str(pamt), paz_dir=str(tmp_path))[0]
    assert e.paz_index == 7
    assert Path(e.paz_file).name == "7.paz"
