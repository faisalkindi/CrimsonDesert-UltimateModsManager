"""Synthetic 2.04 indexes: no game assets or machine-specific paths required."""

import struct

import pytest

from cdumm.archive.papgt_manager import PapgtManager, read_papgt_entries
from cdumm.archive.paz_parse import parse_pamt


def _compact_pamt(*, nested=False):
    folder = b'\0\x08gamedata'
    folder_records = b''
    if nested:
        folder += b'\x0a\x09/binary__'
        folder_records = struct.pack('<4I', 0, 10, 0, 2)
    # The second node points backwards by its byte distance, not a node id.
    nodes = b'\0\x09inventory' + b'\x0b\x06_extra'
    extensions = b'\0.staticinfobody\0.staticinfoheader\0'
    return (
        struct.pack('<4I', 0, 1, 0x610E0232, 0)
        + struct.pack('<2I', 0, 100)
        + struct.pack('<I', len(folder)) + folder
        + struct.pack('<I', len(nodes)) + nodes
        + struct.pack('<I', int(nested)) + folder_records
        + struct.pack('<I', 2)
        + struct.pack('<5I', 0, 16, 20, 40, 0x01020000)
        + struct.pack('<5I', 11, 48, 10, 10, 0x02000000)
        + struct.pack('<I', len(extensions)) + extensions
    )


def _compact_papgt():
    # Four-byte prefix, regular 12-byte entries, strings, 64 opaque bytes.
    names = b'0000\0' + b'0008\0' + b'0040\0'
    entries = b''.join(struct.pack('<3I', flags, i * 5, 0x1234 + i)
                       for i, flags in enumerate((0x7FFF00, 0x7FFF00, 0x2001)))
    return struct.pack('<I', 0x02271681) + entries + struct.pack('<I', len(names)) + names + bytes(64)


def test_compact_pamt_resolves_relative_names_and_extensions(tmp_path):
    path = tmp_path / '0.pamt'
    path.write_bytes(_compact_pamt())
    entries = parse_pamt(str(path))
    assert [e.path for e in entries] == [
        'gamedata/inventory.staticinfobody',
        'gamedata/inventory_extra.staticinfoheader',
    ]
    assert [e.offset for e in entries] == [16, 48]
    assert [e.compression_type for e in entries] == [2, 0]


def test_compact_pamt_does_not_parse_extension_table_as_files(tmp_path):
    path = tmp_path / '0.pamt'
    path.write_bytes(_compact_pamt())
    assert len(parse_pamt(str(path))) == 2


def test_compact_full_paths_share_the_counted_parser(tmp_path):
    from cdumm.archive.overlay_builder import _build_full_path_map
    from cdumm.engine.crimson_browser_handler import _pamt_full_paths

    path = tmp_path / '0008/0.pamt'
    path.parent.mkdir()
    path.write_bytes(_compact_pamt(nested=True))
    full = [
        'gamedata/binary__/inventory.staticinfobody',
        'gamedata/binary__/inventory_extra.staticinfoheader',
    ]
    assert [e.path for e in parse_pamt(str(path), full_paths=True)] == full
    assert _pamt_full_paths(path) == full
    assert _build_full_path_map('0008', tmp_path) == {
        'gamedata/inventory.staticinfobody': 'gamedata/binary__',
        'gamedata/inventory_extra.staticinfoheader': 'gamedata/binary__',
    }


def test_compact_full_paths_reject_out_of_bounds_folder_range(tmp_path):
    data = bytearray(_compact_pamt(nested=True))
    record = data.index(struct.pack('<4I', 0, 10, 0, 2))
    struct.pack_into('<I', data, record + 12, 3)
    path = tmp_path / '0.pamt'
    path.write_bytes(data)
    with pytest.raises(ValueError, match='folder file range'):
        parse_pamt(str(path), full_paths=True)


