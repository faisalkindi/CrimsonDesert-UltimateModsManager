from pathlib import Path

from cdumm.engine.delta_engine import (
    apply_delta,
    generate_delta,
    get_changed_byte_ranges,
    load_delta,
    save_delta,
)


def test_generate_and_apply_delta() -> None:
    vanilla = b"Hello World! This is the original file content." * 100
    modified = bytearray(vanilla)
    modified[10:15] = b"XXXXX"
    modified = bytes(modified)

    delta = generate_delta(vanilla, modified)
    assert len(delta) > 0
    assert len(delta) < len(vanilla)  # Delta should be much smaller

    result = apply_delta(vanilla, delta)
    assert result == modified


def test_delta_identical_files() -> None:
    data = b"identical content" * 50
    delta = generate_delta(data, data)
    result = apply_delta(data, delta)
    assert result == data


def test_get_changed_byte_ranges_single_change() -> None:
    vanilla = b"\x00" * 100
    modified = bytearray(vanilla)
    modified[20:30] = b"\xFF" * 10
    modified = bytes(modified)

    ranges = get_changed_byte_ranges(vanilla, modified)
    assert len(ranges) == 1
    assert ranges[0] == (20, 30)


def test_get_changed_byte_ranges_multiple_changes() -> None:
    vanilla = b"\x00" * 100
    modified = bytearray(vanilla)
    modified[10:15] = b"\xFF" * 5
    modified[50:60] = b"\xAA" * 10
    modified = bytes(modified)

    ranges = get_changed_byte_ranges(vanilla, modified)
    assert len(ranges) == 2
    assert ranges[0] == (10, 15)
    assert ranges[1] == (50, 60)


def test_get_changed_byte_ranges_no_changes() -> None:
    data = b"\x00" * 100
    ranges = get_changed_byte_ranges(data, data)
    assert len(ranges) == 0


def test_get_changed_byte_ranges_size_increase() -> None:
    vanilla = b"\x00" * 100
    modified = b"\x00" * 150

    ranges = get_changed_byte_ranges(vanilla, modified)
    assert (100, 150) in ranges


def test_get_changed_byte_ranges_size_decrease() -> None:
    vanilla = b"\x00" * 150
    modified = b"\x00" * 100

    ranges = get_changed_byte_ranges(vanilla, modified)
    assert (100, 150) in ranges


def test_save_and_load_delta(tmp_path: Path) -> None:
    delta_bytes = b"FAKE_DELTA_DATA_12345"
    delta_path = tmp_path / "deltas" / "1" / "test.bsdiff"

    save_delta(delta_bytes, delta_path)
    assert delta_path.exists()

    loaded = load_delta(delta_path)
    assert loaded == delta_bytes
