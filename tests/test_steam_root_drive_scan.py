"""Drive-scan defensive behaviour for ``_find_steam_root``.

Bug report (spike1234, studoo12 on Nexus): users moved their Steam
install out of Program Files (per CDUMM's own warning), reopened
CDUMM, picked the new folder in the picker, and CDUMM hung — no
main window, no log.

Hypothesis: ``_find_steam_root`` (and the direct-scan fallback in
``find_game_directories``) iterates every drive letter A-Z calling
``Path("X:/Steam").exists()``. On Windows, ``exists()`` against a
disconnected or stale network drive blocks for ~30 s per drive
before timing out. A user with several stale mounts can stall
launch for minutes.

These tests pin the fix: on Windows, only the drive letters
``GetLogicalDrives`` reports as live get probed; everything else is
skipped. On non-Windows the existing behaviour is left alone.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="GetLogicalDrives is a Windows-only kernel32 export — the "
           "drive-letter blocking issue this guards against only "
           "manifests on Windows.")


_DRIVE_ROOT_PROBES = ("Steam", "SteamLibrary")


def _patched_path_exists(probed: list[str]):
    """Build a ``Path.exists`` replacement that records every probe.

    Only records drive-root probes the loop under test makes
    (``X:/Steam``, ``X:/SteamLibrary``). The explicit
    ``STEAM_DEFAULT_PATHS`` probes (Program Files variants) are NOT
    the bug — those are two specific paths, not a 26-letter sweep
    — so we let those fall through to the real ``exists`` and
    return ``False`` for the drive-root probes so the function
    exhausts its scan and returns ``None``.
    """
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        s = str(self).replace("\\", "/")
        # Match exactly the loop-under-test pattern: "X:/Steam"
        # or "X:/SteamLibrary" with nothing after.
        if (len(s) >= 4
                and s[1] == ":"
                and s[2] == "/"
                and s[0].isalpha()
                and s[3:] in _DRIVE_ROOT_PROBES):
            probed.append(s)
            return False
        return real_exists(self)

    return fake_exists


def test_find_steam_root_only_probes_live_drives() -> None:
    """``_find_steam_root`` must consult ``GetLogicalDrives`` and
    skip drive letters that aren't currently mounted.

    Bitmask ``0b10001`` = bit 0 (A:) and bit 4 (E:) set, so the
    only drive roots ever probed should be A:/ and E:/. The 24
    other letters must NOT be touched — that's the defence
    against stale-network-drive timeouts.
    """
    from cdumm.storage import game_finder

    probed: list[str] = []

    # Empty out STEAM_DEFAULT_PATHS so the function reaches the
    # drive-scan loop on a dev box that happens to have Steam at
    # one of the default Program Files locations.
    with patch.object(game_finder, "STEAM_DEFAULT_PATHS", []), \
         patch("ctypes.windll.kernel32.GetLogicalDrives",
               return_value=0b10001, create=True), \
         patch.object(Path, "exists", _patched_path_exists(probed)):
        result = game_finder._find_steam_root()

    assert result is None  # No live drive actually has Steam.

    probed_letters = {p[0].upper() for p in probed}
    # Only A: and E: are mounted in our mocked bitmask.
    assert probed_letters <= {"A", "E"}, (
        f"Unexpected drive letters probed: {probed_letters - {'A', 'E'}}. "
        f"Stale drive letters can block exists() for ~30 s each on "
        f"Windows; the live-drive filter must be in effect.")
    # Sanity: the live drives WERE probed (otherwise the scan
    # silently degrades to a no-op).
    assert "A" in probed_letters or "E" in probed_letters


def test_live_local_drives_decodes_bitmask() -> None:
    """``_live_local_drives`` is the helper that converts the
    GetLogicalDrives bitmask into a string of present drive
    letters. Bit N set means drive ``chr(ord('A') + N)`` is
    mounted. Verify the decoding directly so future changes to
    the scan loop can rely on it."""
    from cdumm.storage import game_finder

    with patch("ctypes.windll.kernel32.GetLogicalDrives",
               return_value=0b10001, create=True):
        live = game_finder._live_local_drives()

    assert set(live) == {"A", "E"}


def test_live_local_drives_full_bitmask() -> None:
    """All 26 bits set => all 26 letters returned. Belt-and-
    braces guard so the helper doesn't accidentally cap itself."""
    from cdumm.storage import game_finder

    full = (1 << 26) - 1
    with patch("ctypes.windll.kernel32.GetLogicalDrives",
               return_value=full, create=True):
        live = game_finder._live_local_drives()

    assert set(live) == set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def test_live_local_drives_empty_bitmask() -> None:
    """No drives mounted => empty string. The scan loops should
    then iterate zero times instead of hitting the 26-letter
    fallback."""
    from cdumm.storage import game_finder

    with patch("ctypes.windll.kernel32.GetLogicalDrives",
               return_value=0, create=True):
        live = game_finder._live_local_drives()

    assert live == ""
