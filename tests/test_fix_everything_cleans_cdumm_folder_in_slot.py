"""Fix Everything's orphan sweep skips language_pack_dirs() (#458): a
0036-0040 folder the PAPGT flags optional, or on 2.04 any folder it
lists, counts as Steam's. Before #384, CDUMM numbered standalone mods
from 0036 up, and a standalone folder carries no _cdumm_overlay.marker,
so the exemption also kept CDUMM's own folder in such a slot. Full
Reset was the last step that cleared it before the rescan; kept, the
rescan hashes it into the snapshot as vanilla.

CDUMM recorded that folder's 0.pamt as a new file in mod_deltas. Since
#384, import skips the slots the PAPGT reserves, so a Steam pack's
slot has no such row.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

import cdumm.worker_process as wp
from cdumm.storage.database import Database

_VANILLA = 0x007FFF00
_OPTIONAL_FR = 0x00010001   # is_optional=1, one language bit (live 0037)


def _build_papgt(entries: list[tuple[str, int]]) -> bytes:
    """Legacy (pre-2.04) PAPGT; a copy of the builder in
    tests/test_language_pack_dirs_survive.py."""
    st = bytearray()
    offs = []
    for n, _f in entries:
        offs.append(len(st))
        st += n.encode() + b"\x00"
    body = bytearray()
    for (_n, f), o in zip(entries, offs):
        body += struct.pack("<III", f, o, 0)
    body += struct.pack("<I", len(st)) + st
    header = b"\x01\x02\x03\x04" + b"\x00" * 4 + bytes([len(entries), 0xFF, 0xFF, 0xFF])
    return bytes(header + body)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(wp, "_emit", lambda obj: None)


def _game(tmp_path: Path) -> tuple[Path, Path]:
    game = tmp_path / "game"
    (game / "meta").mkdir(parents=True)
    (game / "meta" / "0.papgt").write_bytes(
        _build_papgt([("0000", _VANILLA), ("0037", _OPTIONAL_FR)]))
    (game / "0037").mkdir()
    (game / "0037" / "0.pamt").write_bytes(b"\x00" * 16)
    (game / "0037" / "0.paz").write_bytes(b"\x00" * 64)
    vanilla = tmp_path / "CDMods" / "vanilla"
    vanilla.mkdir(parents=True)
    return game, vanilla


def _full_reset(tmp_path: Path, game: Path, vanilla: Path) -> None:
    wp._run_fix(str(game), str(vanilla), str(tmp_path / "cdumm.db"),
                steam_verified="1")


def test_cdumm_standalone_folder_in_a_language_slot_is_cleaned(tmp_path):
    game, vanilla = _game(tmp_path)
    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, priority) "
        "VALUES (1, 'pre-#384 standalone', 'paz', 1)")
    for fp, size in (("0037/0.pamt", 16), ("0037/0.paz", 64)):
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, is_new) VALUES (1, ?, 'dummy', 0, ?, 1)",
            (fp, size))
    db.connection.commit()
    db.close()
    _full_reset(tmp_path, game, vanilla)
    assert not (game / "0037").exists(), (
        "Fix Everything kept CDUMM's own standalone folder as a language pack")


def test_steam_pack_in_the_same_slot_is_still_kept(tmp_path):
    game, vanilla = _game(tmp_path)
    _full_reset(tmp_path, game, vanilla)
    assert (game / "0037" / "0.paz").is_file()
