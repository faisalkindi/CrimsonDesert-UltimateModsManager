"""GitHub #63: `cdumm launch-game` subcommand runs the apply
pipeline first and then launches the game on success. On apply
failure, exits non-zero WITHOUT launching."""
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
    # cmd_launch_game checks for cdumm.db existence — touch a placeholder
    (game_dir / "CDMods" / "cdumm.db").write_bytes(b"")
    return game_dir


def test_launch_game_invokes_apply_then_launcher(tmp_path: Path,
                                                 monkeypatch):
    """Successful apply -> launch_game() called -> exit 0."""
    game_dir = _setup_game_dir(tmp_path)

    from cdumm import cli
    from cdumm.engine import launcher
    monkeypatch.setattr(cli, "_resolve_game_dir", lambda override: game_dir)

    # Stub apply: returns success (no errors emitted)
    apply_called = {"count": 0}

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            apply_called["count"] += 1
            self.progress_updated = MagicMock()
            self.error_occurred = MagicMock()
            self.progress_updated.connect = lambda f: None
            self.error_occurred.connect = lambda f: None

        def run(self):
            pass  # success — no error_occurred emit

    import cdumm.engine.apply_engine as ae
    monkeypatch.setattr(ae, "ApplyWorker", _FakeWorker)

    launch_called = {"count": 0}
    monkeypatch.setattr(launcher, "launch_game",
                        lambda gd: launch_called.update(count=launch_called["count"] + 1))

    args = type("A", (), {"game_dir": str(game_dir)})()

    with pytest.raises(SystemExit) as exc:
        cli.cmd_launch_game(args)

    assert exc.value.code == 0, f"Expected exit 0, got {exc.value.code}"
    assert apply_called["count"] == 1
    assert launch_called["count"] == 1


def test_launch_game_skips_launcher_when_apply_fails(tmp_path: Path,
                                                    monkeypatch):
    """Apply error -> launcher NOT called -> exit 1."""
    game_dir = _setup_game_dir(tmp_path)

    from cdumm import cli
    from cdumm.engine import launcher
    monkeypatch.setattr(cli, "_resolve_game_dir", lambda override: game_dir)

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            self._error_cb = None
            self.progress_updated = MagicMock()
            self.error_occurred = MagicMock()
            self.progress_updated.connect = lambda f: None
            self.error_occurred.connect = self._capture_error

        def _capture_error(self, cb):
            self._error_cb = cb

        def run(self):
            if self._error_cb:
                self._error_cb("simulated apply failure")

    import cdumm.engine.apply_engine as ae
    monkeypatch.setattr(ae, "ApplyWorker", _FakeWorker)

    launch_called = {"count": 0}
    monkeypatch.setattr(launcher, "launch_game",
                        lambda gd: launch_called.update(count=launch_called["count"] + 1))

    args = type("A", (), {"game_dir": str(game_dir)})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_launch_game(args)

    assert exc.value.code != 0, "Apply failure must exit non-zero"
    assert launch_called["count"] == 0, (
        "launcher.launch_game must NOT be called when apply fails — "
        "user expects fail-fast (don't launch into a broken state)."
    )
