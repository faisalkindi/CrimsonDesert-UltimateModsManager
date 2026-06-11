"""Xbox and Epic drive scans must honor the live-drive filter.

The Steam scan already routes through ``_drive_letters_for_scan`` /
``_live_local_drives`` so stale network drive letters (which can block
``Path.exists`` for ~30 s each on Windows) are never probed. The Xbox
Game Pass scan used to sweep all 26 letters twice (a .GamingRoot
pre-pass unioned with the full alphabet, which made the pre-pass a
no-op), and the Epic fallback swept A-Z too. These tests pin the fix:
both scans only probe the letters ``_drive_letters_for_scan`` returns.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cdumm.storage import game_finder


def _recording_exists(probed: list[str]):
    """Path.exists replacement that records drive-letter probes made
    by the loops under test and reports them absent. Explicit
    non-drive paths (Program Files, ProgramData) are also forced
    False so a dev box with a real Epic/Xbox install can't leak
    into the assertions."""
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        s = str(self).replace("\\", "/")
        if ("XboxGames" in s or ".GamingRoot" in s
                or "/Epic Games/" in s or "/EpicGames/" in s):
            probed.append(s)
            return False
        if "ModifiableWindowsApps" in s or "ProgramData/Epic" in s:
            return False
        return real_exists(self)

    return fake_exists


def test_xbox_scan_only_probes_live_drives() -> None:
    probed: list[str] = []
    with patch.object(game_finder, "_drive_letters_for_scan",
                      lambda: "E"), \
         patch.object(Path, "exists", _recording_exists(probed)):
        result = game_finder._find_xbox_game_pass()

    assert result == []
    drive_probes = [p for p in probed if len(p) > 1 and p[1] == ":"]
    assert drive_probes, "Xbox scan never probed the live drive at all"
    letters = {p[0].upper() for p in drive_probes}
    assert letters == {"E"}, (
        f"Xbox scan probed non-live drive letters: {letters - {'E'}}. "
        "Stale letters can block exists() ~30 s each on Windows.")


def test_xbox_scan_has_no_gamingroot_prepass() -> None:
    """The old .GamingRoot pre-pass swept all 26 letters before the
    main scan and its result was unioned away. It must be gone."""
    probed: list[str] = []
    with patch.object(game_finder, "_drive_letters_for_scan",
                      lambda: "E"), \
         patch.object(Path, "exists", _recording_exists(probed)):
        game_finder._find_xbox_game_pass()

    assert not any(".GamingRoot" in p for p in probed), (
        "Xbox scan still probes .GamingRoot markers; that pre-pass "
        "was a no-op (its result was unioned with the full alphabet) "
        "and reintroduces the 26-letter sweep.")


def test_epic_fallback_only_probes_live_drives(monkeypatch) -> None:
    # Keep the registry path out of the picture so only the
    # fallback drive loop runs.
    try:
        import winreg

        def _no_key(*args, **kwargs):
            raise OSError("no Epic registry key in this test")
        monkeypatch.setattr(winreg, "OpenKey", _no_key)
    except ImportError:
        pass  # non-Windows: _find_epic_games skips winreg anyway

    probed: list[str] = []
    with patch.object(game_finder, "_drive_letters_for_scan",
                      lambda: "E"), \
         patch.object(Path, "exists", _recording_exists(probed)):
        result = game_finder._find_epic_games()

    assert result == []
    drive_probes = [p for p in probed if len(p) > 1 and p[1] == ":"]
    assert drive_probes, "Epic fallback never probed the live drive"
    letters = {p[0].upper() for p in drive_probes}
    assert letters == {"E"}, (
        f"Epic fallback probed non-live drive letters: {letters - {'E'}}")
