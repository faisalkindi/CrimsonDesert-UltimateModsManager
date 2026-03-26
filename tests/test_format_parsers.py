import struct

from cdmm.archive.format_parsers.pabgb_parser import identify_pabgb_records
from cdmm.archive.format_parsers.paac_parser import identify_paac_records
from cdmm.archive.format_parsers.pamt_parser import identify_pamt_records
from cdmm.archive.format_parsers.base import identify_records_for_file


def _make_pabgb_data(count: int, record_size: int = 64) -> bytes:
    """Create fake pabgb data with count records."""
    # Header: u16 count + N x (u32 hash, u32 offset)
    header = struct.pack("<H", count)
    body_start = 2 + count * 8
    for i in range(count):
        offset = body_start + i * record_size
        header += struct.pack("<II", 0xDEAD0000 + i, offset)
    body = b"\x00" * (count * record_size)
    return header + body


def test_pabgb_identifies_record() -> None:
    data = _make_pabgb_data(10, record_size=64)
    body_start = 2 + 10 * 8  # 82
    # Byte 100 should fall in record 0 (offset 82, ends at 146)
    result = identify_pabgb_records(data, 100, 110)
    assert result is not None
    assert "record 0" in result


def test_pabgb_identifies_later_record() -> None:
    data = _make_pabgb_data(10, record_size=64)
    body_start = 2 + 10 * 8  # 82
    # Record 5 starts at 82 + 5*64 = 402
    result = identify_pabgb_records(data, 410, 420)
    assert result is not None
    assert "5" in result


def test_pabgb_too_small() -> None:
    assert identify_pabgb_records(b"\x00\x00", 0, 1) is None


def test_paac_identifies_header() -> None:
    # Fake paac: 68-byte header + some data
    data = struct.pack("<I", 50) + b"\x00" * 64 + b"M0%D" + b"\x00" * 100
    result = identify_paac_records(data, 10, 20)
    assert result is not None
    assert "header" in result


def test_paac_identifies_state_record() -> None:
    header = struct.pack("<I", 50) + b"\x00" * 64
    # Place M0%D markers
    record1 = b"M0%D" + b"\x00" * 100
    record2 = b"M0%D" + b"\x00" * 100
    data = header + record1 + record2

    # Byte in second record region
    pos = 68 + 104 + 10
    result = identify_paac_records(data, pos, pos + 5)
    assert result is not None
    assert "state record" in result


def test_paac_too_small() -> None:
    assert identify_paac_records(b"\x00" * 10, 0, 1) is None


def test_pamt_identifies_hash_field() -> None:
    data = b"\x00" * 200
    result = identify_pamt_records(data, 0, 4)
    assert result is not None
    assert "hash" in result.lower()


def test_pamt_identifies_paz_count() -> None:
    data = struct.pack("<III", 0xABCD, 5, 0x610E0232) + b"\x00" * 200
    result = identify_pamt_records(data, 4, 8)
    assert result is not None
    assert "PAZ count" in result


def test_pamt_identifies_paz_entry() -> None:
    data = struct.pack("<III", 0xABCD, 5, 0x610E0232) + b"\x00" * 200
    result = identify_pamt_records(data, 16, 24)
    assert result is not None
    assert "PAZ entry 0" in result


def test_pamt_identifies_later_entry() -> None:
    data = struct.pack("<III", 0xABCD, 5, 0x610E0232) + b"\x00" * 200
    # Entry 1 starts at offset 24, each subsequent is 12 bytes
    result = identify_pamt_records(data, 30, 36)
    assert result is not None
    assert "entry" in result.lower()


def test_pamt_identifies_file_records() -> None:
    data = struct.pack("<III", 0xABCD, 2, 0x610E0232) + b"\x00" * 500
    # Well past the PAZ table
    result = identify_pamt_records(data, 400, 410)
    assert result is not None
    assert "file records" in result.lower()


def test_base_dispatcher_unknown_format() -> None:
    result = identify_records_for_file("0008/0.paz", 100, 200, b"\x00" * 500)
    assert result is None  # .paz is not directly parseable


def test_base_dispatcher_no_data() -> None:
    result = identify_records_for_file("test.pabgb", 0, 10, None)
    assert result is None
