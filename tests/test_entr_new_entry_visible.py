"""New-in-mod PAMT entries must be VISIBLE when dropped
(audit finding 7).

_try_paz_entry_import skipped entries present in the mod's PAMT but
absent from vanilla's with a comment claiming they are "handled
separately"; no such handler exists, so the content silently vanished.
Until real add-entry support lands, the skip must log a warning and
surface a result.info message naming the dropped files (result.info
reaches the GUI as a yellow InfoBar).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.pamt_synth import build_pamt


@pytest.fixture(autouse=True)
def _pure_python_pamt_parse(monkeypatch):
    """Force the pure-Python PAMT parser so the synthetic PAMT layout
    only has to satisfy cdumm.archive.paz_parse, and clear the LRU
    cache between tests."""
    import cdumm.engine.mod_health_check as mhc
    monkeypatch.setattr(mhc, "_NATIVE_PARSE_PAMT", None, raising=False)
    # The LRU is keyed on (path, mtime, size); tmp_path files are
    # always fresh, but clear anyway for hygiene when supported.
    cache_clear = getattr(
        getattr(mhc, "_cached_vanilla_pamt_tuples", None),
        "cache_clear", None)
    if cache_clear:
        cache_clear()
    yield
    if cache_clear:
        cache_clear()


def test_new_pamt_entry_drop_is_surfaced(tmp_path: Path, caplog):
    from cdumm.engine.import_handler import (
        ModImportResult, _try_paz_entry_import,
    )
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    extracted = tmp_path / "extracted"
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    # Vanilla 0008: one uncompressed entry a.bin = b"AAAA"
    van_dir = game_dir / "0008"
    van_dir.mkdir(parents=True)
    (van_dir / "0.pamt").write_bytes(build_pamt([
        {"name": "a.bin", "offset": 0, "comp_size": 4,
         "orig_size": 4, "flags": 0},
    ]))
    (van_dir / "0.paz").write_bytes(b"AAAA")

    # Mod 0008: a.bin changed AND a brand-new b.bin entry appended.
    mod_dir = extracted / "0008"
    mod_dir.mkdir(parents=True)
    (mod_dir / "0.pamt").write_bytes(build_pamt([
        {"name": "a.bin", "offset": 0, "comp_size": 4,
         "orig_size": 4, "flags": 0},
        {"name": "b.bin", "offset": 4, "comp_size": 4,
         "orig_size": 4, "flags": 0},
    ]))
    (mod_dir / "0.paz").write_bytes(b"BBBB" + b"NEW!")

    db = Database(tmp_path / "test.db")
    db.initialize()
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) "
        "VALUES ('EntrMod', 'paz', 1)")
    db.connection.commit()
    mod_id = cur.lastrowid

    result = ModImportResult("EntrMod")
    import logging
    with caplog.at_level(logging.WARNING,
                         logger="cdumm.engine.import_handler"):
        ok = _try_paz_entry_import(
            mod_dir / "0.paz", van_dir / "0.paz", "0008/0.paz",
            extracted, game_dir, mod_id, db, deltas_dir, result)

    assert ok is True, "the changed a.bin entry must still import"
    assert result.info, (
        "dropping the mod's new b.bin entry must be surfaced on "
        "result.info, not silently skipped")
    assert "b.bin" in result.info
    assert "cannot apply yet" in result.info
    assert any("no vanilla counterpart" in r.message
               for r in caplog.records)
    db.close()
