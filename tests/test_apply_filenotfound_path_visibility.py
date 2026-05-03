"""Bug C from Nexus 2026-05-03 (Jyoungy13): apply fails with
"[winerror2] the system cannot find the file specified" but the
error has no path. Reporter Steam-verified files and re-linked
the mod folder; without knowing WHICH file is missing they
cannot diagnose further.

Root cause: ApplyWorker.run() wraps `self._apply()` in
`except Exception as e: emit(f"Apply failed: {e}")`. On Windows,
str(FileNotFoundError) = "[WinError 2] The system cannot find
the file specified" — no path. The exception object DOES carry
.filename / .filename2 attributes; the handler just doesn't read
them.

Fix: in the top-level except, when the exception is OSError-flavored,
include e.filename in the user-visible error message.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def test_apply_run_includes_filename_on_filenotfound(tmp_path: Path,
                                                    monkeypatch):
    """When _apply raises FileNotFoundError with .filename set, the
    error_occurred signal must include the filename so the user can
    see which vanilla file is missing."""
    from cdumm.engine.apply_engine import ApplyWorker

    db_path = tmp_path / "cdumm.db"
    # Initialize a fresh DB so Database(...).initialize() succeeds
    from cdumm.storage.database import Database
    db = Database(db_path)
    db.initialize()
    db.close()

    worker = ApplyWorker(tmp_path / "game", tmp_path / "vanilla", db_path)

    captured: list[str] = []

    class _SignalStub:
        def emit(self, msg):
            captured.append(msg)
    worker.error_occurred = _SignalStub()
    worker.warning = _SignalStub()
    worker.progress_updated = _SignalStub()
    worker.finished = _SignalStub()

    SENTINEL_PATH = r"C:\Game\0010\0.paz"

    def _raising_apply():
        raise FileNotFoundError(2, "The system cannot find the file specified",
                                SENTINEL_PATH)

    monkeypatch.setattr(worker, "_apply", _raising_apply)

    worker.run()

    assert captured, "Expected error_occurred.emit to be called"
    msg = captured[0]
    assert SENTINEL_PATH in msg, (
        f"Top-level apply error did not include the missing file path. "
        f"Captured: {msg!r}. The user reporter (Jyoungy13) saw a bare "
        f"'[WinError 2] the system cannot find the file specified' with "
        f"no actionable info about which vanilla file vanished."
    )
