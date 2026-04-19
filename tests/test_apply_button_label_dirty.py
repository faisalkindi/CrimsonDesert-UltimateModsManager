"""HIGH #15: Apply button must stay visible if variant labels are dirty.

_on_variant_changed previously only checked whether the variant radio
differs from initial. A user who edits labels (via Configure) and then
reverts the variant to its initial pick would see the Apply button
vanish, dropping their label edits on the floor.

Fix: Apply is visible if EITHER the variant changed OR labels dirty.
"""
from __future__ import annotations

from cdumm.gui.components.config_panel import _is_apply_visible


def test_apply_hidden_when_nothing_changed():
    assert not _is_apply_visible(
        variant_widgets={0: False, 1: True},
        variant_initial={0: False, 1: True},
        label_dirty=set(),
    )


def test_apply_shown_when_variant_changed():
    assert _is_apply_visible(
        variant_widgets={0: True, 1: False},  # user switched picks
        variant_initial={0: False, 1: True},
        label_dirty=set(),
    )


def test_apply_shown_when_labels_dirty_even_if_variant_reverted():
    """The bug: user edits labels on variantA, then reverts variant to
    initial. Labels are still dirty, Apply must remain visible."""
    assert _is_apply_visible(
        variant_widgets={0: False, 1: True},   # same as initial
        variant_initial={0: False, 1: True},
        label_dirty={"variantA.json"},
    )


def test_apply_shown_when_both_dirty():
    assert _is_apply_visible(
        variant_widgets={0: True, 1: False},
        variant_initial={0: False, 1: True},
        label_dirty={"variantA.json"},
    )
