"""End-to-end test: real Nexus mod 1103 renders with a working
preset selector after Phase 1 lands."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


_MOD_1103_PATH = Path(
    "C:/Users/faisa/Downloads/Compressed/"
    "JSON Stamina - Spirit Adjuster And Regen-1103-1-4-1777707454/"
    "Stamina Spirit Adjuster + Regen.json"
)


@pytest.fixture
def app(qtbot):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def mod_1103_patches():
    if not _MOD_1103_PATH.exists():
        pytest.skip("mod 1103 fixture not on disk")
    with _MOD_1103_PATH.open(encoding="utf-8") as f:
        d = json.load(f)
    # ConfigPanel.show_mod takes one combined patches list. Mod 1103
    # ships 2 game_files (skill + buffinfo), each with its own changes
    # array. Real production code passes the flattened list.
    flat = []
    for patch in d["patches"]:
        for c in patch["changes"]:
            # Add an "enabled": True default so the panel renders
            # checkboxes (the importer normally sets this).
            c.setdefault("enabled", True)
            flat.append(c)
    return flat


def test_mod_1103_renders_11_preset_radios(qtbot, app, mod_1103_patches):
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.show_mod(
        mod_id=1103, name="Stamina/Spirit Adjuster", author="0xNobody",
        version="1.4", status="active",
        file_count=2, patches=mod_1103_patches, conflicts=[],
    )
    assert panel._preset_radio_group is not None, (
        "Expected preset selector to be detected for mod 1103, "
        "but ConfigPanel didn't build it."
    )
    # 11 preset tags + Custom = 12 radios.
    radios = panel._preset_radio_group.buttons()
    assert len(radios) == 12, f"Expected 12 radios, got {len(radios)}"
    tags = sorted(b.text() for b in radios)
    expected = sorted([
        "0%", "1%", "2%", "3%", "4%", "5%",
        "10%", "25%", "50%", "75%", "100%",
        "Custom",
    ])
    assert tags == expected


def test_mod_1103_clicking_10_percent_enables_only_10_percent_patches(
    qtbot, app, mod_1103_patches,
):
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.show_mod(
        mod_id=1103, name="t", author="x", version="1",
        status="active", file_count=2, patches=mod_1103_patches, conflicts=[],
    )

    ten_radio = next(
        b for b in panel._preset_radio_group.buttons() if b.text() == "10%"
    )
    ten_radio.click()

    # Per detection, [10%] tag has 531 indices.
    expected_enabled_indices = set(panel._preset_groups["10%"])
    assert len(expected_enabled_indices) == 531

    enabled_count = 0
    for idx, toggle in panel._toggles.items():
        if toggle.isChecked():
            enabled_count += 1
            assert idx in expected_enabled_indices, (
                f"toggle {idx} is checked but not in [10%] preset group"
            )
    assert enabled_count == 531, (
        f"Expected exactly 531 toggles checked, got {enabled_count}"
    )


def test_mod_1103_switching_presets_flips_groups(
    qtbot, app, mod_1103_patches,
):
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.show_mod(
        mod_id=1103, name="t", author="x", version="1",
        status="active", file_count=2, patches=mod_1103_patches, conflicts=[],
    )

    fifty = next(b for b in panel._preset_radio_group.buttons() if b.text() == "50%")
    one_hundred = next(b for b in panel._preset_radio_group.buttons() if b.text() == "100%")

    fifty.click()
    one_hundred.click()

    expected_enabled_indices = set(panel._preset_groups["100%"])
    fifty_indices = set(panel._preset_groups["50%"])

    for idx, toggle in panel._toggles.items():
        if idx in fifty_indices:
            assert toggle.isChecked() is False, f"50% toggle {idx} should be off"
        if idx in expected_enabled_indices:
            assert toggle.isChecked() is True, f"100% toggle {idx} should be on"
