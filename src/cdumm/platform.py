"""Platform abstraction layer.

Centralises the OS-specific decisions that the rest of the codebase
makes ad hoc, so adding a new platform is one file diff instead of
fifteen. CDUMM is a Windows app at heart — the Crimson Desert client is
Windows-only — but the manager itself runs natively on macOS and Linux
so users on Whisky / CrossOver / Wine can manage mods without booting
into the Wine prefix every time.

Three things this module owns:

1. ``app_data_dir()`` — the per-user state directory CDUMM writes to.
   Windows: ``%LOCALAPPDATA%\\cdumm`` (i.e. ``~/AppData/Local/cdumm``).
   macOS:   ``~/Library/Application Support/cdumm``.
   Linux:   ``$XDG_DATA_HOME/cdumm`` (defaults to ``~/.local/share/cdumm``).

2. ``open_path(path)`` — the cross-platform replacement for
   ``os.startfile``. Hands a file or URL to whatever the user's OS
   considers the default opener (Explorer / Finder / xdg-open).

3. ``IS_WINDOWS`` / ``IS_MACOS`` / ``IS_LINUX`` flags so callers don't
   keep re-checking ``sys.platform`` strings.

Anything that touches the registry, the Win32 API, or NTFS-specific
behaviour stays guarded by ``IS_WINDOWS``. Anything that talks about
``bin64/CrimsonDesert.exe`` lives in the Wine prefix on macOS / Linux,
not on the host filesystem — but the Path object is the same shape
either way, so callers don't need to care.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def app_data_dir() -> Path:
    """Per-user CDUMM state directory.

    Returns the directory where CDUMM stores its log, single-instance
    lock, ``game_dir.txt`` pointer, the welcome-wizard marker, and any
    pre-CDMods migration database. Creating it is the caller's job —
    this function just returns the path.

    Historical: prior to v3.2 the path was hard-coded to
    ``Path.home() / "AppData" / "Local" / "cdumm"`` everywhere, which
    is a Windows-style path that happens to work as a regular folder
    on POSIX (Python's pathlib is happy with literal "AppData" as a
    directory name) — but stuffing app state into ``~/AppData`` on
    macOS and Linux is a faux pas, and on macOS it lives outside the
    Library tree where Time Machine and `defaults` expect it. This
    helper picks the right per-platform location.
    """
    if IS_WINDOWS:
        # %LOCALAPPDATA% is the canonical location; fall back to the
        # constructed path for the (rare) profile that has the env
        # var unset.
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "cdumm"
        return Path.home() / "AppData" / "Local" / "cdumm"
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "cdumm"
    # Linux / other POSIX: respect XDG_DATA_HOME.
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "cdumm"
    return Path.home() / ".local" / "share" / "cdumm"


def open_path(path: str | Path) -> bool:
    """Hand ``path`` (a file, folder, or URL) to the OS default opener.

    Replacement for ``os.startfile``, which only exists on Windows.
    Returns True when the launcher fired; False when no opener was
    available or the call raised.

    Errors are caught and logged at WARNING level (with the failing
    path + the underlying OSError detail) so callers don't have to
    wrap each call in their own try/except. The previous version
    silently swallowed every exception, which lost the diagnostic
    detail the old ``os.startfile`` raise gave us — review feedback
    on PR #64 flagged that callers like ``mods_page._ctx_open_source``
    used to log the OSError message themselves and now show a
    generic "OS opener unavailable". Logging here at the source
    means every callsite gets free OSError detail in the user's log
    file without needing to change.

    On macOS and Linux this also handles ``steam://`` / ``nxm://`` URLs
    correctly because both ``open`` and ``xdg-open`` route URL schemes
    through the desktop's registered handlers. On Windows
    ``os.startfile`` did the same.
    """
    target = str(path)
    try:
        if IS_WINDOWS:
            os.startfile(target)  # noqa: pyl-no-startfile -- Windows-only branch
            return True
        if IS_MACOS:
            subprocess.Popen(["open", target])
            return True
        # Linux / other POSIX: prefer xdg-open, fall back to gio open.
        for tool in ("xdg-open", "gio"):
            if shutil.which(tool):
                if tool == "gio":
                    subprocess.Popen(["gio", "open", target])
                else:
                    subprocess.Popen([tool, target])
                return True
        logger.warning(
            "open_path: no opener found for %s "
            "(tried xdg-open, gio); install xdg-utils or gnome-vfs", target)
        return False
    except OSError as e:
        logger.warning("open_path: failed to open %s: %s", target, e)
        return False
    except Exception as e:
        logger.warning(
            "open_path: unexpected error opening %s: %s", target, e)
        return False


def worker_command(extra_args: list[str]) -> tuple[str, list[str]]:
    """Return ``(exe, args)`` for spawning a CDUMM worker subprocess.

    CDUMM's worker mode (``--worker <subcmd> ...``) is invoked via
    QProcess for snapshots, applies, imports, etc. The Windows
    PyInstaller build packages everything as a single ``CDUMM.exe``
    that re-launches itself; on the frozen build ``sys.executable``
    points at that exe and a plain ``[exe, "--worker", ...]`` works.

    Run-from-source (the macOS / Linux dev path until packaged
    ``.app`` / ``.AppImage`` builds exist) is different: ``sys.executable``
    is the Python interpreter, and ``[python3, "--worker", ...]``
    causes Python to reject ``--worker`` as an unknown command-line
    flag — the subprocess dies before reaching ``cdumm.main`` and
    every snapshot / apply / import worker silently no-ops.

    This helper picks the right pair: ``-m cdumm.main`` is prepended
    in run-from-source so the interpreter actually runs CDUMM's
    entry point first, then sees ``--worker``. Symptom of the bug
    this prevents: snapshot scans report ``0 files indexed`` and
    Apply / Import refuse to run.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, list(extra_args)
    return sys.executable, ["-m", "cdumm.main", *extra_args]


def subprocess_no_window_kwargs() -> dict:
    """Return ``subprocess.Popen``/``subprocess.run`` kwargs that
    suppress the brief console window flash on Windows.

    The Windows ``console=False`` PyInstaller exe transiently allocates
    a console handle for any GUI-targeting child unless we pass
    ``CREATE_NO_WINDOW``; that flash appears every time CDUMM spawns
    7-Zip, ``--worker`` subprocesses, etc. On non-Windows the flag
    doesn't exist and shouldn't be passed (the kwarg is rejected).

    Use as ``subprocess.run(cmd, **subprocess_no_window_kwargs())``.
    """
    if not IS_WINDOWS:
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flag}
