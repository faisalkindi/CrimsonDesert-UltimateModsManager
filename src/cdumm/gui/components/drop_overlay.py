"""Full-area translucent overlay shown when a file is dragged over the window."""

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsOpacityEffect, QVBoxLayout, QWidget

from qfluentwidgets import CaptionLabel, SubtitleLabel, isDarkTheme

from cdumm.i18n import tr


class DropOverlay(QWidget):
    """Translucent drop overlay. Invisible by default.

    The parent widget calls ``show_overlay`` / ``hide_overlay`` from its
    own dragEnterEvent / dragLeaveEvent / dropEvent.  The overlay itself
    just paints and stays out of the way.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setVisible(False)

        # Opacity for fade animation
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)

        self._fade_anim = QPropertyAnimation(self._opacity, b"opacity")
        self._fade_anim.setDuration(200)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # ── Center content ────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Large icon
        self._icon_label = SubtitleLabel(tr("drop.title"))
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setStyleSheet("font-size: 48px; background: transparent;")
        layout.addWidget(self._icon_label, 0, Qt.AlignmentFlag.AlignCenter)

        # Primary text
        self._title = SubtitleLabel(tr("drop.subtitle"))
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title, 0, Qt.AlignmentFlag.AlignCenter)

        # Hint text
        self._hint = CaptionLabel(tr("drop.formats"))
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._hint, 0, Qt.AlignmentFlag.AlignCenter)

        self._apply_theme()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _apply_theme(self) -> None:
        dark = isDarkTheme()
        title_color = "#5CB8F0" if dark else "#2878D0"
        hint_color = "#6B7585" if dark else "#8B95A5"
        self._title.setStyleSheet(
            f"color: {title_color}; background: transparent;"
        )
        self._hint.setStyleSheet(
            f"color: {hint_color}; background: transparent;"
        )

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_theme()
            self.update()  # repaint overlay tint

    # ------------------------------------------------------------------
    # Paint — translucent background + dashed border
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = isDarkTheme()

        # Translucent fill — darker tint for dark mode
        if dark:
            painter.fillRect(self.rect(), QColor(40, 80, 160, 25))
        else:
            painter.fillRect(self.rect(), QColor(40, 120, 208, 15))

        # Dashed border
        border_color = "#5CB8F0" if dark else "#2878D0"
        pen = QPen(QColor(border_color), 3, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

        painter.end()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_overlay(self) -> None:
        """Fade the overlay in and raise it above siblings."""
        self.setVisible(True)
        self.raise_()
        self._fade_anim.stop()
        try:
            self._fade_anim.finished.disconnect()
        except RuntimeError:
            pass
        self._fade_anim.setStartValue(self._opacity.opacity())
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def hide_overlay(self) -> None:
        """Fade the overlay out."""
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity.opacity())
        self._fade_anim.setEndValue(0.0)

        def _on_done():
            if self._opacity.opacity() < 0.05:
                self.setVisible(False)

        try:
            self._fade_anim.finished.disconnect()
        except RuntimeError:
            pass
        self._fade_anim.finished.connect(_on_done)
        self._fade_anim.start()
