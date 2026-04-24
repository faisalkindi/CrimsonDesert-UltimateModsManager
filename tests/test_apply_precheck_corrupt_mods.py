"""B3: at the start of Apply, walk every enabled mod's stored PAMT
deltas and parse-test them. Surface any that fail so users don't
sit through a partial apply that silently drops the broken mod.

B1 now catches corrupt PAMTs at import time. B3 covers the existing
installs from before B1 — any corrupt pamt already sitting in the
deltas store gets flagged at the top of apply with a clear warning.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path


def _apply_engine_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "engine" / "apply_engine.py").read_text(
                encoding="utf-8")


def test_precheck_function_exists():
    """The precheck must be a module-scope callable so it's testable
    without standing up the ApplyWorker."""
    from cdumm.engine import apply_engine
    assert hasattr(apply_engine, "precheck_enabled_mod_pamts"), (
        "expected module-scope helper "
        "precheck_enabled_mod_pamts(db) -> list[str]")


def test_precheck_returns_empty_on_clean_db(tmp_path):
    """No enabled mods → no warnings."""
    from cdumm.engine.apply_engine import precheck_enabled_mod_pamts
    from cdumm.storage.database import Database
    db = Database(tmp_path / "test.db")
    db.initialize()
    try:
        assert precheck_enabled_mod_pamts(db) == []
    finally:
        db.close()


def test_precheck_flags_corrupt_pamt(tmp_path):
    """Mod with a corrupt .pamt delta → precheck surfaces a warning
    naming the mod."""
    from cdumm.engine.apply_engine import precheck_enabled_mod_pamts
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()

    # Stage a corrupt pamt on disk — claim paz_count=1 million
    # (overflows our sanity bound).
    bad_pamt = tmp_path / "0.pamt"
    blob = b"\x00" * 4 + struct.pack("<I", 1_000_000) + b"\x00" * 40
    bad_pamt.write_bytes(blob)

    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (1, 'Broken Mod', 'paz', 1, 1)")
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end, is_new) "
        "VALUES (1, '0042/0.pamt', ?, 0, 0, 0)",
        (str(bad_pamt),))
    db.connection.commit()

    try:
        warnings = precheck_enabled_mod_pamts(db)
        assert warnings, "corrupt pamt must produce at least one warning"
        joined = " ".join(warnings)
        assert "Broken Mod" in joined, (
            "warning must name the mod so users know which one to fix")
    finally:
        db.close()


def test_precheck_ignores_disabled_mods(tmp_path):
    """Only walk ENABLED mods — disabled mods are opt-out already."""
    from cdumm.engine.apply_engine import precheck_enabled_mod_pamts
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()

    bad_pamt = tmp_path / "0.pamt"
    blob = b"\x00" * 4 + struct.pack("<I", 1_000_000) + b"\x00" * 40
    bad_pamt.write_bytes(blob)

    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (1, 'Disabled Broken', 'paz', 0, 1)")
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end, is_new) "
        "VALUES (1, '0042/0.pamt', ?, 0, 0, 0)",
        (str(bad_pamt),))
    db.connection.commit()

    try:
        assert precheck_enabled_mod_pamts(db) == [], (
            "disabled mods must not generate precheck warnings")
    finally:
        db.close()


def test_precheck_wired_into_apply():
    """_apply() must call the precheck and feed its warnings into
    _soft_warnings + warning.emit at the TOP of apply (before Phase
    1) so the user sees the problem before the long-running phases."""
    src = _apply_engine_src()
    # Anchor: the #145 cross-layer comment block sitting right at
    # the top of _apply's prelude — unique marker, deterministic.
    anchor = src.find("#145 cross-layer merge: build the PAZ-dir")
    assert anchor != -1, (
        "cross-layer prelude anchor not found — apply_engine "
        "refactor?")
    # Scope: 3000 chars around the anchor. Precheck must appear
    # within this window and BEFORE the cross-layer build itself.
    window = src[max(0, anchor - 3000):anchor + 2500]
    assert "precheck_enabled_mod_pamts" in window, (
        "_apply() must call precheck_enabled_mod_pamts near the "
        "cross-layer prelude so corrupt mods from pre-B1 installs "
        "get flagged at the top of apply")
    # Strict: call must appear BEFORE the cross-layer build, so the
    # warning fires in InfoBar before the long-running phases.
    pre_idx = window.find("precheck_enabled_mod_pamts")
    xlayer_idx = window.find(
        "self._paz_dir_overrides = collect_paz_dir_overrides(")
    assert pre_idx != -1 and xlayer_idx != -1
    assert pre_idx < xlayer_idx, (
        "precheck must run BEFORE the cross-layer build so users "
        "see the warning at the top of apply, not mid-run")
