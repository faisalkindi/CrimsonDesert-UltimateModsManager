"""Regression: v3.1.7 B3 apply-time precheck false-positives every mod.

Issue #38 (LeoBodnar, v3.1.7.1): after the v3.1.7.1 import fix, Apply
shipped a warning banner on every run with messages like:

    Mod 'Barber Unlocked' has a corrupt 0036/0.pamt:
    invalid literal for int() with base 10: '0036_0.pamt'.
    Re-import it from the original zip — this mod will not apply
    until it's reimported.

Root cause: ``precheck_enabled_mod_pamts`` calls
``parse_pamt(delta_path)`` but ``delta_path`` is a BSDIFF patch file
(``0036_0.pamt.bsdiff``), not a PAMT. Two things break:

1. bsdiff binary data isn't PAMT format — parsing it as PAMT is
   nonsensical regardless of filename.
2. The stem of ``0036_0.pamt.bsdiff`` is ``0036_0.pamt`` (non-
   numeric), so parse_pamt's ``int(pamt_stem)`` blows up with the
   exact same error pattern as v3.1.7's B1 bug.

Fix: the precheck is unsalvageable in its current form — it can't
validate stored deltas without also running the vanilla-read +
bsdiff-patch reconstruction dance, which belongs in apply itself.
Make it a documented no-op. B1 (v3.1.7.1) catches corrupt PAMTs at
import time; the apply flow already handles bad deltas downstream.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_precheck_does_not_parse_bsdiff_as_pamt(tmp_path: Path):
    """With a realistic mod + stored pamt delta (just bsdiff bytes on
    disk at the conventional path), the precheck must NOT emit a
    warning. Before the fix, every single enabled pamt-touching mod
    got flagged on every apply."""
    from cdumm.engine.apply_engine import precheck_enabled_mod_pamts
    from cdumm.storage.database import Database

    # Set up a minimal DB with one enabled PAZ mod and a .pamt delta row.
    db_path = tmp_path / "cdumm.db"
    db = Database(db_path)
    db.initialize()
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority) "
        "VALUES (?, 'paz', 1, 0)", ("Barber Unlocked",))
    mod_id = cur.lastrowid
    # Mimic how save_delta names the delta file: file_path with '/'
    # replaced by '_', '.bsdiff' appended.
    deltas_dir = tmp_path / "deltas" / str(mod_id)
    deltas_dir.mkdir(parents=True)
    delta_file = deltas_dir / "0036_0.pamt.bsdiff"
    # Real bsdiff patch bytes aren't needed — we're testing that the
    # precheck doesn't try to parse them as PAMT at all. Any opaque
    # blob that isn't a valid PAMT header triggers the bug.
    delta_file.write_bytes(b"BSDIFF40" + b"\x00" * 32)
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path) "
        "VALUES (?, ?, ?)",
        (mod_id, "0036/0.pamt", str(delta_file)))
    db.connection.commit()

    warnings = precheck_enabled_mod_pamts(db)
    assert warnings == [], (
        f"precheck must not false-positive on well-formed delta rows. "
        f"Got: {warnings}")


def test_precheck_returns_list(tmp_path: Path):
    """Contract preserved: still returns a list (so callers' .extend /
    .emit code paths keep working)."""
    from cdumm.engine.apply_engine import precheck_enabled_mod_pamts
    from cdumm.storage.database import Database

    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    result = precheck_enabled_mod_pamts(db)
    assert isinstance(result, list)
