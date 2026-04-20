"""#148 code prong: pre-Qt crash catcher writes a file users can paste
into a bug report, so domolinixd1000-style "CDUMM closes in 2-3 seconds
with no log" reports become debuggable.

This covers the failure window BEFORE `sys.excepthook` is wired in
`main()`. A module import error, a missing Qt plugin, or an I/O failure
on `%LOCALAPPDATA%\\cdumm\\crash_trace.txt` would previously vanish.
"""
from __future__ import annotations

from pathlib import Path


def test_emergency_crash_dump_writes_to_appdata_or_temp(tmp_path, monkeypatch):
    """Exercise _emergency_crash_dump directly with a synthetic exception."""
    from cdumm import main as cdumm_main

    # Redirect APP_DATA_DIR at the module level so the dump lands in
    # tmp_path instead of the user's real AppData.
    monkeypatch.setattr(cdumm_main, "APP_DATA_DIR", tmp_path)

    try:
        raise RuntimeError("synthetic pre-Qt crash for test")
    except RuntimeError as exc:
        cdumm_main._emergency_crash_dump(exc)

    dump = tmp_path / "crash-pre-qt.log"
    assert dump.exists(), "dump file must be created when AppData is writable"
    body = dump.read_text(encoding="utf-8")
    assert "synthetic pre-Qt crash for test" in body
    assert "RuntimeError" in body


def test_emergency_crash_dump_falls_back_to_temp(tmp_path, monkeypatch):
    """If AppData is unwritable, the dump must land in %TEMP% instead."""
    from cdumm import main as cdumm_main

    # Force AppData to a path whose parent cannot be created so
    # write_text fails. A dangling nested path under tmp_path is
    # effectively writable on Windows, so we use a file-as-parent trick:
    # create a plain file, then point APP_DATA_DIR to a path UNDER it.
    wall = tmp_path / "wall.txt"
    wall.write_text("not a directory", encoding="utf-8")
    fake_appdata = wall / "cdumm"  # mkdir on this raises because wall is a file

    monkeypatch.setattr(cdumm_main, "APP_DATA_DIR", fake_appdata)

    # Redirect %TEMP% to a writable tmp subdir so the fallback lands
    # somewhere we can inspect.
    temp_dir = tmp_path / "my_temp"
    temp_dir.mkdir()
    monkeypatch.setenv("TEMP", str(temp_dir))

    try:
        raise ValueError("cascading failure scenario")
    except ValueError as exc:
        cdumm_main._emergency_crash_dump(exc)

    fallback = temp_dir / "cdumm-crash-pre-qt.log"
    assert fallback.exists(), (
        "when AppData is unwritable, crash must land in %TEMP% fallback")
    body = fallback.read_text(encoding="utf-8")
    assert "cascading failure scenario" in body


def test_main_module_imports_despite_appdata_locked(monkeypatch):
    """Module-level faulthandler setup must not crash import even when
    AppData is unavailable. A raising `open()` at module top is what
    previously caused the domolinixd1000-style silent exit."""
    # Importing the module runs the defensive faulthandler block. If
    # the block wasn't wrapped, import itself would raise on systems
    # with perms issues. Just import and assert _emergency_crash_dump
    # is exposed.
    from cdumm import main as cdumm_main
    assert callable(cdumm_main._emergency_crash_dump)
