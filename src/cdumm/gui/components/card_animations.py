"""Reusable card entrance animations for CDUMM v3."""

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QSequentialAnimationGroup,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget


def staggered_fade_in(
    widgets: list[QWidget],
    duration: int = 250,
    stagger: int = 30,
    max_animated: int = 20,
) -> QSequentialAnimationGroup | None:
    """Animate widgets with a staggered fade-in.

    Only the first *max_animated* widgets are animated; the rest appear
    instantly.  The returned group must be kept alive by the caller.
    """
    if not widgets:
        return None

    group = QSequentialAnimationGroup()

    for i, widget in enumerate(widgets):
        if i >= max_animated:
            widget.setGraphicsEffect(None)
            break

        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)

        parallel = QParallelAnimationGroup()

        fade = QPropertyAnimation(effect, b"opacity")
        fade.setDuration(duration)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        parallel.addAnimation(fade)

        group.addPause(stagger if i > 0 else 0)
        group.addAnimation(parallel)

    # Clean up effects when done so they don't eat GPU
    animated = widgets[:max_animated]

    def _cleanup():
        for w in animated:
            try:
                w.setGraphicsEffect(None)
            except RuntimeError:
                pass  # widget already deleted

    group.finished.connect(_cleanup)
    group.start()
    return group
