"""Regression for GitHub #200 (lupo1190): the preset-picker dialog's
Install/Cancel buttons get pushed off the bottom of the window when a
mod ships many presets.

The dialog is a MessageBoxBase whose content is title + header +
(optional) multi-select button + a scrollable preset list + the
Install/Cancel button row. The #184 change scaled the scroll
viewport's minimum height to 45 percent of the parent height, but
nothing bounded the dialog's TOTAL height against the parent window.
With a long preset list the scroll viewport grew the dialog past the
window, so the action buttons rendered below the visible area and the
user could not click Install.

The fix caps the scroll viewport so the whole dialog always fits
inside the parent window with room for the buttons. The list scrolls
internally instead of growing the dialog. These tests pin that the
dialog's preferred height never exceeds the parent window height,
while a roomy window still gets a tall list (the #184 guarantee).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pytestqt")

from cdumm.i18n import load as load_translations

load_translations("en")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _make_parent(qtbot, width: int, height: int):
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    parent.resize(width, height)
    qtbot.addWidget(parent)
    return parent


def _many_presets(tmp_path, n=10):
    """Build n presets, each with a couple of labelled changes so the
    rows carry a Customize button (the tall-row case from #200)."""
    presets = []
    for i in range(1, n + 1):
        changes = [
            {"label": f"[Opt{j}] choice {j}", "offset": j * 4, "value": "1"}
            for j in range(2)
        ]
        fp = tmp_path / f"{i:02d}.json"
        fp.write_text("{}", encoding="utf-8")
        presets.append(
            (fp, {"name": f"{i:02d} - long preset name variant {i}",
                  "patches": [{"game_file": "x.pabgb", "changes": changes}]}))
    return presets


def _make_picker(qtbot, parent, presets):
    from cdumm.gui.preset_picker import PresetPickerDialog
    dlg = PresetPickerDialog(presets, parent)
    qtbot.addWidget(dlg)
    return dlg


@pytest.mark.parametrize("par_h", [1000, 700, 520, 460])
def test_dialog_fits_inside_parent_window(qtbot, app, tmp_path, par_h):
    """With a long preset list the dialog's preferred height must not
    exceed the parent window height, so the Install/Cancel buttons stay
    on-screen at every window size (#200)."""
    parent = _make_parent(qtbot, 1329, par_h)
    dlg = _make_picker(qtbot, parent, _many_presets(tmp_path, 10))
    need = dlg.widget.sizeHint().height()
    assert need <= par_h, (
        f"preset dialog wants {need}px but the window is only {par_h}px tall; "
        f"the Install/Cancel buttons would be clipped (overflow "
        f"{need - par_h}px)")


def test_large_window_still_shows_a_tall_list(qtbot, app, tmp_path):
    """The #184 guarantee: a roomy window still gets a tall scroll
    viewport so power users see many presets at once, not a tiny
    scroller. 45 percent of 1440 is 648; the cap only kicks in on
    small windows."""
    from qfluentwidgets import SingleDirectionScrollArea
    parent = _make_parent(qtbot, 2560, 1440)
    dlg = _make_picker(qtbot, parent, _many_presets(tmp_path, 10))
    scrolls = dlg.findChildren(SingleDirectionScrollArea)
    assert any(s.minimumHeight() >= 600 for s in scrolls), (
        f"large window should still get a tall list: "
        f"{[s.minimumHeight() for s in scrolls]}")
