"""GitHub #199 follow-up (RoGreat, 2026-06-11): VAXIS Water Physics
and Enhanced Internal Graphics edit DIFFERENT files in the same small
PAZ (0003, 760 KB) and Internal Graphics was dropped wholesale with a
same-data conflict.

Two root causes, both pinned here:

1. Entry-level decomposition only ran for PAZ files over 10 MB (a
   v1.8.0 perf gate), so small shared PAZs produced vanilla-anchored
   raw byte diffs that cannot compose: two mods on the same small PAZ
   were winner-takes-all. The gate is gone; every .paz attempts
   decomposition.

2. Encrypted-but-UNCOMPRESSED entries (.material under technique/,
   the #199 class) were stored as ciphertext-with-plaintext-flags by
   the ENTR path: the extension heuristic does not know .material and
   the lz4 probe only ran for compressed entries. Verified live: the
   applied overlay carried ChaCha20 ciphertext marked as plain and
   the game read garbage. The ENTR path now runs the same
   plaintext-signature probe the CB repack path got in v3.3.19 and
   stores decrypted content with encrypted=True metadata.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.pamt_synth import build_pamt


@pytest.fixture(autouse=True)
def _pure_python_pamt_parse(monkeypatch):
    import cdumm.engine.mod_health_check as mhc
    monkeypatch.setattr(mhc, "_NATIVE_PARSE_PAMT", None, raising=False)
    cache_clear = getattr(
        getattr(mhc, "_cached_vanilla_pamt_tuples", None),
        "cache_clear", None)
    if cache_clear:
        cache_clear()
    yield
    if cache_clear:
        cache_clear()


def _setup(tmp_path: Path, van_entries, van_paz, mod_entries, mod_paz):
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    extracted = tmp_path / "extracted"
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    van_dir = game_dir / "0003"
    van_dir.mkdir(parents=True)
    (van_dir / "0.pamt").write_bytes(build_pamt(van_entries))
    (van_dir / "0.paz").write_bytes(van_paz)
    mod_dir = extracted / "0003"
    mod_dir.mkdir(parents=True)
    (mod_dir / "0.pamt").write_bytes(build_pamt(mod_entries))
    (mod_dir / "0.paz").write_bytes(mod_paz)
    db = Database(tmp_path / "test.db")
    db.initialize()
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) "
        "VALUES ('M', 'paz', 1)")
    db.connection.commit()
    return game_dir, extracted, deltas_dir, van_dir, mod_dir, db, cur.lastrowid


def test_small_paz_has_no_size_gate():
    """Source-level pin: the ENTR dispatch must not be gated on PAZ
    size. The v1.8.0 `mod_size > 10 * 1024 * 1024` gate routed small
    shared PAZs to non-composable raw diffs."""
    src = (Path(__file__).resolve().parents[1] / "src" / "cdumm" /
           "engine" / "import_handler.py").read_text(encoding="utf-8")
    anchor = src.index("Entry-level decomposition for PAZ files")
    window = src[anchor:anchor + 2200]
    assert 'rel_path.endswith(".paz")' in window
    assert "10 * 1024 * 1024" not in window.split(
        "Fast-track for different-size large files")[0], (
        "the ENTR size gate is back; small shared PAZs will stop "
        "composing (GitHub #199 RoGreat regression)")


def test_encrypted_uncompressed_entry_stores_plaintext(tmp_path: Path):
    """An encrypted-but-uncompressed entry whose extension the
    heuristic does not know (.material) must be stored DECRYPTED with
    encrypted=True metadata, so the overlay re-encrypts correctly at
    build time instead of double-marking ciphertext as plaintext."""
    from cdumm.archive.paz_crypto import encrypt
    from cdumm.engine.import_handler import (
        ModImportResult, _try_paz_entry_import,
    )

    van_plain = b"\xef\xbb\xbf<Technique Name=\"Water\"/>\r\n<Old/>"
    mod_plain = b"\xef\xbb\xbf<Technique Name=\"Water\"/>\r\n<NewWave/>"
    van_cipher = encrypt(van_plain, "water.material")
    mod_cipher = encrypt(mod_plain, "water.material")

    van_entries = [{"name": "water.material", "offset": 0,
                    "comp_size": len(van_cipher),
                    "orig_size": len(van_cipher), "flags": 0}]
    mod_entries = [{"name": "water.material", "offset": 0,
                    "comp_size": len(mod_cipher),
                    "orig_size": len(mod_cipher), "flags": 0}]

    (game_dir, extracted, deltas_dir, van_dir, mod_dir, db,
     mod_id) = _setup(tmp_path, van_entries, van_cipher,
                      mod_entries, mod_cipher)

    result = ModImportResult("M")
    ok = _try_paz_entry_import(
        mod_dir / "0.paz", van_dir / "0.paz", "0003/0.paz",
        extracted, game_dir, mod_id, db, deltas_dir, result)
    assert ok is True, "encrypted-uncompressed entry did not import"

    rows = db.connection.execute(
        "SELECT delta_path, entry_path FROM mod_deltas "
        "WHERE mod_id = ?", (mod_id,)).fetchall()
    assert len(rows) == 1
    delta_path, entry_path = rows[0]
    assert entry_path.endswith("water.material")

    # The saved ENTR delta must hold PLAINTEXT content and
    # encrypted=True metadata.
    from cdumm.engine.delta_engine import load_entry_delta
    content, metadata = load_entry_delta(Path(delta_path))
    assert content == mod_plain, (
        "stored content is not the mod's plaintext (ciphertext "
        "stored as plaintext was the #199 ENTR bug)")
    assert metadata.get("encrypted") is True, (
        "metadata must mark the slot encrypted so the overlay "
        "re-encrypts at build time")
    db.close()


def test_plain_uncompressed_entry_unaffected(tmp_path: Path):
    """Control: a genuinely-plain uncompressed text entry must not be
    flagged encrypted by the new probe."""
    from cdumm.engine.import_handler import (
        ModImportResult, _try_paz_entry_import,
    )

    van_plain = b"<config><a/></config>"
    mod_plain = b"<config><b/></config>"
    van_entries = [{"name": "plain.bin", "offset": 0,
                    "comp_size": len(van_plain),
                    "orig_size": len(van_plain), "flags": 0}]
    mod_entries = [{"name": "plain.bin", "offset": 0,
                    "comp_size": len(mod_plain),
                    "orig_size": len(mod_plain), "flags": 0}]

    (game_dir, extracted, deltas_dir, van_dir, mod_dir, db,
     mod_id) = _setup(tmp_path, van_entries, van_plain,
                      mod_entries, mod_plain)

    result = ModImportResult("M")
    ok = _try_paz_entry_import(
        mod_dir / "0.paz", van_dir / "0.paz", "0003/0.paz",
        extracted, game_dir, mod_id, db, deltas_dir, result)
    assert ok is True

    from cdumm.engine.delta_engine import load_entry_delta
    delta_path = db.connection.execute(
        "SELECT delta_path FROM mod_deltas WHERE mod_id = ?",
        (mod_id,)).fetchone()[0]
    content, metadata = load_entry_delta(Path(delta_path))
    assert content == mod_plain
    assert not metadata.get("encrypted"), (
        "plain text entry wrongly flagged encrypted")
    db.close()
