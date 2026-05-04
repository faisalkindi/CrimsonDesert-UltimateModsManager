"""Behavior change requested by Faisal 2026-05-04: when a mod has
``last_apply_skipped_count > 0`` the green Active / Installed
StatusBadge must be hidden. The yellow SKIPPED badge owns the
status surface in that state, showing both is misleading because
the mod isn't fully active."""
from __future__ import annotations

import pytest

pytest_qt = pytest.importorskip("pytestqt")


def _make_card(qtbot, **overrides):
    from cdumm.gui.components.mod_card import ModCard
    kwargs = dict(
        mod_id=1, order=1, name="Test Mod", author="",
        version="1.0", status="Installed", file_count=1,
        has_config=False, has_notes=False, is_new=False,
        enabled=True,
    )
    kwargs.update(overrides)
    card = ModCard(**kwargs)
    qtbot.addWidget(card)
    return card


def test_status_badge_visible_when_no_skips(qtbot):
    """Regression guard: zero-skip cards keep the Active pill."""
    card = _make_card(qtbot, last_apply_skipped_count=0)
    assert card._status_badge.isVisible() or not card.isVisible(), (
        "StatusBadge must remain visible on cards with no skips."
    )
    # The widget itself must NOT be explicitly hidden , Qt's
    # isVisible() returns False on widgets whose parent isn't shown
    # yet (the test card hasn't been show()'d), so we check
    # isVisibleTo or the explicit hidden flag instead.
    assert not card._status_badge.isHidden()


def test_status_badge_hidden_when_skip_count_positive(qtbot):
    """Skip count > 0 must hide StatusBadge so the yellow SKIPPED
    pill is the only status surface."""
    card = _make_card(qtbot, last_apply_skipped_count=2,
                      last_apply_skip_summary="[]")
    assert card._status_badge.isHidden(), (
        "StatusBadge must be hidden when last_apply_skipped_count > 0. "
        "Yellow SKIPPED owns the status column in that state. "
        "Pre-fix the green Active pill renders alongside SKIPPED, "
        "which Faisal flagged as misleading 2026-05-04."
    )
