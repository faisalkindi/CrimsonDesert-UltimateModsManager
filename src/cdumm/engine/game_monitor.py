"""Game process monitoring for automated mod bisection.

Launches Crimson Desert through Steam, monitors the process for crashes,
and reports whether the game survived a stability window. Pure utility
module — no GUI, no Qt dependencies.

Ported from CDCrashMonitor v3 (cd_crash_monitor.py).

The crash-monitor flow uses Windows-only crashpad introspection (the
``.dmp`` files Pearl Abyss writes into ``%LOCALAPPDATA%\\CrashDumps``)
that has no macOS equivalent, so the high-level :func:`launch_and_test`
runs on Windows only. The lower-level Steam helpers
(:func:`get_steam_app_id`) are pure-Python and work everywhere — the
fluent_window's launch path on macOS calls them directly.
"""
import atexit
import ctypes
import glob
import logging
import ntpath
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable

import psutil

logger = logging.getLogger(__name__)

# Aliased to ``_IS_WINDOWS`` (module-private convention) so existing
# in-file usages keep working without per-callsite churn. Routes through
# the central platform abstraction in cdumm.platform so a future
# refactor only has to update one place.
from cdumm.platform import IS_WINDOWS as _IS_WINDOWS

GAME_EXE_NAME = "CrimsonDesert.exe"
CRASH_DUMP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CrashDumps")
FALLBACK_APP_ID = "3321460"

# Sentinel returned by :func:`wait_for_exit` when the OpenProcess handle
# couldn't be acquired — typically because the process exited between
# :func:`find_game_process` reporting it and the subsequent wait. Keeps
# the int-or-None return shape (caller distinguishes ``None`` = still
# running). Distinct from "platform-unsupported" — that case raises
# NotImplementedError so callers can't silently mistake one for the
# other (PR #64 review note).
PROCESS_GONE = 0xDEAD

if _IS_WINDOWS:
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    _psapi = None
    _k32 = None

# Track crashpad state for atexit cleanup
_crashpad_bin64: str | None = None


def _atexit_restore():
    if _crashpad_bin64:
        restore_crashpad(Path(_crashpad_bin64))


atexit.register(_atexit_restore)


# ── Process management ────────────────────────────────────────────

def find_game_process() -> int | None:
    """Find CrimsonDesert.exe PID. Returns PID or None.

    Windows uses ``EnumProcesses`` from ``psapi.dll``. On macOS / Linux
    the game runs under Proton/Wine, so we scan ``psutil.process_iter``
    and match by the executable **basename** — the process command line
    carries the Windows-style path (e.g.
    ``S:\\common\\Crimson Desert\\bin64\\CrimsonDesert.exe``) even on
    Linux, so ``ntpath.basename`` is used to split it regardless of the
    host OS (``os.path.basename`` wouldn't split backslashes on posix).
    GitHub #195 / PR #201 (RoGreat): matching the basename — not a
    hard-coded install path — is what makes this work on any machine.
    """
    if not _IS_WINDOWS:
        target = GAME_EXE_NAME.lower()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline")
                if cmdline and ntpath.basename(cmdline[0]).lower() == target:
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess, IndexError):
                continue  # process vanished / not inspectable — skip it
        return None
    arr = (ctypes.c_ulong * 4096)()
    needed = ctypes.c_ulong()
    if not _psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
        return None
    buf = ctypes.create_string_buffer(260)
    count = needed.value // ctypes.sizeof(ctypes.c_ulong)
    for i in range(count):
        pid = arr[i]
        if pid == 0:
            continue
        h = _k32.OpenProcess(0x0410, False, pid)
        if h:
            if _psapi.GetModuleBaseNameA(h, None, buf, 260):
                name = buf.value.decode("ascii", errors="replace").lower()
                if name == GAME_EXE_NAME.lower():
                    _k32.CloseHandle(h)
                    return pid
            _k32.CloseHandle(h)
    return None


