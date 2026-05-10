"""Tests for the ConfigPanel resize handle (Task 2.1)."""
from __future__ import annotations

import pytest


@pytest.fixture
def app(qtbot):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _show_simple_mod(panel, qtbot):
    panel.show_mod(
        mod_id=1, name="t", author="x", version="1",
        status="active", file_count=1,
        patches=[{"label": "p", "enabled": True}],
        conflicts=[],
    )


def test_panel_has_resize_handle(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    assert hasattr(panel, "_resize_handle")
    assert panel._resize_handle is not None


def test_resize_handle_cursor_is_size_hor(qtbot, app):
    from PySide6.QtCore import Qt
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    assert panel._resize_handle.cursor().shape() == Qt.CursorShape.SizeHorCursor


def test_set_panel_width_clamps_to_min(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_panel_width(100)  # below min
    assert panel._PANEL_WIDTH == 480


def test_set_panel_width_clamps_to_max(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_panel_width(2000)  # above max
    assert panel._PANEL_WIDTH == 1200


def test_set_panel_width_in_range(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_panel_width(800)
    assert panel._PANEL_WIDTH == 800


def test_set_panel_width_resizes_visible_panel(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    _show_simple_mod(panel, qtbot)
    panel.set_panel_width(800)
    # The panel's actual width should reflect the new value
    # (animation may still be running, but maxWidth should be set).
    assert panel.maximumWidth() == 800


# ----------------------------------------------------------------------
# Bug #5 (scottykyzer, Nexus): "It can't be resized, so text runs off
# the screen." The handle exists in code; verify it is BOTH visible
# (renders pixels at the right edge) AND functional (drag actually
# changes the panel's maximumWidth).
# ----------------------------------------------------------------------


def test_resize_handle_visible_after_show_mod(qtbot, app):
    """The handle widget exists, is visible, has a non-zero width, and
    is positioned at the panel's right edge after show_mod.

    If this fails, the handle exists in source but is invisible to the
    user — which would explain the Nexus bug report.
    """
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)

    # Force a known width and skip animation so geometry is final.
    panel.set_panel_width(800)
    _show_simple_mod(panel, qtbot)
    if hasattr(panel, "_anim"):
        panel._anim.stop()
    panel.setMaximumWidth(800)
    panel.setMinimumWidth(800)
    panel.resize(800, 600)
    panel.show()
    qtbot.wait(150)

    handle = panel._resize_handle
    assert handle is not None, "resize handle missing"
    assert handle.isVisible(), (
        "resize handle is not visible after show_mod — user has nothing "
        "to grab. This matches Nexus bug #5 (scottykyzer)."
    )
    geom = handle.geometry()
    assert geom.width() >= 4, (
        f"handle width {geom.width()} is too small to grab"
    )
    assert geom.height() > 0, f"handle has no height: {geom.height()}"
    # Right-edge anchored: x should be panel_width - handle_width.
    assert geom.x() == panel.width() - geom.width(), (
        f"handle not at right edge: x={geom.x()}, panel_w={panel.width()}, "
        f"handle_w={geom.width()}"
    )


def test_drag_changes_maximum_width(qtbot, app):
    """Synthesise a drag on the handle and verify maximumWidth tracks
    the cursor delta (with sign-flip per the docstring: drag LEFT
    widens, drag RIGHT shrinks).

    If this fails, the handle is visible but unresponsive — the other
    half of the Nexus bug.
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    _show_simple_mod(panel, qtbot)
    panel.set_panel_width(700)
    if hasattr(panel, "_anim"):
        panel._anim.stop()
    panel.setMaximumWidth(700)
    panel.setMinimumWidth(0)  # let the drag shrink/grow
    panel.resize(700, 600)
    panel.show()
    qtbot.wait(50)

    initial_max = panel.maximumWidth()
    handle = panel._resize_handle

    # Start cursor at global X = 1000 (arbitrary anchor).
    start_global_x = 1000.0
    drag_dx = -120.0  # drag LEFT 120 px → expected widen by 120

    # Build synthetic press / move events.
    local_pos = QPointF(2.0, 5.0)
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        local_pos,
        QPointF(start_global_x, 5.0),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    handle.mousePressEvent(press)

    move = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        local_pos,
        QPointF(start_global_x + drag_dx, 5.0),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,  # button held during move
        Qt.KeyboardModifier.NoModifier,
    )
    handle.mouseMoveEvent(move)

    expected = initial_max + int(-drag_dx)  # sign-flip per impl
    actual = panel.maximumWidth()
    # Clamp expectation to the panel's [MIN, MAX] range.
    expected_clamped = max(panel._MIN_PANEL_WIDTH,
                           min(panel._MAX_PANEL_WIDTH, expected))
    assert abs(actual - expected_clamped) <= 2, (
        f"drag did not move maximumWidth: initial={initial_max}, "
        f"after_drag={actual}, expected≈{expected_clamped}"
    )

    # Release with no DB wired — should be a clean no-op via persist.
    release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        local_pos,
        QPointF(start_global_x + drag_dx, 5.0),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    handle.mouseReleaseEvent(release)
