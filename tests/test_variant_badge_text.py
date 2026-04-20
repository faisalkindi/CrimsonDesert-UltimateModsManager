"""D-Badge coverage: mutex variant mods get 'Active: <name>' badge
instead of 'N/M variants'.

Mutex-pack mods (GildsGear-style, 40+ alts with 'Category / Variant'
labels) would show 'Active: Abyss Gear 1' as the third badge.
Non-mutex mods keep the original '1/5 variants' shape.
"""
from __future__ import annotations

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def _find_badges(panel) -> list[str]:
    """Return the text of every badge (QLabel) in _badge_row layout."""
    from PySide6.QtWidgets import QLabel
    labels: list[str] = []
    layout = panel._badge_row
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is None:
            continue
        w = item.widget()
        if isinstance(w, QLabel):
            t = w.text()
            if t:
                labels.append(t)
    return labels


def test_mutex_pack_shows_active_loadout_badge(qtbot):
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)

    variants = [
        {"filename": "a1.json", "label": "Abyss Gears / Abyss Gear 1",
         "enabled": True, "group": 0},
        {"filename": "a2.json", "label": "Abyss Gears / Abyss Gear 2",
         "enabled": False, "group": 0},
        {"filename": "w1.json", "label": "Weapons / Ammo 1",
         "enabled": False, "group": 0},
        {"filename": "w2.json", "label": "Weapons / Ammo 2",
         "enabled": False, "group": 0},
    ]
    panel.show_variant_mod(
        mod_id=1, name="Gilds Gear", author="GildyBoye", version="1.0",
        status="Deactivated", variants=variants)

    all_text = " | ".join(_find_badges(panel))
    # Mutex-pack mode: the variant count badge should read as an
    # 'Active:' text, not the generic 'N/M variants' form.
    assert "Active:" in all_text, (
        f"mutex-pack mode should show an 'Active:' badge; got {all_text}")
    assert "Abyss Gear 1" in all_text, (
        f"Active badge should reflect the enabled variant; got {all_text}")


def test_plain_variant_mod_shows_count_badge(qtbot):
    """Normal variant mods (non-mutex-pack) keep 'N/M variants'."""
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)

    variants = [
        {"filename": "ex.json", "label": "Extra Shadows",
         "enabled": True, "group": 0},
        {"filename": "no.json", "label": "No Extra Shadows",
         "enabled": False, "group": 0},
    ]
    panel.show_variant_mod(
        mod_id=1, name="Vaxis LoD", author="Vaxis", version="1.0",
        status="Deactivated", variants=variants)

    all_text = " | ".join(_find_badges(panel))
    assert "variants" in all_text.lower(), (
        f"plain variant mod should show 'N/M variants' badge; got {all_text}")
    assert "Active:" not in all_text, (
        f"plain variant mod shouldn't use mutex-pack badge; got {all_text}")