def wait_for_exit(pid: int, timeout_ms: int) -> int | None:
    """Wait for process to exit. Returns exit code, or None if still running.

    If the process is already gone (can't open handle), returns
    :data:`PROCESS_GONE` as a sentinel to distinguish from "still
    running" (None). On macOS / Linux the wait is done with
    ``psutil.Process.wait`` (the game runs under Proton): a timeout
    means still running (``None``); the process being gone / no longer
    inspectable means :data:`PROCESS_GONE`. Only those specific psutil
    errors are caught — a bare ``except`` would also swallow
    ``KeyboardInterrupt`` / ``SystemExit`` (PR #201 review).
    """
    if not _IS_WINDOWS:
        try:
            psutil.Process(pid).wait(timeout=timeout_ms / 1000)
            return PROCESS_GONE          # exited within the timeout
        except psutil.TimeoutExpired:
            return None                  # still running
        except psutil.Error:
            return PROCESS_GONE          # NoSuchProcess / gone / inaccessible
    h = _k32.OpenProcess(0x00100400, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_INFORMATION
    if not h:
        return PROCESS_GONE  # process already gone before we could open a handle
    try:
        r = _k32.WaitForSingleObject(h, timeout_ms)
        if r == 0:  # WAIT_OBJECT_0
            ec = ctypes.c_ulong()
            _k32.GetExitCodeProcess(h, ctypes.byref(ec))
            return ec.value
        return None  # still running
    finally:
        _k32.CloseHandle(h)


def kill_process(pid: int) -> None:
    """Terminate a process by PID. Cross-platform: ``TerminateProcess``
    on Windows, ``SIGTERM`` under Proton on macOS / Linux."""
    if not _IS_WINDOWS:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass  # already gone / not ours — nothing to kill
        return
    h = _k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if h:
        _k32.TerminateProcess(h, 0)
        _k32.CloseHandle(h)


# ── Crashpad control ──────────────────────────────────────────────

def disable_crashpad(bin64: Path) -> None:
    """Rename crashpad_handler.exe to prevent it intercepting crashes."""
    global _crashpad_bin64
    src = bin64 / "crashpad_handler.exe"
    dst = bin64 / "crashpad_handler.exe.monitoring"
    if src.exists() and not dst.exists():
        os.rename(src, dst)
        _crashpad_bin64 = str(bin64)


def restore_crashpad(bin64: Path) -> None:
    """Restore crashpad_handler.exe after monitoring."""
    global _crashpad_bin64
    src = bin64 / "crashpad_handler.exe.monitoring"
    dst = bin64 / "crashpad_handler.exe"
    if src.exists() and not dst.exists():
        os.rename(src, dst)
    _crashpad_bin64 = None


# ── Crash evidence ────────────────────────────────────────────────

def delete_old_dumps() -> None:
    """Clear old game crash dumps."""
    for f in glob.glob(os.path.join(CRASH_DUMP_DIR, "CrimsonDesert.exe*.dmp")):
        try:
            os.remove(f)
        except OSError:
            pass


def find_latest_dump(after_ts: float) -> Path | None:
    """Find newest game dump created after timestamp."""
    dumps = glob.glob(os.path.join(CRASH_DUMP_DIR, "CrimsonDesert.exe*.dmp"))
    valid = []
    for f in dumps:
        try:
            mt = os.path.getmtime(f)
            if mt > after_ts:
                valid.append((f, mt))
        except OSError:
            continue
    if valid:
        return Path(max(valid, key=lambda x: x[1])[0])
    return None


# ── Steam integration ─────────────────────────────────────────────

def _sanitise_app_id(raw: str) -> str | None:
    """Return ``raw`` if it is a clean numeric app id, else None.

    Faisal 2026-05-12 GitHub #88 (zvitko-hue): Franci's steam_appid.txt
    or its source equivalent contained an embedded null character.
    ``.strip()`` only removes leading/trailing whitespace, not inner
    nulls, so the resulting id was something like "3321460\x00".
    Python's ``os.startfile`` rejects strings containing embedded
    nulls with ``ValueError: startfile: embedded null character in
    filepath`` and the launcher silently bailed. Steam app ids are
    always purely numeric, so the fix is to filter to digits and
    require the result is non-empty; otherwise the caller falls back
    to the known-good FALLBACK_APP_ID.
    """
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    return digits or None


def get_steam_app_id(game_dir: Path) -> str:
    """Detect Steam app ID for Crimson Desert.

    Resolution order (GitHub #186 reordered the first two):
      1. The Crimson Desert ``appmanifest_<id>.acf`` in steamapps.
         This is Steam's own authoritative record of the installed
         game and the exact id Steam uses for ``steam://rungameid``.
      2. ``steam_appid.txt`` in the game root / bin64. This is a
         developer / DRM helper file that can be stale or corrupt.
      3. The known-good fallback ``3321460``.

    lupo1190 (GitHub #186) had a steam_appid.txt that resolved to the
    truncated id ``332146`` (the real id 3321460 with the trailing 0
    dropped). Checking steam_appid.txt first meant CDUMM launched
    ``steam://rungameid/332146`` and Steam answered 'Game configuration
    unavailable'. The appmanifest (appmanifest_3321460.acf) was present
    and correct the whole time, so it now takes precedence.
    """
    steamapps = game_dir.parent.parent
    if steamapps.is_dir():
        try:
            entries = os.listdir(steamapps)
        except OSError:
            entries = []
        for fn in entries:
            if fn.startswith("appmanifest_") and fn.endswith(".acf"):
                try:
                    content = (steamapps / fn).read_text(encoding="utf-8")
                    if "Crimson Desert" in content:
                        manifest_id = fn.replace(
                            "appmanifest_", "").replace(".acf", "")
                        cleaned = _sanitise_app_id(manifest_id)
                        if cleaned:
                            return cleaned
                except OSError:
                    pass
    for sub in ["", "bin64"]:
        appid_file = game_dir / sub / "steam_appid.txt"
        if appid_file.exists():
            try:
                cleaned = _sanitise_app_id(
                    appid_file.read_text(encoding="utf-8"))
                if cleaned:
                    return cleaned
            except OSError:
                pass
    return FALLBACK_APP_ID


def launch_via_steam(game_dir: Path) -> float:
    """Launch game through Steam URI. Returns launch timestamp.

    Cross-platform: uses ``os.startfile`` on Windows (so the Steam
    overlay attaches), and ``open <url>`` on macOS / ``xdg-open`` on
    Linux for the same effect when a Steam client is installed. The
    higher-level :func:`launch_and_test` flow is still Windows-only
    because it needs Pearl Abyss' crashpad ``.dmp`` files.
    """
    app_id = get_steam_app_id(game_dir)
    ts = time.time()
    url = f"steam://rungameid/{app_id}"
    if _IS_WINDOWS:
        os.startfile(url)
    else:
        from cdumm.platform import open_path
        open_path(url)
    return ts


# ── High-level: launch, monitor, detect crash ─────────────────────

def launch_and_test(
    game_dir: Path,
    stable_seconds: int = 45,
    launch_timeout: int = 60,
    log_cb: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Launch game, wait for crash or stability. Returns True if crashed.

    Two-timeout pattern:
    - launch_timeout: max seconds to wait for process to appear
    - stable_seconds: game must survive this long to count as "stable"
    """
    def _log(msg):
        if log_cb:
            log_cb(msg)
        logger.info("GameMonitor: %s", msg)

    bin64 = game_dir / "bin64"

    # Wait for any previous game instance to exit
    if find_game_process():
        _log("Waiting for previous game instance to exit...")
        for _ in range(30):
            if cancel_check and cancel_check():
                _log("Cancelled during pre-exit wait")
                return False
            if not find_game_process():
                break
            time.sleep(1)

    disable_crashpad(bin64)
    delete_old_dumps()

    try:  # try/finally guarantees crashpad restore
        launch_ts = launch_via_steam(game_dir)
        _log("Launched through Steam, waiting for process...")

        # Wait for process to appear
        pid = None
        for i in range(launch_timeout):
            if cancel_check and cancel_check():
                _log("Cancelled during launch wait")
                return False
            pid = find_game_process()
            if pid:
                break
            time.sleep(1)

        if not pid:
            _log(f"Game process not found after {launch_timeout}s — treating as crash")
            return True

        _log(f"Game detected (PID {pid}) — monitoring for {stable_seconds}s...")

        # Monitor for stability
        start = time.time()
        while time.time() - start < stable_seconds:
            if cancel_check and cancel_check():
                _log("Cancelled — killing game")
                kill_process(pid)
                time.sleep(2)
                return False

            exit_code = wait_for_exit(pid, 1000)
            if exit_code is not None:
                elapsed = time.time() - start
                _log(f"CRASHED after {elapsed:.0f}s (exit code {exit_code})")
                return True

        # Survived — kill and move on
        _log(f"Stable — survived {stable_seconds}s. Killing game...")
        kill_process(pid)
        # Wait for process to fully exit
        for _ in range(10):
            if not find_game_process():
                break
            time.sleep(1)

        return False

    except Exception as e:
        _log(f"Error during monitoring: {e}")
        return True  # treat errors as crash to be safe
    finally:
        restore_crashpad(bin64)
