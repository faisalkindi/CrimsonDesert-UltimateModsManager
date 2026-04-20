"""B-Bg coverage: FolderVariantDialog must render a non-black background.

Qt forum thread 154590 / 106970: widgets without explicit background
or autoFillBackground can render solid black on Windows + PySide6
under certain theme combos. FolderVariantDialog sets both defensively.
This test uses QWidget.grab() to verify the top-center pixel isn't
black after render.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def test_folder_variant_dialog_not_all_black(qtbot, tmp_path: Path):
    """Grab the dialog widget, sample several interior pixels; they
    must NOT all be (0,0,0). A black-rendered dialog would return
    solid zeros across every sample."""
    from PySide6.QtWidgets import QMainWindow
    from cdumm.gui.preset_picker import FolderVariantDialog

    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "mod.json").write_text('{"patches": []}')
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "mod.json").write_text('{"patches": []}')

    # MaskDialogBase (the fluentwidgets base) requires a visible
    # parent window to position its backdrop mask against.
    host = QMainWindow()
    host.resize(800, 600)
    host.show()
    qtbot.addWidget(host)
    qtbot.waitExposed(host)

    dlg = FolderVariantDialog(
        [tmp_path / "A", tmp_path / "B"], parent=host)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    dlg.adjustSize()
    qtbot.wait(50)   # settle paint

    pixmap = dlg.grab()
    img = pixmap.toImage()
    assert not img.isNull(), "grab() returned null image — dialog didn't render"
    w, h = img.width(), img.height()
    assert w > 0 and h > 0

    # Sample 5 interior points: center + 4 quadrants.
    sample_points = [
        (w // 2, h // 2),
        (w // 4, h // 4),
        (3 * w // 4, h // 4),
        (w // 4, 3 * h // 4),
        (3 * w // 4, 3 * h // 4),
    ]
    non_black_samples = 0
    for x, y in sample_points:
        pixel = img.pixel(x, y)
        # Qt QImage.pixel returns ARGB as uint32.
        r = (pixel >> 16) & 0xFF
        g = (pixel >> 8) & 0xFF
        b = pixel & 0xFF
        if (r, g, b) != (0, 0, 0):
            non_black_samples += 1
    assert non_black_samples >= 3, (
        f"At least 3 of 5 sample points should be non-black; only "
        f"{non_black_samples} were. Dialog is rendering mostly black.")

    dlg.close()
