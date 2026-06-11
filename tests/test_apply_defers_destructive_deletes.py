"""Audit finding C1 (2026-06-10): the apply/revert paths deleted
overlay/orphan dirs BEFORE txn.commit(). A failed commit then rolled
back to the pre-apply PAMTs/PAPGT, which referenced the already
deleted dirs, and a PAPGT entry pointing at a missing PAMT crashes
the game at boot with no recovery path.

The fix defers all destructive deletions until after commit and
teaches PapgtManager.rebuild an ``exclude_dirs`` set so the NEW
index never references the queued-for-deletion dirs even though
they are still on disk at rebuild time.
"""
from __future__ import annotations

import struct
from pathlib import Path


def _build_papgt(entries: list[tuple[str, int]]) -> bytes:
    """Minimal PAPGT with the given (dir_name, cached_hash) entries."""
    string_table = bytearray()
    body = bytearray()
    offsets = []
    for name, _h in entries:
        offsets.append(len(string_table))
        string_table += name.encode("ascii") + b"\x00"
    for (name, h), off in zip(entries, offsets):
        body += struct.pack("<III", 0x003FFF00, off, h)
    body += struct.pack("<I", len(string_table))
    body += string_table

    out = bytearray()
    out += b"\x01\x02\x03\x04"
    out += b"\x00\x00\x00\x00"
    out += bytes([len(entries), 0xFF, 0xFF, 0xFF])
    out += body
    return bytes(out)


def _papgt_dir_names(papgt: bytes) -> set[str]:
    entry_count = papgt[8]
    entry_start = 12
    string_table_off = entry_start + entry_count * 12 + 4
    names = set()
    for i in range(entry_count):
        pos = entry_start + i * 12
        name_off = struct.unpack_from("<I", papgt, pos + 4)[0]
        abs_off = string_table_off + name_off
        end = papgt.index(0, abs_off)
        names.add(papgt[abs_off:end].decode("ascii"))
    return names


def test_rebuild_exclude_dirs_drops_entry_despite_dir_on_disk(tmp_path: Path):
    """An excluded dir whose PAMT still exists on disk (deletion is
    deferred) must NOT appear in the rebuilt PAPGT, neither via the
    kept-entries pass nor via the new-dirs-on-disk scan."""
    from cdumm.archive.papgt_manager import PapgtManager

    game_dir = tmp_path / "game"
    (game_dir / "meta").mkdir(parents=True)
    # 0080: stale overlay queued for deletion, PAMT still on disk.
    (game_dir / "0080").mkdir()
    (game_dir / "0080" / "0.pamt").write_bytes(b"PAMT" + b"\x00" * 64)
    (game_dir / "meta" / "0.papgt").write_bytes(
        _build_papgt([("0001", 0xAAAAAAAA), ("0080", 0xBBBBBBBB)]))

    mgr = PapgtManager(game_dir, tmp_path / "vanilla-absent")
    rebuilt = mgr.rebuild(exclude_dirs={"0080"})
    names = _papgt_dir_names(rebuilt)
    assert "0080" not in names, (
        "excluded dir leaked into the rebuilt PAPGT")
    assert "0001" in names, "vanilla entry must survive"


def test_rebuild_without_exclusion_keeps_disk_dir(tmp_path: Path):
    """Control: same setup, no exclusion: the on-disk 0080 stays."""
    from cdumm.archive.papgt_manager import PapgtManager

    game_dir = tmp_path / "game"
    (game_dir / "meta").mkdir(parents=True)
    (game_dir / "0080").mkdir()
    (game_dir / "0080" / "0.pamt").write_bytes(b"PAMT" + b"\x00" * 64)
    (game_dir / "meta" / "0.papgt").write_bytes(
        _build_papgt([("0001", 0xAAAAAAAA), ("0080", 0xBBBBBBBB)]))

    mgr = PapgtManager(game_dir, tmp_path / "vanilla-absent")
    rebuilt = mgr.rebuild()
    assert "0080" in _papgt_dir_names(rebuilt)


def test_transactional_io_staged_files_accessor(tmp_path: Path):
    """Phase 3b's already-staged guard relied on txn.staged_files(),
    which did not exist (audit finding I4); pin the accessor."""
    from cdumm.archive.transactional_io import TransactionalIO

    game = tmp_path / "game"
    staging = tmp_path / "staging"
    game.mkdir()
    staging.mkdir()
    txn = TransactionalIO(game, staging)
    assert txn.staged_files() == []
    txn.stage_file("0008/0.paz", b"data")
    txn.stage_file("meta/0.pathc", b"pathc")
    assert txn.staged_files() == ["0008/0.paz", "meta/0.pathc"]
    # The accessor returns a copy, not the live list.
    txn.staged_files().append("tampered")
    assert "tampered" not in txn.staged_files()


def test_apply_engine_has_no_precommit_rmtree():
    """Source-level pin: inside apply_engine, every shutil.rmtree on a
    game-dir numbered directory must come AFTER a txn.commit() in the
    same method (the deferred lists). Heuristic: the file must not
    contain an rmtree between the orphan-cleanup scan markers and the
    commit call."""
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "src" / "cdumm" /
           "engine" / "apply_engine.py").read_text(encoding="utf-8")
    # The apply-path orphan scan starts at this marker and must reach
    # 'txn.commit()' before any rmtree appears.
    apply_scan = src.index("Orphan-cleanup scan:")
    commit_after = src.index("txn.commit()", apply_scan)
    between = src[apply_scan:commit_after]
    assert "rmtree" not in between, (
        "pre-commit rmtree reintroduced in the apply orphan cleanup")
    # Same for the Fix Everything scan.
    fix_scan = src.index("Cleaning orphan directories...")
    commit_after2 = src.index("txn.commit()", fix_scan)
    between2 = src[fix_scan:commit_after2]
    assert "rmtree" not in between2, (
        "pre-commit rmtree reintroduced in the revert orphan cleanup")
