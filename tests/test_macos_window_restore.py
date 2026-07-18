"""Regression tests for macOS hide-on-launch window recovery."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSystemTrayIcon

import cdumm.gui.app_icon as app_icon
import cdumm.gui.fluent_window as fluent_window
from cdumm.gui.fluent_window import CdummWindow


def test_frozen_macos_icon_uses_bundled_png(monkeypatch, tmp_path):
    logo = tmp_path / "assets" / "cdumm-logo.png"
    logo.parent.mkdir()
    logo.write_bytes(b"png fixture")

    monkeypatch.setattr(app_icon, "IS_MACOS", True)
    monkeypatch.setattr(app_icon, "IS_LINUX", False)
    monkeypatch.setattr(app_icon.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_icon.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert app_icon.application_icon_path() == logo


def test_macos_does_not_override_native_dock_icon(monkeypatch):
    calls = []
    target = SimpleNamespace(
        setWindowIcon=lambda icon: calls.append(icon))
    monkeypatch.setattr(app_icon, "IS_MACOS", True)

    applied = app_icon.apply_application_icon(target)

    assert applied is False
    assert calls == []


def _fake_window(*, visible: bool, minimized: bool):
    calls = []
    window = SimpleNamespace(
        isVisible=lambda: visible,
        isMinimized=lambda: minimized,
        _restore_from_tray=lambda: calls.append("restore"),
    )
    return window, calls


def test_macos_dock_activation_restores_hidden_window(monkeypatch):
    monkeypatch.setattr(fluent_window, "IS_MACOS", True)
    window, calls = _fake_window(visible=False, minimized=False)

    CdummWindow._on_application_state_changed(
        window, Qt.ApplicationState.ApplicationActive)

    assert calls == ["restore"]


def test_macos_inactive_event_does_not_restore(monkeypatch):
    monkeypatch.setattr(fluent_window, "IS_MACOS", True)
    window, calls = _fake_window(visible=False, minimized=False)

    CdummWindow._on_application_state_changed(
        window, Qt.ApplicationState.ApplicationInactive)

    assert calls == []


def test_visible_window_is_not_needlessly_restored(monkeypatch):
    monkeypatch.setattr(fluent_window, "IS_MACOS", True)
    window, calls = _fake_window(visible=True, minimized=False)

    CdummWindow._on_application_state_changed(
        window, Qt.ApplicationState.ApplicationActive)

    assert calls == []


def test_single_click_on_menu_bar_icon_restores_window():
    calls = []
    window = SimpleNamespace(
        _restore_from_tray=lambda: calls.append("restore"))

    CdummWindow._on_tray_activated(
        window, QSystemTrayIcon.ActivationReason.Trigger)

    assert calls == ["restore"]


def test_hidden_macos_tray_quit_shows_window_before_close(monkeypatch):
    calls = []
    tray = SimpleNamespace(hide=lambda: calls.append("hide-tray"))
    window = SimpleNamespace(
        _tray_icon=tray,
        isVisible=lambda: False,
        show=lambda: calls.append("show-window"),
        close=lambda: calls.append("close-window"),
    )
    monkeypatch.setattr(fluent_window, "IS_MACOS", True)

    CdummWindow._quit_from_tray(window)

    assert calls == ["hide-tray", "show-window", "close-window"]


def test_macos_close_event_does_not_reenter_qapplication_quit():
    source = inspect.getsource(CdummWindow.closeEvent)
    guard = source.index("if IS_MACOS:")
    forced_quit = source.index("_qapp.quit()")

    assert guard < forced_quit
