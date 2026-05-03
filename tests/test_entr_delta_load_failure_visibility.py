"""Bug D from Nexus 2026-05-03 (Torie1985): Barber Unlocked mod
applies cleanly with no errors but doesn't actually do anything
in-game. Pastebin log https://pastebin.com/TDRfpxCX shows:

    Failed to load entry delta E:\\...\\deltas\\20\\
    customizationcolorpalette.xml.entr: Not an entry delta: ...

The .entr file is missing the 4-byte ENTRY_MAGIC ('ENTR') header
that load_entry_delta() expects. Apply pipeline catches the
ValueError at apply_engine.py:2354 + 3328, logs a warning to
logger only, then `continue`s and silently moves on. The user
sees zero errors in the GUI/CLI and zero in-game effect.

Fix: surface the failure to self.warning so the user sees an
actionable message ("mod X's delta is corrupt; re-import it"),
not just a debug log line nobody reads.
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest


def test_apply_emits_warning_on_entr_load_failure():
    """When load_entry_delta raises (corrupt .entr, missing magic),
    apply must emit a user-visible warning naming the mod, not
    silently continue."""
    from cdumm.engine.apply_engine import ApplyWorker

    worker = ApplyWorker.__new__(ApplyWorker)
    worker._soft_warnings = []

    captured: list[str] = []

    class _SignalStub:
        def emit(self, msg):
            captured.append(msg)

    worker.warning = _SignalStub()

    # Method (introduced by the fix) takes the delta dict + exception
    # and emits a self.warning that names the mod.
    helper = getattr(worker, "_warn_entr_load_failure", None)
    assert helper is not None, (
        "apply_engine needs a _warn_entr_load_failure(d, exc) helper "
        "that surfaces 'Mod X's entry delta is corrupt; re-import it' "
        "to self.warning. The current pattern of logger.warning + "
        "continue swallows the failure and the user sees no errors "
        "and no in-game effect (Bug D Torie1985 / Barber Unlocked)."
    )

    delta_dict = {
        "delta_path": r"E:\Game\CDMods\deltas\20\foo.entr",
        "mod_name": "Barber Unlocked",
    }
    helper(delta_dict, ValueError(
        r"Not an entry delta: E:\Game\CDMods\deltas\20\foo.entr"))

    assert captured, "Helper must emit a warning"
    msg = captured[0]
    assert "Barber Unlocked" in msg, (
        f"Warning must name the mod so the user can act. Got: {msg!r}")
    assert "re-import" in msg.lower() or "reimport" in msg.lower(), (
        f"Warning should suggest the user re-import the mod. Got: {msg!r}")


def test_apply_engine_uses_helper_at_load_entry_delta_failure_sites():
    """Source-text guard: both load_entry_delta failure sites at
    apply_engine.py ~2354 and ~3328 must call the helper instead of
    using the silent 'logger.warning + continue' pattern."""
    from pathlib import Path
    src_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "cdumm" / "engine" / "apply_engine.py"
    )
    text = src_path.read_text(encoding="utf-8")

    # Find the failure sites by their old pattern. After the fix the
    # `logger.warning("Failed to load entry delta` lines should each
    # be paired with a `_warn_entr_load_failure(...)` call within the
    # next 5 lines (or replaced by it entirely).
    import re
    log_lines = [m.start() for m in re.finditer(
        r'logger\.warning\(\s*"Failed to load entry delta', text)]
    helper_calls = [m.start() for m in re.finditer(
        r"_warn_entr_load_failure\(", text)]

    # We expect at least 2 helper call sites (one per original logger
    # site), plus the helper definition itself = 3 total references.
    assert len(helper_calls) >= 2, (
        f"Expected the _warn_entr_load_failure helper to be called at "
        f"the two ENTR-load-failure sites in apply_engine. Helper call "
        f"count: {len(helper_calls)}. Original logger.warning sites: "
        f"{len(log_lines)}."
    )
