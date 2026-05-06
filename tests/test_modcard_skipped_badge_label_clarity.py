"""M3: SKIPPED badge label currently reads '⚠ SKIPPED (2)' , the
naked '(2)' confused Faisal who asked 'what is this (2)?' on
2026-05-04. The number is the count of patches dropped from the most
recent Apply, but nothing in the surface tells you that.

Fix: include a plain-English unit so the badge reads, for example,
'⚠ SKIPPED (2 patches)'. The threshold for clarity is: a user who
has never read CDUMM's docs should be able to glance at the badge
and know what the number measures.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from cdumm.gui.components.mod_card import ModCard
from cdumm import i18n


def setup_module(_module):
    i18n.load("en")


def test_skipped_badge_text_includes_unit_word(qtbot):
    card = ModCard(
        mod_id=1, order=1, name="Test", author="x", version="1",
        status="active", file_count=1,
        last_apply_skipped_count=2,
        last_apply_skip_summary='[{"label":"a","reason":"b","file":"c"}]',
    )
    qtbot.addWidget(card)
    badge = card.findChild(QLabel, "skippedBadge")
    assert badge is not None
    text = badge.text().lower()
    # Acceptable: 'patches', 'patch', any explicit unit word. The
    # bare '(N)' fails this test.
    assert "patch" in text, (
        f"Badge text {badge.text()!r} doesn't carry a unit. The naked "
        f"'(N)' confused users. Surface what the number measures.")


def test_skipped_badge_text_singular_when_count_is_one(qtbot):
    """One patch should read 'patch', many should read 'patches'.
    Subtle but it's the kind of small polish that separates 'looks
    professional' from 'looks like a debug overlay'."""
    card = ModCard(
        mod_id=1, order=1, name="Test", author="x", version="1",
        status="active", file_count=1,
        last_apply_skipped_count=1,
        last_apply_skip_summary='[{"label":"a","reason":"b","file":"c"}]',
    )
    qtbot.addWidget(card)
    badge = card.findChild(QLabel, "skippedBadge")
    assert badge is not None
    text = badge.text().lower()
    # 'patch' is in 'patches' too, so check the exact tokenization.
    assert "1 patch" in text and "1 patches" not in text, (
        f"Singular count should read '1 patch', got {badge.text()!r}.")
