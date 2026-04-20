"""Qt-level test for _ctx_open_source: make sure the right-click action
resolves the path, calls os.startfile when it exists, and shows an InfoBar
when it doesn't.

We don't spin up the full ModsPage (heavy). Instead we call the unbound
method against a lightweight fake_self that carries the handful of
attributes _ctx_open_source actually touches: _game_dir and window().
os.startfile is monkeypatched so the test doesn't actually open Explorer.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def _make_fake_self(tmp_path: Path) -> SimpleNamespace:
    """Minimum surface _ctx_open_source touches on `self`."""
    return SimpleNamespace(
        _game_dir=tmp_path,
        window=lambda: None,  # InfoBar parent can be None for our asserts
    )


def test_ctx_open_source_calls_startfile_when_path_exists(qtbot, tmp_path, monkeypatch):
    """Happy path: source_path exists, os.startfile gets called with it."""
    from cdumm.gui.pages.mods_page import ModsPage

    source = tmp_path / "mod-source"
    source.mkdir()
    mod = {"id": 1, "source_path": str(source)}

    startfile_calls: list[str] = []
    monkeypatch.setattr("os.startfile", lambda p: startfile_calls.append(p), raising=False)
    # Silence InfoBar (irrelevant to this test and needs a parent widget).
    from qfluentwidgets import InfoBar
    monkeypatch.setattr(InfoBar, "warning", MagicMock())
    monkeypatch.setattr(InfoBar, "error", MagicMock())

    fake_self = _make_fake_self(tmp_path)
    ModsPage._ctx_open_source(fake_self, 1, mod)

    assert startfile_calls == [str(source)]


def test_ctx_open_source_shows_infobar_when_no_path(qtbot, tmp_path, monkeypatch):
    """resolve returns None -> InfoBar.warning shown, os.startfile NOT called."""
    from cdumm.gui.pages.mods_page import ModsPage

    startfile_calls: list[str] = []
    monkeypatch.setattr("os.startfile", lambda p: startfile_calls.append(p), raising=False)
    warning_mock = MagicMock()
    error_mock = MagicMock()
    from qfluentwidgets import InfoBar
    monkeypatch.setattr(InfoBar, "warning", warning_mock)
    monkeypatch.setattr(InfoBar, "error", error_mock)

    mod = {"id": 999, "source_path": None}  # no fallback either
    fake_self = _make_fake_self(tmp_path)
    ModsPage._ctx_open_source(fake_self, 999, mod)

    assert startfile_calls == []
    assert warning_mock.called
    assert not error_mock.called


def test_ctx_open_source_shows_error_infobar_when_startfile_raises(qtbot, tmp_path, monkeypatch):
    """os.startfile raises OSError -> InfoBar.error, no crash."""
    from cdumm.gui.pages.mods_page import ModsPage

    source = tmp_path / "mod-source"
    source.mkdir()
    mod = {"id": 1, "source_path": str(source)}

    def boom(p):
        raise OSError("access denied")

    monkeypatch.setattr("os.startfile", boom, raising=False)
    error_mock = MagicMock()
    warning_mock = MagicMock()
    from qfluentwidgets import InfoBar
    monkeypatch.setattr(InfoBar, "error", error_mock)
    monkeypatch.setattr(InfoBar, "warning", warning_mock)

    fake_self = _make_fake_self(tmp_path)
    ModsPage._ctx_open_source(fake_self, 1, mod)  # must not raise

    assert error_mock.called
    assert not warning_mock.called


def test_ctx_open_source_early_returns_when_no_game_dir(qtbot, tmp_path, monkeypatch):
    """If the app has no game_dir configured, bail silently."""
    from cdumm.gui.pages.mods_page import ModsPage

    startfile_calls: list[str] = []
    monkeypatch.setattr("os.startfile", lambda p: startfile_calls.append(p), raising=False)
    warning_mock = MagicMock()
    error_mock = MagicMock()
    from qfluentwidgets import InfoBar
    monkeypatch.setattr(InfoBar, "warning", warning_mock)
    monkeypatch.setattr(InfoBar, "error", error_mock)

    fake_self = SimpleNamespace(_game_dir=None, window=lambda: None)
    ModsPage._ctx_open_source(fake_self, 1, {"id": 1, "source_path": None})

    assert startfile_calls == []
    assert not warning_mock.called
    assert not error_mock.called
