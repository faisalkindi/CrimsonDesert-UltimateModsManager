"""GitHub #457 (madcatnoir, macOS v2.04.00): the bug report's installation
checks always looked for ``bin64/CrimsonDesert.exe``, a Windows/Linux(Proton)
layout. The native macOS build is a ``Crimson Desert.app`` bundle with no
``bin64/`` and no ``.exe`` at all (MACOS.md), so on every macOS report this
unconditionally flagged "bin64/ folder missing — game install is incomplete
or the game_dir is wrong" and "Game Exe: ... (MISSING)" even for a perfectly
valid install, telling the user their setup was broken when it wasn't.
"""
from pathlib import Path

from cdumm.gui import bug_report


def _make_valid_macos_game_dir(tmp_path: Path) -> Path:
    game_dir = tmp_path / "packages"
    (game_dir / "0008").mkdir(parents=True)
    (game_dir / "0008" / "0.paz").touch()
    (game_dir / "meta").mkdir(parents=True)
    (game_dir / "meta" / "0.papgt").touch()
    (game_dir / "meta" / "0.pathc").touch()
    return game_dir


def test_macos_report_does_not_flag_missing_bin64(tmp_path, monkeypatch, db):
    monkeypatch.setattr(bug_report, "IS_MACOS", True)
    game_dir = _make_valid_macos_game_dir(tmp_path)

    report = bug_report.generate_bug_report(db, game_dir, tmp_path / "appdata")

    assert "bin64/ folder missing" not in report
    assert "game_dir is wrong" not in report
    assert "Game Exe:" not in report
    assert "MISSING" not in report


def test_windows_report_still_flags_missing_bin64(tmp_path, monkeypatch, db):
    """Guard against over-correcting: Windows/Linux installs really do need
    bin64/, so the check must still fire there."""
    monkeypatch.setattr(bug_report, "IS_MACOS", False)
    game_dir = tmp_path / "Crimson Desert"
    game_dir.mkdir(parents=True)

    report = bug_report.generate_bug_report(db, game_dir, tmp_path / "appdata")

    assert "bin64/ folder missing" in report
