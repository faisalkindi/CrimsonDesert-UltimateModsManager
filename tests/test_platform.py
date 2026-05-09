"""Unit tests for ``cdumm.platform`` — the cross-platform abstraction
layer that mediates AppData paths, the ``open_path`` opener, and the
``worker_command`` PyInstaller-vs-source dispatch.

The maintainer's PR #64 review specifically asked for tests covering
``worker_command`` (frozen + run-from-source) and ``app_data_dir``
(per-platform branches with env-var coverage), plus the ``open_path``
error-detail logging fix. This file does all three by mocking
``sys.platform`` / ``sys.frozen`` / env vars rather than skipping per
platform — the platform branches are exercised on every CI runner.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ── worker_command ───────────────────────────────────────────────────


class TestWorkerCommand:
    """``worker_command(extra)`` returns the ``(exe, args)`` pair to
    pass to ``QProcess.start``. Frozen builds invoke the bundled
    CDUMM.exe directly; run-from-source has to prepend ``-m
    cdumm.main`` so Python reaches CDUMM's entry point before seeing
    ``--worker``."""

    def test_frozen_returns_executable_with_args_unchanged(self):
        from cdumm import platform as plat
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", "/path/to/CDUMM.exe"):
            exe, args = plat.worker_command(["--worker", "snapshot", "/game"])
        assert exe == "/path/to/CDUMM.exe"
        assert args == ["--worker", "snapshot", "/game"]

    def test_unfrozen_prepends_m_cdumm_main(self):
        from cdumm import platform as plat
        # ``sys.frozen`` is absent on a normal interpreter; getattr
        # default in worker_command should treat that as False.
        original_frozen = getattr(sys, "frozen", None)
        if hasattr(sys, "frozen"):
            del sys.frozen
        try:
            with patch.object(sys, "executable", "/usr/bin/python3"):
                exe, args = plat.worker_command(["--worker", "apply"])
            assert exe == "/usr/bin/python3"
            assert args == ["-m", "cdumm.main", "--worker", "apply"]
        finally:
            if original_frozen is not None:
                sys.frozen = original_frozen

    def test_returns_a_fresh_list_not_a_view_of_the_input(self):
        """Caller mutating the returned list must not affect later
        calls. Defensive — ``worker_command`` does ``list(extra_args)``
        on the frozen path; verify that hasn't drifted."""
        from cdumm import platform as plat
        original = ["--worker", "verify"]
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", "/x/CDUMM.exe"):
            _, args = plat.worker_command(original)
        args.append("MUTATED")
        assert original == ["--worker", "verify"]


# ── app_data_dir ─────────────────────────────────────────────────────


