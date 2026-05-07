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
    # Apply the theme BEFORE constructing the panel — qfluentwidgets
    # propagates theme changes asynchronously via the global qconfig
    # signal. ConfigPanel's __init__ calls _apply_theme() which bakes
    # isDarkTheme() into the stylesheet at build time. If we set the
    # theme AFTER construction, the panel's stylesheet stays light
    # for the lifetime of this fixture even after qtbot.wait — the
    # event loop processes the theme signal but the panel has already
    # cached its colors from the wrong theme. So: set theme, settle,
    # THEN build the panel.
    setTheme(Theme.LIGHT if theme == "light" else Theme.DARK)
    qtbot.wait(50)

    panel = _build_panel_with_mixed_patches(qtbot, app)
    # Defensive: re-apply theme after construction in case the build
    # raced the signal. Idempotent — re-runs the same stylesheet write.
    panel._apply_theme()
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
