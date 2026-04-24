"""B3: at the start of Apply, walk every enabled mod's stored PAMT
deltas and parse-test them.

**v3.1.7.2 update:** the original implementation was fundamentally
broken — it ``parse_pamt``'d delta files, which are BSDIFF patches
not PAMT bytes. Issue #38 (LeoBodnar) showed every valid mod being
false-flagged on every Apply. The precheck is now a no-op pending a
proper reimplementation that reconstructs the modified PAMT via
bsdiff4.patch before parsing. See ``precheck_enabled_mod_pamts``
docstring for the full rationale.

B1 (v3.1.7.1) catches corrupt PAMTs at import time, so the safety
net is mostly redundant; the apply flow's downstream error handling
covers the remaining legacy case.
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


def test_precheck_is_a_noop_pending_proper_impl(tmp_path):
    """v3.1.7.2: placeholder returns [] regardless of DB state. The
    previous implementation called parse_pamt on delta files, which
    are BSDIFF patches — produced garbage PAMT headers that flagged
    every valid mod as corrupt on every Apply."""
    from cdumm.engine.apply_engine import precheck_enabled_mod_pamts
    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()

    # Stage what the old test called a "corrupt pamt" — in v3.1.7.1
    # this produced a warning; in v3.1.7.2 it must not, because the
    # precheck never opens the file in the first place.
    bad_pamt = tmp_path / "0.pamt"
    blob = b"\x00" * 4 + struct.pack("<I", 1_000_000) + b"\x00" * 40
    bad_pamt.write_bytes(blob)

    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (1, 'Anything', 'paz', 1, 1)")
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
        "byte_start, byte_end, is_new) "
        "VALUES (1, '0042/0.pamt', ?, 0, 0, 0)",
        (str(bad_pamt),))
    db.connection.commit()

    try:
        assert precheck_enabled_mod_pamts(db) == [], (
            "precheck is a no-op in v3.1.7.2 — it must not "
            "false-positive on any DB state")
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
