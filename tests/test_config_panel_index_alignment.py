"""Mixed toggle + editable patches regression: when a JSON mod ships
patches in the order [toggle, editable, toggle, editable] (or any
mix where editables don't all sit at the end), the config panel
used to emit them grouped — toggles first, then editables — losing
the original patch index. mods_page then used `enumerate(patches)`
to assign keys to `custom_values`, which lined up only by accident.

In the worst case, an editable's value was stored under the index
of a TOGGLE, so apply-time looked up the editable's real index and
found nothing. The custom value silently fell back to the default.

Fix: include the original patch `index` in each emitted dict so the
consumer keys `custom_values` by it, not by enumerate position.
"""
from __future__ import annotations

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def test_on_apply_emits_original_patch_indices(qtbot):
    """When the panel has 4 patches [toggle, editable, toggle, editable],
    the emitted list must carry each item's ORIGINAL patch index, so
    mods_page can store custom_values["1"] for the first editable
    (not custom_values["2"] under the toggle's slot)."""
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)

    # Manually populate the panel's internal state mimicking what
    # show_mod() would build for a 4-patch mod with a mix of types.
    panel._mod_id = 99
    panel._labels = {0: "Toggle A", 1: "Multiplier", 2: "Toggle B", 3: "Bonus"}

    # Two toggles at indices 0 and 2
    from PySide6.QtWidgets import QCheckBox, QSpinBox
    cb0 = QCheckBox()
    cb0.setChecked(True)
    cb2 = QCheckBox()
    cb2.setChecked(False)
    panel._toggles = {0: cb0, 2: cb2}
    panel._initial_states = {0: True, 2: False}

    # Two editables at indices 1 and 3
    sb1 = QSpinBox()
    sb1.setRange(1, 100)
    sb1.setValue(7)
    sb3 = QSpinBox()
    sb3.setRange(1, 100)
    sb3.setValue(42)
    panel._value_inputs = {1: sb1, 3: sb3}
    panel._initial_values = {1: 5, 3: 10}

    panel._variant_mode = False
    panel._variant_widgets = {}
    panel._variants_meta = []

    captured: list = []
    panel.apply_clicked.connect(lambda mod_id, lst: captured.append((mod_id, lst)))
    panel._on_apply()

    assert captured, "apply_clicked never fired"
    mod_id, items = captured[0]
    assert mod_id == 99

    # Build a {index: payload} map and check each ORIGINAL slot.
    by_index = {p.get("index"): p for p in items}
    assert set(by_index.keys()) == {0, 1, 2, 3}, (
        f"Each emitted item must carry its original patch index. Got "
        f"keys: {sorted(k for k in by_index.keys() if k is not None)!r}"
    )
    assert by_index[0]["enabled"] is True
    assert by_index[1]["value"] == 7
    assert by_index[2]["enabled"] is False
    assert by_index[3]["value"] == 42
