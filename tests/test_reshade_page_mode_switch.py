"""ReShadePage mode switching: the page body must rebuild correctly when
the detect result flips between installed / not_installed / error.

We don't actually install ReShade — we monkeypatch detect_reshade_install
to return canned results, then assert the page ends up with the right
top-level widget for each state.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytest_qt = pytest.importorskip("pytestqt")

from cdumm.engine.reshade_detect import ReshadeInstall
from cdumm.i18n import load as load_translations

# tr() looks up strings in a module-level dict that starts empty. Load English
# once for this module so UI labels come out as the real text, not raw keys.
load_translations("en")


def _installed_result(bin64: Path, presets: int = 2) -> ReshadeInstall:
    return ReshadeInstall(
        state="installed",
        dll_path=bin64 / "dxgi.dll",
        ini_path=bin64 / "ReShade.ini",
        shaders_dir=bin64 / "reshade-shaders",
        presets=[bin64 / f"Preset{i}.ini" for i in range(presets)],
        base_path=bin64,
        error=None,
    )


def _not_installed_result(bin64: Path | None = None) -> ReshadeInstall:
    return ReshadeInstall(
        state="not_installed",
        dll_path=None, ini_path=None, shaders_dir=None,
        presets=[], base_path=bin64, error=None)


def _error_result(msg: str) -> ReshadeInstall:
    return ReshadeInstall(
        state="error",
        dll_path=None, ini_path=None, shaders_dir=None,
        presets=[], base_path=None, error=msg)


def _body_labels(page) -> list[str]:
    """Collect all BodyLabel / StrongBodyLabel / CaptionLabel text inside
    the page's body layout. Used to snoop which view is rendered."""
    from qfluentwidgets import BodyLabel, CaptionLabel, StrongBodyLabel
    found: list[str] = []
    stack = [page._body_layout]
    while stack:
        layout = stack.pop()
        for i in range(layout.count()):
            item = layout.itemAt(i)
            w = item.widget()
            if w is not None:
                # Recurse into child layouts of this widget
                if w.layout() is not None:
                    stack.append(w.layout())
                if isinstance(w, (BodyLabel, CaptionLabel, StrongBodyLabel)):
                    found.append(w.text())
            elif item.layout() is not None:
                stack.append(item.layout())
    return found


def test_page_starts_blank_without_game_dir(qtbot):
    from cdumm.gui.pages.reshade_page import ReshadePage
    page = ReshadePage()
    qtbot.addWidget(page)
    # No set_managers call -> refresh() is a no-op, body stays empty.
    assert page.current_state is None
    assert page._body_layout.count() == 0


def test_page_mode_switch_rebuilds_ui_for_all_three_states(qtbot, tmp_path):
    from cdumm.gui.pages.reshade_page import ReshadePage
    bin64 = tmp_path / "bin64"
    bin64.mkdir()

    page = ReshadePage()
    qtbot.addWidget(page)
    page._game_dir = tmp_path  # skip set_managers' initial refresh side-effect

    # State 1: not_installed -> install wizard card
    with patch("cdumm.gui.pages.reshade_page.detect_reshade_install",
               return_value=_not_installed_result(bin64)):
        page.refresh()
    assert page.current_state == "not_installed"
    labels = _body_labels(page)
    assert any("not installed" in t.lower() for t in labels), labels

    # State 2: error -> error card
    with patch("cdumm.gui.pages.reshade_page.detect_reshade_install",
               return_value=_error_result("PermissionError: denied")):
        page.refresh()
    assert page.current_state == "error"
    labels = _body_labels(page)
    assert any("couldn't check" in t.lower() for t in labels), labels
    assert any("PermissionError" in t for t in labels), labels

    # State 3: installed -> installed summary
    with patch("cdumm.gui.pages.reshade_page.detect_reshade_install",
               return_value=_installed_result(bin64, presets=3)):
        page.refresh()
    assert page.current_state == "installed"
    labels = _body_labels(page)
    assert any("installed" in t.lower() for t in labels), labels
    # 3 preset files should surface in the summary
    assert any("3 preset" in t for t in labels), labels


def test_page_installed_empty_state_shows_no_presets_hint(qtbot, tmp_path):
    from cdumm.gui.pages.reshade_page import ReshadePage
    bin64 = tmp_path / "bin64"
    bin64.mkdir()

    result = _installed_result(bin64, presets=0)
    page = ReshadePage()
    qtbot.addWidget(page)
    page._game_dir = tmp_path

    with patch("cdumm.gui.pages.reshade_page.detect_reshade_install",
               return_value=result):
        page.refresh()

    labels = _body_labels(page)
    assert any("no preset files were found" in t.lower() for t in labels), labels


def test_refresh_is_noop_without_game_dir(qtbot):
    from cdumm.gui.pages.reshade_page import ReshadePage
    page = ReshadePage()
    qtbot.addWidget(page)
    # No game_dir -> refresh should silently return, not touch the layout.
    with patch("cdumm.gui.pages.reshade_page.detect_reshade_install") as m:
        page.refresh()
    m.assert_not_called()
    assert page._body_layout.count() == 0


def test_set_managers_triggers_initial_detect(qtbot, tmp_path):
    """First set_managers call with a game_dir should run detection."""
    from cdumm.gui.pages.reshade_page import ReshadePage
    page = ReshadePage()
    qtbot.addWidget(page)

    with patch("cdumm.gui.pages.reshade_page.detect_reshade_install",
               return_value=_not_installed_result()) as m:
        page.set_managers(game_dir=tmp_path)
    m.assert_called_once()
    assert page.current_state == "not_installed"
