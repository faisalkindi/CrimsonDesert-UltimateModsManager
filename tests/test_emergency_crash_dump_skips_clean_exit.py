"""``_emergency_crash_dump`` must skip clean exits.

Bug from Faisal 2026-04-27: user reported "Crash (app closed/froze)"
but logs showed only `Traceback ... SystemExit: 0` in
``crash-pre-qt.log`` — that's a CLEAN exit. Investigation:

  main.py:501  sys.exit(main())
  main.py:502  except BaseException as _bootstrap_exc:
  main.py:506      _emergency_crash_dump(_bootstrap_exc)

``BaseException`` catches ``SystemExit`` and ``KeyboardInterrupt``
alongside actual crashes. When the user closes the app normally,
``sys.exit(0)`` raises ``SystemExit(0)``, gets caught here, and a
fake-looking traceback gets written to ``crash-pre-qt.log``. Users
inspecting the file (or the bug-report tool flagging it) interpret
it as a crash — phantom bug reports follow.

The handler must skip ``SystemExit`` and ``KeyboardInterrupt`` —
only write for actual unexpected exceptions.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.main import _emergency_crash_dump


def test_systemexit_zero_does_not_write_crash_file(tmp_path, monkeypatch):
    """SystemExit(0) is a clean exit. Must not produce a crash file."""
    import cdumm.main as main_mod
    monkeypatch.setattr(main_mod, "APP_DATA_DIR", tmp_path)

    try:
        raise SystemExit(0)
    except SystemExit as e:
        _emergency_crash_dump(e)

    crash_file = tmp_path / "crash-pre-qt.log"
    assert not crash_file.exists(), (
        "SystemExit(0) is a clean exit — must NOT produce a "
        "crash-pre-qt.log file. The file misleads users (and the "
        "bug-report tool) into thinking the app crashed.")


def test_systemexit_nonzero_does_not_write_crash_file(tmp_path, monkeypatch):
    """SystemExit(N) for any N is also a deliberate exit (e.g. error
    code from --check), not a crash."""
    import cdumm.main as main_mod
    monkeypatch.setattr(main_mod, "APP_DATA_DIR", tmp_path)

    try:
        raise SystemExit(2)
    except SystemExit as e:
        _emergency_crash_dump(e)

    crash_file = tmp_path / "crash-pre-qt.log"
    assert not crash_file.exists()


def test_keyboard_interrupt_does_not_write_crash_file(tmp_path, monkeypatch):
    """Ctrl+C from the user is not a crash."""
    import cdumm.main as main_mod
    monkeypatch.setattr(main_mod, "APP_DATA_DIR", tmp_path)

    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt as e:
        _emergency_crash_dump(e)

    crash_file = tmp_path / "crash-pre-qt.log"
    assert not crash_file.exists()


def test_real_exception_still_writes_crash_file(tmp_path, monkeypatch):
    """Genuine unexpected exceptions MUST still write the crash file
    — that's what the handler exists for. Regression guard."""
    import cdumm.main as main_mod
    monkeypatch.setattr(main_mod, "APP_DATA_DIR", tmp_path)

    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        _emergency_crash_dump(e)

    crash_file = tmp_path / "crash-pre-qt.log"
    assert crash_file.exists(), (
        "Real exceptions must still produce crash-pre-qt.log — "
        "that's the entire purpose of this handler")
    content = crash_file.read_text(encoding="utf-8")
    assert "RuntimeError" in content
    assert "boom" in content
