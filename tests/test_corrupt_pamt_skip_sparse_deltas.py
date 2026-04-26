"""Cross-layer override scan must only treat full-file PAMT
replacements as candidates.

Bug from Faisal 2026-04-26 (Richardker2545 + DerBambusbjoern + Giony
on Nexus): Crimson Browser format mods (e.g. r457 Graphics Tweaks
mod 602) are converted into NNNN/0.paz + NNNN/0.pamt at import, but
the PAMT is stored as an SPRS sparse delta (.bsdiff extension), not
a full file (.newfile extension). collect_paz_dir_overrides was
copying the .bsdiff delta to 0.pamt and trying to parse it as a
full PAMT, which fails with 'folder_size exceeds file size' because
the SPRS magic bytes look like garbage offsets.

The cross-layer override feature only makes sense for full-file
PAZ-dir replacements (.newfile). Sparse deltas are handled by the
byte-merge path elsewhere.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from cdumm.engine.apply_engine import collect_paz_dir_overrides


def _make_db_with_sparse_pamt_delta(tmp_path: Path):
    """A CB-style mod whose stored PAMT delta is .bsdiff sparse,
    not .newfile full replacement."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, mod_type TEXT, "
        "enabled INTEGER, priority INTEGER)"
    )
    conn.execute(
        "CREATE TABLE mod_deltas ("
        "id INTEGER PRIMARY KEY, mod_id INTEGER, file_path TEXT, "
        "delta_path TEXT)"
    )
    conn.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (1, 'r457 Graphics Tweaks', 'paz', 1, 50)"
    )
    paz_delta = tmp_path / "0003_0.paz.bsdiff"
    pamt_delta = tmp_path / "0003_0.pamt.bsdiff"
    # Real SPRS sparse-patch header bytes (see _process_extracted_files)
    sprs_bytes = (b"SPRS" + b"\x0f\x00\x00\x00" + b"\x00" * 200)
    paz_delta.write_bytes(sprs_bytes)
    pamt_delta.write_bytes(sprs_bytes)
    conn.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path) "
        "VALUES (1, '0003/0.paz', ?)", (str(paz_delta),))
    conn.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path) "
        "VALUES (1, '0003/0.pamt', ?)", (str(pamt_delta),))
    conn.commit()

    class _DBWrap:
        def __init__(self, c):
            self.connection = c

    return _DBWrap(conn)


def test_sparse_pamt_delta_is_skipped_silently(
        tmp_path: Path, monkeypatch) -> None:
    """A CB-converted mod with .bsdiff sparse PAMT delta must NOT
    fire the 'corrupt archive' warning. The SPRS sparse delta is
    not a full PAMT — collect_paz_dir_overrides should skip it
    silently. Only .newfile full-file replacements are valid
    cross-layer override candidates.

    Spy on parse_pamt to confirm the sparse delta never reaches it
    (semantic correctness, not just absence of warning)."""
    db = _make_db_with_sparse_pamt_delta(tmp_path)

    parse_calls = {"count": 0}
    from cdumm.archive import paz_parse as _pp

    def _spy(path, paz_dir=None):
        parse_calls["count"] += 1
        # Should never reach here for a sparse delta.
        return []

    monkeypatch.setattr(_pp, "parse_pamt", _spy)
    # Re-import the symbol used inside collect_paz_dir_overrides
    from cdumm.engine import apply_engine
    monkeypatch.setattr("cdumm.engine.apply_engine.parse_pamt",
                        _spy, raising=False)

    warnings: list[str] = []
    overrides = collect_paz_dir_overrides(db, warnings_out=warnings)

    assert parse_calls["count"] == 0, (
        f"parse_pamt was called {parse_calls['count']} time(s) on a "
        f"sparse .bsdiff delta. It should be skipped before reaching "
        f"the parser — sparse deltas aren't full PAMTs.")
    assert warnings == [], (
        f"sparse-delta PAMT must not warn the user. Got:\n"
        + "\n".join(f"  - {w}" for w in warnings))
    assert overrides == {}
