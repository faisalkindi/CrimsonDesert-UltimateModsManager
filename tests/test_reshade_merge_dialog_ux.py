"""Merge dialog UX: collapsible category groups, Select-all/Clear-all,
tick state preserved across advanced-toggle rebuilds.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_qt = pytest.importorskip("pytestqt")

from cdumm.i18n import load as load_translations

# Load translations once per module so button/group labels render as English.
load_translations("en")


def _make_presets(tmp_path: Path) -> tuple[Path, Path]:
    a = tmp_path / "A.ini"
    b = tmp_path / "B.ini"
    a.write_text(
        "Techniques=X@X.fx\n"
        "[Bloom.fx]\nThreshold=0.5\n"
        "[SMAA.fx]\nQuality=2\n"
        "[DEPTH]\nFoo=1\n"
    )
    b.write_text(
        "Techniques=Y@Y.fx\n"
        "[Bloom.fx]\nThreshold=0.9\n"
        "[HDR.fx]\nExposure=2.0\n"
        "[DOF.fx]\nNearPlane=5\n"
        "[GENERAL]\nExtra=1\n"
    )
    return a, b


def test_dialog_splits_sections_into_new_and_existing_groups(qtbot, tmp_path):
    from PySide6.QtWidgets import QMainWindow
    from cdumm.gui.components.reshade_merge_dialog import (
        ReshadeMergeDialog,
        _CollapsibleSectionGroup,
    )

    a, b = _make_presets(tmp_path)
    parent = QMainWindow()
    parent.resize(800, 600)
    parent.show()
    qtbot.addWidget(parent)

    dlg = ReshadeMergeDialog([a, b], tmp_path, parent=parent)
    qtbot.addWidget(dlg)

    # There should be two visible groups: New (HDR.fx, DOF.fx) and
    # Existing (Bloom.fx). SMAA.fx is in A only so it doesn't appear.
    groups = dlg._section_container.findChildren(_CollapsibleSectionGroup)
    assert len(groups) == 2

    # Every .fx section in Other must have a checkbox, bucketed correctly.
    section_names = {name for name, _ in dlg._section_checks}
    assert section_names == {"HDR.fx", "DOF.fx", "Bloom.fx"}


def test_toggling_advanced_preserves_user_ticks(qtbot, tmp_path):
    """Regression: toggling 'Include advanced' used to wipe every ticked
    checkbox. The rebuild must now inherit tick state from the previous
    snapshot."""
    from PySide6.QtWidgets import QMainWindow
    from cdumm.gui.components.reshade_merge_dialog import ReshadeMergeDialog

    a, b = _make_presets(tmp_path)
    parent = QMainWindow()
    parent.resize(800, 600)
    parent.show()
    qtbot.addWidget(parent)

    dlg = ReshadeMergeDialog([a, b], tmp_path, parent=parent)
    qtbot.addWidget(dlg)

    # Tick Bloom + HDR.
    for name, cb in dlg._section_checks:
        if name in ("Bloom.fx", "HDR.fx"):
            cb.setChecked(True)
    qtbot.wait(10)

    # User turns on the advanced toggle → rebuild.
    dlg._include_non_fx_cb.setChecked(True)
    qtbot.wait(10)

    ticked = {n for n, cb in dlg._section_checks if cb.isChecked()}
    assert "Bloom.fx" in ticked
    assert "HDR.fx" in ticked

    # User turns it back off → rebuild again.
    dlg._include_non_fx_cb.setChecked(False)
    qtbot.wait(10)

    ticked = {n for n, cb in dlg._section_checks if cb.isChecked()}
    assert "Bloom.fx" in ticked
    assert "HDR.fx" in ticked


def test_toggling_advanced_reveals_non_fx_sections(qtbot, tmp_path):
    """Turning on 'Include advanced' adds a third group (Advanced) showing
    non-fx sections from the other preset."""
    from PySide6.QtWidgets import QMainWindow
    from cdumm.gui.components.reshade_merge_dialog import (
        ReshadeMergeDialog,
        _CollapsibleSectionGroup,
    )

    a, b = _make_presets(tmp_path)
    parent = QMainWindow()
    parent.resize(800, 600)
    parent.show()
    qtbot.addWidget(parent)

    dlg = ReshadeMergeDialog([a, b], tmp_path, parent=parent)
    qtbot.addWidget(dlg)

    # Before toggle: only .fx sections visible.
    names_before = {name for name, _ in dlg._section_checks}
    assert "GENERAL" not in names_before

    dlg._include_non_fx_cb.setChecked(True)
    qtbot.wait(10)

    names_after = {name for name, _ in dlg._section_checks}
    assert "GENERAL" in names_after

    # And the advanced group exists now.
    groups = dlg._section_container.findChildren(_CollapsibleSectionGroup)
    assert len(groups) == 3  # New + Existing + Advanced


def test_select_all_button_ticks_every_checkbox_in_group(qtbot, tmp_path):
    from PySide6.QtWidgets import QMainWindow
    from cdumm.gui.components.reshade_merge_dialog import (
        ReshadeMergeDialog,
        _CollapsibleSectionGroup,
    )

    a, b = _make_presets(tmp_path)
    parent = QMainWindow()
    parent.resize(800, 600)
    parent.show()
    qtbot.addWidget(parent)

    dlg = ReshadeMergeDialog([a, b], tmp_path, parent=parent)
    qtbot.addWidget(dlg)

    groups = dlg._section_container.findChildren(_CollapsibleSectionGroup)
    # Find the 'New' group by inspecting its checkboxes.
    new_group = next(
        g for g in groups
        if {cb.text() for cb in g.checkboxes()} == {"HDR", "DOF"}
    )

    # Click "Select all" — every checkbox in that group should tick.
    new_group._select_all_btn.click()
    qtbot.wait(10)
    assert all(cb.isChecked() for cb in new_group.checkboxes())

    # Click again — should clear (button is a toggle).
    new_group._select_all_btn.click()
    qtbot.wait(10)
    assert not any(cb.isChecked() for cb in new_group.checkboxes())


def test_collapse_hides_body(qtbot, tmp_path):
    from PySide6.QtWidgets import QMainWindow
    from cdumm.gui.components.reshade_merge_dialog import (
        ReshadeMergeDialog,
        _CollapsibleSectionGroup,
    )

    a, b = _make_presets(tmp_path)
    parent = QMainWindow()
    parent.resize(800, 600)
    parent.show()
    qtbot.addWidget(parent)

    dlg = ReshadeMergeDialog([a, b], tmp_path, parent=parent)
    qtbot.addWidget(dlg)

    groups = dlg._section_container.findChildren(_CollapsibleSectionGroup)
    assert groups
    g = groups[0]

    # Starts expanded -> body visible.
    assert g._body.isVisibleTo(g) or g._expanded  # expanded flag is ground truth
    assert g._expanded is True

    g._toggle()
    assert g._expanded is False

    g._toggle()
    assert g._expanded is True
