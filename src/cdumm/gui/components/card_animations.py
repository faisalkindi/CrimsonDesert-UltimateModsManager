"""Reusable card entrance animations for CDUMM v3."""

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget


def staggered_fade_in(
    widgets: list[QWidget],
    duration: int = 250,
    stagger: int = 0,
    max_animated: int = 0,
) -> QParallelAnimationGroup | None:
    """Fade all cards in together as one quick parallel animation.

    Every card starts at opacity 0 and fades to 1 simultaneously.
    A subtle wave is created by giving each card a tiny extra delay
    (capped so the total never exceeds ~400ms regardless of card count).

    The returned group must be kept alive by the caller.
    """
    if not widgets:
        return None

    group = QParallelAnimationGroup()
    effects = []

    # Cap per-card delay so total animation stays short even with many mods
    count = len(widgets)
    per_card_extra = min(4, 150 // max(count, 1))

    for i, widget in enumerate(widgets):
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        effects.append((widget, effect))

        fade = QPropertyAnimation(effect, b"opacity")
        fade.setDuration(duration + i * per_card_extra)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(fade)

    # Clean up effects when done so they don't interfere with drag pixmaps
    def _cleanup():
        for w, _ in effects:
            try:
                w.setGraphicsEffect(None)
            except RuntimeError:
                pass  # widget already deleted

    group.finished.connect(_cleanup)
    group.start()
    return group
