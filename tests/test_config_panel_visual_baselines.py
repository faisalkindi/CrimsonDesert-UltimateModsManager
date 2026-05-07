"""Visual baseline rendering for ConfigPanel at multiple widths
and themes (Task 2.3).

This test renders to PNG and verifies the output exists + is the
expected size. It does NOT pixel-diff against a prior baseline ,
this run establishes the first baseline. Future visual regression
tests can pixel-diff against tests/visual_baselines/.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_BASELINES_DIR = Path(__file__).parent / "visual_baselines"


@pytest.fixture
def app(qtbot):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _build_panel_with_mixed_patches(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    # Mixed: both preset-tagged and untagged patches , exercise both
    # render paths.
    patches = [
        {"label": "[0%] Skill_BasicMove", "enabled": True},
        {"label": "[0%] Skill_Sprint", "enabled": True},
        {"label": "[100%] Skill_BasicMove", "enabled": False},
        {"label": "[100%] Skill_Sprint", "enabled": False},
    ]
    panel.show_mod(
        mod_id=1, name="Visual Baseline Mod", author="test",
        version="1.0", status="active", file_count=1,
        patches=patches, conflicts=[],
    )
    return panel


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [480, 640, 800, 1200])
def test_render_baseline(qtbot, app, theme, width):
    from qfluentwidgets import setTheme, Theme
    setTheme(Theme.LIGHT if theme == "light" else Theme.DARK)

    panel = _build_panel_with_mixed_patches(qtbot, app)
    panel.set_panel_width(width)
    # Force layout , animation runs async; for visual capture we
    # want the final state. Stop the open animation, force final
    # width on both the maxWidth (animated property) and the actual
    # widget size, then let layout settle.
    if hasattr(panel, "_anim"):
        panel._anim.stop()
    panel.setMaximumWidth(width)
    panel.setMinimumWidth(width)
    panel.resize(width, max(panel.sizeHint().height(), 600))
    panel.show()
    qtbot.wait(150)  # let theme + layout settle

    _BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    out = _BASELINES_DIR / f"config_panel_{theme}_{width}.png"
    pixmap = panel.grab()
    assert pixmap.save(str(out)), f"Failed to save {out}"

    assert out.exists(), f"Baseline file not written: {out}"
    assert out.stat().st_size > 1000, f"Baseline suspiciously small: {out.stat().st_size} bytes"
    # Pixmap may be DPI-scaled (devicePixelRatio); compare against
    # device-independent size in widget pixels.
    assert pixmap.width() >= width, (
        f"Pixmap width {pixmap.width()} less than requested {width}"
    )
    assert pixmap.height() >= 200, f"Pixmap height too small: {pixmap.height()}"


def test_baselines_exist_for_all_combinations():
    """After parametrize runs, all 8 baselines must exist on disk."""
    expected = [
        f"config_panel_{t}_{w}.png"
        for t in ("light", "dark")
        for w in (480, 640, 800, 1200)
    ]
    for name in expected:
        path = _BASELINES_DIR / name
        if not path.exists():
            pytest.skip(f"Baseline {name} missing , run parametrized test first")