class TestAppDataDir:
    """Resolves to the platform-canonical per-user state directory.

    Windows: ``%LOCALAPPDATA%\\cdumm`` (env-driven, with a Path-
    constructed fallback when the env var is unset).
    macOS:   ``~/Library/Application Support/cdumm`` (no env var).
    Linux:   ``$XDG_DATA_HOME/cdumm`` with ``~/.local/share/cdumm``
             fallback when XDG_DATA_HOME is unset.
    """

    def test_windows_uses_localappdata_when_set(self, monkeypatch):
        from cdumm import platform as plat
        monkeypatch.setattr(plat, "IS_WINDOWS", True)
        monkeypatch.setattr(plat, "IS_MACOS", False)
        monkeypatch.setattr(plat, "IS_LINUX", False)
        monkeypatch.setenv("LOCALAPPDATA", "C:/Users/Foo/AppData/Local")
        result = plat.app_data_dir()
        # Compare via str so Windows-style backslashes don't fail on POSIX.
        assert str(result).replace("\\", "/").endswith(
            "Users/Foo/AppData/Local/cdumm")

    def test_windows_falls_back_when_localappdata_unset(self, monkeypatch):
        from cdumm import platform as plat
        monkeypatch.setattr(plat, "IS_WINDOWS", True)
        monkeypatch.setattr(plat, "IS_MACOS", False)
        monkeypatch.setattr(plat, "IS_LINUX", False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        result = plat.app_data_dir()
        # Path.home() / "AppData" / "Local" / "cdumm"
        # On non-Windows hosts Path.home() differs but the suffix is
        # platform-agnostic.
        assert result.name == "cdumm"
        assert result.parent.name == "Local"
        assert result.parent.parent.name == "AppData"

    def test_macos_uses_library_application_support(self, monkeypatch):
        from cdumm import platform as plat
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        monkeypatch.setattr(plat, "IS_MACOS", True)
        monkeypatch.setattr(plat, "IS_LINUX", False)
        result = plat.app_data_dir()
        parts = result.parts
        assert "Library" in parts
        assert "Application Support" in parts
        assert result.name == "cdumm"

    def test_linux_uses_xdg_data_home_when_set(self, monkeypatch, tmp_path):
        from cdumm import platform as plat
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        monkeypatch.setattr(plat, "IS_MACOS", False)
        monkeypatch.setattr(plat, "IS_LINUX", True)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        result = plat.app_data_dir()
        assert result == tmp_path / "cdumm"

    def test_linux_falls_back_to_local_share(self, monkeypatch):
        from cdumm import platform as plat
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        monkeypatch.setattr(plat, "IS_MACOS", False)
        monkeypatch.setattr(plat, "IS_LINUX", True)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        result = plat.app_data_dir()
        assert result.name == "cdumm"
        assert result.parent.name == "share"
        assert result.parent.parent.name == ".local"


# ── open_path error logging (review note 5b) ─────────────────────────


class TestOpenPathErrorLogging:
    """The previous ``open_path`` swallowed all exceptions silently;
    callers like ``mods_page._ctx_open_source`` lost the OSError
    message they used to log via the pre-refactor ``os.startfile``
    path. PR #64 review item 5b asked us to preserve that detail.
    Fix: log at WARNING inside ``open_path`` itself so every callsite
    benefits without churning per-call try/except blocks.
    """

    def test_oserror_logs_path_and_reason(self, monkeypatch, caplog):
        from cdumm import platform as plat

        # Run the macOS branch (the simplest single-call branch in
        # open_path) — patching subprocess.Popen forces an OSError
        # before the launcher actually fires.
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        monkeypatch.setattr(plat, "IS_MACOS", True)
        monkeypatch.setattr(plat, "IS_LINUX", False)

        def _raise(*a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(subprocess, "Popen", _raise)
        # Also patch the platform module's reference (subprocess is
        # imported there as a top-level name — not strictly needed if
        # Popen is the one Python attribute, but defensive).
        monkeypatch.setattr(plat.subprocess, "Popen", _raise)

        with caplog.at_level(logging.WARNING, logger="cdumm.platform"):
            result = plat.open_path("/some/file.txt")

        assert result is False
        assert any(
            "/some/file.txt" in rec.message
            and "permission denied" in rec.message
            for rec in caplog.records
        ), f"expected OSError detail in WARNING log; got: {caplog.records}"

    def test_no_opener_available_logs_clear_message(
            self, monkeypatch, caplog):
        """Linux without xdg-open / gio installed should log a clear
        'no opener found' WARNING and return False — that branch
        was previously a silent ``return False``."""
        from cdumm import platform as plat
        import shutil as _shutil

        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        monkeypatch.setattr(plat, "IS_MACOS", False)
        monkeypatch.setattr(plat, "IS_LINUX", True)
        monkeypatch.setattr(_shutil, "which", lambda _: None)
        monkeypatch.setattr(plat.shutil, "which", lambda _: None)

        with caplog.at_level(logging.WARNING, logger="cdumm.platform"):
            result = plat.open_path("/some/file.txt")

        assert result is False
        assert any(
            "no opener found" in rec.message and "/some/file.txt" in rec.message
            for rec in caplog.records
        )


# ── subprocess_no_window_kwargs ──────────────────────────────────────


class TestSubprocessNoWindowKwargs:
    """The CREATE_NO_WINDOW flag exists only on Windows. The helper
    returns the right kwargs for the current platform; plain ``{}`` on
    macOS / Linux so passing ``**kwargs`` to ``subprocess.run`` doesn't
    trip ``creationflags`` rejections."""

    def test_windows_returns_creationflags(self, monkeypatch):
        from cdumm import platform as plat
        monkeypatch.setattr(plat, "IS_WINDOWS", True)
        kwargs = plat.subprocess_no_window_kwargs()
        assert "creationflags" in kwargs
        assert isinstance(kwargs["creationflags"], int)
        # 0x08000000 = CREATE_NO_WINDOW on Windows
        assert kwargs["creationflags"] == 0x08000000

    def test_non_windows_returns_empty(self, monkeypatch):
        from cdumm import platform as plat
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        assert plat.subprocess_no_window_kwargs() == {}
