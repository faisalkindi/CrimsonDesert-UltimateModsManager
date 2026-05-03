"""Loop R1: cmd_apply and cmd_launch_game must surface
ApplyWorker.warning signal output to stderr. Otherwise the
GitHub #62 vanilla-extraction error fix is silently dropped in
CLI mode — the whole point of #62 was to make the real cause
visible to debuggers/support."""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _setup_game_dir(tmp_path: Path) -> Path:
    game_dir = tmp_path / "game"
    bin64 = game_dir / "bin64"
    bin64.mkdir(parents=True)
    (bin64 / "CrimsonDesert.exe").write_bytes(b"\x4d\x5a")
    (game_dir / "CDMods").mkdir()
    (game_dir / "CDMods" / "cdumm.db").write_bytes(b"")
    return game_dir


class _FakeWorker:
    """ApplyWorker stub that emits a warning during run()."""
    SENTINEL_WARNING = "vanilla extraction failed for iteminfo.pabgb"

    def __init__(self, *args, **kwargs):
        self._warning_cb = None
        self.progress_updated = MagicMock()
        self.error_occurred = MagicMock()
        self.warning = MagicMock()
        self.progress_updated.connect = lambda f: None
        self.error_occurred.connect = lambda f: None
        self.warning.connect = self._capture_warning

    def _capture_warning(self, cb):
        self._warning_cb = cb

    def run(self):
        if self._warning_cb:
            self._warning_cb(self.SENTINEL_WARNING)


def test_cmd_apply_surfaces_warnings_to_stderr(tmp_path: Path,
                                              monkeypatch, capsys):
    game_dir = _setup_game_dir(tmp_path)

    from cdumm import cli
    monkeypatch.setattr(cli, "_resolve_game_dir", lambda override: game_dir)

    import cdumm.engine.apply_engine as ae
    monkeypatch.setattr(ae, "ApplyWorker", _FakeWorker)

    args = type("A", (), {"game_dir": str(game_dir)})()
    with pytest.raises(SystemExit):
        cli.cmd_apply(args)

    err = capsys.readouterr().err
    assert _FakeWorker.SENTINEL_WARNING in err, (
        f"cmd_apply did not surface warning {_FakeWorker.SENTINEL_WARNING!r} "
        f"to stderr. Captured stderr: {err!r}"
    )


def test_cmd_launch_game_surfaces_warnings_to_stderr(tmp_path: Path,
                                                    monkeypatch, capsys):
    game_dir = _setup_game_dir(tmp_path)

    from cdumm import cli
    from cdumm.engine import launcher
    monkeypatch.setattr(cli, "_resolve_game_dir", lambda override: game_dir)
    monkeypatch.setattr(launcher, "launch_game", lambda gd: None)

    import cdumm.engine.apply_engine as ae
    monkeypatch.setattr(ae, "ApplyWorker", _FakeWorker)

    args = type("A", (), {"game_dir": str(game_dir)})()
    with pytest.raises(SystemExit):
        cli.cmd_launch_game(args)

    err = capsys.readouterr().err
    assert _FakeWorker.SENTINEL_WARNING in err, (
        f"cmd_launch_game did not surface warning to stderr. Captured: {err!r}"
    )
