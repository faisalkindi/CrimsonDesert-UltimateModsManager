"""GitHub #195 / PR #201 (RoGreat): Find Culprit Mod now works on non-Windows.

The bisection classifies each round as crash-vs-stable purely from whether the
CrimsonDesert.exe process appears and then survives (``find_game_process`` +
``wait_for_exit``); it does not depend on the Windows-only Pearl Abyss crashpad
``.dmp`` files. So the feature is enabled cross-platform by:

  * finding the process under Proton/Wine by executable BASENAME (the command
    line carries a Windows-style path even on Linux, so ``ntpath.basename``),
  * waiting for exit with ``psutil`` and only catching the specific psutil
    errors (a bare ``except`` would swallow KeyboardInterrupt / SystemExit).

These tests force the non-Windows branch on any host and mock psutil, so they
run in CI on Windows too.
"""
from __future__ import annotations

import psutil
import pytest

import cdumm.engine.game_monitor as gm


class _FakeProc:
    def __init__(self, pid, cmdline):
        self.info = {"pid": pid, "cmdline": cmdline}


class _RaisingProc:
    @property
    def info(self):
        raise psutil.NoSuchProcess(999)


# ── find_game_process (non-Windows basename match) ────────────────────

def _force_unix(monkeypatch, procs):
    monkeypatch.setattr(gm, "_IS_WINDOWS", False)
    monkeypatch.setattr(gm.psutil, "process_iter", lambda attrs=None: iter(procs))


def test_finds_process_by_basename_under_proton(monkeypatch):
    # Proton runs the game with a Windows-style path on Linux.
    procs = [
        _FakeProc(22254, ["/nix/store/…/gamemoded"]),
        _FakeProc(22533, ["S:\\common\\Crimson Desert\\bin64\\CrimsonDesert.exe",
                          "PlatformServiceType=Steam"]),
    ]
    _force_unix(monkeypatch, procs)
    assert gm.find_game_process() == 22533


def test_basename_match_is_case_insensitive(monkeypatch):
    _force_unix(monkeypatch, [
        _FakeProc(7, ["Z:\\games\\crimsondesert.EXE"])])
    assert gm.find_game_process() == 7


def test_crashpad_handler_is_not_matched(monkeypatch):
    # Same directory, different exe — must not be mistaken for the game.
    _force_unix(monkeypatch, [
        _FakeProc(1, ["S:\\common\\Crimson Desert\\bin64\\crashpad_handler.exe"])])
    assert gm.find_game_process() is None


def test_finds_native_macos_steam_executable(monkeypatch):
    # GitHub #299 (lwjiyuan): the NATIVE macOS Steam build's process is
    # CrimsonDesert_Steam (CFBundleExecutable), not CrimsonDesert.exe.
    # Find Culprit must detect it, or every bisection round scores a
    # false crash and can blame an innocent mod.
    _force_unix(monkeypatch, [_FakeProc(42, [
        "/Users/x/Library/Application Support/Steam/steamapps/common/"
        "Crimson Desert/CrimsonDesert_Steam.app/Contents/MacOS/"
        "CrimsonDesert_Steam"])])
    assert gm.find_game_process() == 42


def test_finds_native_build_without_steam_suffix(monkeypatch):
    _force_unix(monkeypatch, [_FakeProc(43, [
        "/Applications/Crimson Desert.app/Contents/MacOS/CrimsonDesert"])])
    assert gm.find_game_process() == 43


def test_native_macos_helper_process_not_matched(monkeypatch):
    # A differently-named sibling (e.g. a Steam helper) must not match.
    _force_unix(monkeypatch, [_FakeProc(1, [
        "/Users/x/.../MacOS/CrimsonDesert_SteamHelper"])])
    assert gm.find_game_process() is None


def test_returns_none_when_absent(monkeypatch):
    _force_unix(monkeypatch, [_FakeProc(1, ["/usr/bin/foo"])])
    assert gm.find_game_process() is None


def test_skips_empty_none_cmdline_and_bad_procs(monkeypatch):
    # Empty/None cmdline and a proc that raises must be skipped, not crash the
    # scan, and a later real match still wins.
    procs = [
        _FakeProc(1, []),
        _FakeProc(2, None),
        _RaisingProc(),
        _FakeProc(22533, ["S:\\x\\bin64\\CrimsonDesert.exe"]),
    ]
    _force_unix(monkeypatch, procs)
    assert gm.find_game_process() == 22533


# ── wait_for_exit (non-Windows, psutil, specific excepts) ─────────────

class _FakeProcess:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    def wait(self, timeout=None):
        if self._behaviour == "running":
            raise psutil.TimeoutExpired(timeout or 0)
        if self._behaviour == "gone":
            raise psutil.NoSuchProcess(1234)
        return 0  # exited cleanly within the timeout


def _patch_process(monkeypatch, behaviour):
    monkeypatch.setattr(gm, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        gm.psutil, "Process", lambda pid: _FakeProcess(behaviour))


def test_wait_still_running_returns_none(monkeypatch):
    _patch_process(monkeypatch, "running")
    assert gm.wait_for_exit(1234, 1000) is None


def test_wait_exited_returns_process_gone(monkeypatch):
    _patch_process(monkeypatch, "exited")
    assert gm.wait_for_exit(1234, 1000) == gm.PROCESS_GONE


def test_wait_missing_process_returns_process_gone(monkeypatch):
    _patch_process(monkeypatch, "gone")
    assert gm.wait_for_exit(1234, 1000) == gm.PROCESS_GONE


def test_wait_does_not_swallow_keyboardinterrupt(monkeypatch):
    # The old bare `except` would have caught this; the specific excepts must
    # let it propagate.
    monkeypatch.setattr(gm, "_IS_WINDOWS", False)

    class _Boom:
        def wait(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(gm.psutil, "Process", lambda pid: _Boom())
    with pytest.raises(KeyboardInterrupt):
        gm.wait_for_exit(1234, 1000)


# ── kill_process (non-Windows SIGTERM, guarded) ───────────────────────

def test_kill_process_sigterm_on_unix(monkeypatch):
    monkeypatch.setattr(gm, "_IS_WINDOWS", False)
    sent = {}
    monkeypatch.setattr(gm.os, "kill",
                        lambda pid, sig: sent.update(pid=pid, sig=sig))
    gm.kill_process(4321)
    assert sent == {"pid": 4321, "sig": gm.signal.SIGTERM}


def test_kill_process_swallows_already_gone(monkeypatch):
    monkeypatch.setattr(gm, "_IS_WINDOWS", False)

    def _raise(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(gm.os, "kill", _raise)
    gm.kill_process(4321)  # must not raise