def test_compact_empty_pamt_omits_file_count(tmp_path):
    path = tmp_path / '0.pamt'
    path.write_bytes(struct.pack('<8I', 0, 0, 0x610E0232, 0, 0, 0, 0, 1) + b'\0')
    assert parse_pamt(str(path)) == []


def test_compact_pamt_with_no_folder_names(tmp_path):
    data = bytearray(_compact_pamt())
    folder_size = struct.unpack_from('<I', data, 24)[0]
    del data[28:28 + folder_size]
    struct.pack_into('<I', data, 24, 0)
    path = tmp_path / '0.pamt'
    path.write_bytes(data)
    assert parse_pamt(str(path))[0].path == 'inventory.staticinfobody'


def test_compact_tree_parent_distance_uses_multiple_bytes(tmp_path):
    data = bytearray(_compact_pamt())
    size_pos = 28 + struct.unpack_from('<I', data, 24)[0]
    start = size_pos + 4
    old_size = struct.unpack_from('<I', data, size_pos)[0]
    prefix = b'a' * 140
    nodes = b'\0\x8c' + prefix + b'\x8e\x01\x01b'
    data[start:start + old_size] = nodes
    struct.pack_into('<I', data, size_pos, len(nodes))
    records_start = start + len(nodes) + 8
    struct.pack_into('<I', data, records_start + 20, 142)
    path = tmp_path / '0.pamt'
    path.write_bytes(data)
    assert parse_pamt(str(path))[1].path == 'gamedata/' + 'a' * 140 + 'b.staticinfoheader'


@pytest.mark.parametrize('damage', ['distance', 'extension', 'truncated'])
def test_compact_pamt_rejects_damaged_index(tmp_path, damage):
    data = bytearray(_compact_pamt())
    if damage == 'distance':
        data[data.index(b'\x0b\x06_extra')] = 12  # not an existing parent
    elif damage == 'extension':
        i = data.index(struct.pack('<5I', 0, 16, 20, 40, 0x01020000))
        struct.pack_into('<I', data, i + 16, 0xFF020000)
    else:
        data = data[:-1]
    path = tmp_path / '0.pamt'
    path.write_bytes(data)
    with pytest.raises(ValueError):
        parse_pamt(str(path))


def test_compact_papgt_lists_all_reserved_entries(tmp_path):
    path = tmp_path / '0.papgt'
    path.write_bytes(_compact_papgt())
    assert read_papgt_entries(path) == [
        ('0000', 0x7FFF00, 0x1234), ('0008', 0x7FFF00, 0x1235),
        ('0040', 0x2001, 0x1236),
    ]


@pytest.mark.parametrize('stale_snapshot', [False, True])
def test_rebuild_refuses_new_index_instead_of_dropping_game_entries(tmp_path, stale_snapshot):
    game = tmp_path / 'game'
    (game / 'meta').mkdir(parents=True)
    path = game / 'meta/0.papgt'
    original = _compact_papgt()
    path.write_bytes(original)
    vanilla = None
    if stale_snapshot:
        vanilla = tmp_path / 'vanilla'
        (vanilla / 'meta').mkdir(parents=True)
        # An older snapshot must never let a live 2.04 index be overwritten.
        names = b'0000\0'
        legacy = (bytes(12) + struct.pack('<3I', 0x7FFF00, 0, 1)
                  + struct.pack('<I', len(names)) + names)
        (vanilla / 'meta/0.papgt').write_bytes(legacy)
    with pytest.raises(ValueError, match='2.04'):
        PapgtManager(game, vanilla).rebuild()
    assert path.read_bytes() == original


def test_rebuild_refuses_unrecognised_index_without_estimating(tmp_path):
    game = tmp_path / 'game'
    (game / 'meta').mkdir(parents=True)
    path = game / 'meta/0.papgt'
    original = _compact_papgt()[:-1]
    path.write_bytes(original)
    with pytest.raises(ValueError, match='refusing to estimate'):
        PapgtManager(game).rebuild()
    assert path.read_bytes() == original
