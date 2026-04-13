"""Game process monitoring for automated mod bisection.

Launches Crimson Desert through Steam, monitors the process for crashes,
and reports whether the game survived a stability window. Pure utility
module — no GUI, no Qt dependencies.

Ported from CDCrashMonitor v3 (cd_crash_monitor.py).
"""
import atexit
import ctypes
import glob
import logging
import os
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

GAME_EXE_NAME = "CrimsonDesert.exe"
CRASH_DUMP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CrashDumps")
FALLBACK_APP_ID = "3321460"

if os.name == "nt":
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
    """Find CrimsonDesert.exe PID. Returns PID or None."""
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

    If the process is already gone (can't open handle), returns 0xDEAD
    as a sentinel to distinguish from "still running" (None).
    """
    h = _k32.OpenProcess(0x00100400, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_INFORMATION
    if not h:
        return 0xDEAD  # process already gone
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
    """Terminate a process by PID."""
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

def get_steam_app_id(game_dir: Path) -> str:
    """Detect Steam app ID for Crimson Desert."""
    for sub in ["", "bin64"]:
        appid_file = game_dir / sub / "steam_appid.txt"
        if appid_file.exists():
            try:
                return appid_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    steamapps = game_dir.parent.parent
    if steamapps.is_dir():
        for fn in os.listdir(steamapps):
            if fn.startswith("appmanifest_") and fn.endswith(".acf"):
                try:
                    content = (steamapps / fn).read_text(encoding="utf-8")
                    if "Crimson Desert" in content:
                        return fn.replace("appmanifest_", "").replace(".acf", "")
                except OSError:
                    pass
    return FALLBACK_APP_ID


def launch_via_steam(game_dir: Path) -> float:
    """Launch game through Steam URI. Returns launch timestamp."""
    app_id = get_steam_app_id(game_dir)
    ts = time.time()
    os.startfile(f"steam://rungameid/{app_id}")
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
