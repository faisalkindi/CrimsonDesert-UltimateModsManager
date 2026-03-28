"""Fullscreen overlay for mod update drag-drop."""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget


class UpdateOverlay(QWidget):
    """Translucent overlay that covers the main window for drag-drop mod updates."""

    folder_dropped = Signal(Path)  # emitted when a valid folder is dropped
    cancelled = Signal()

    def __init__(self, mod_name: str, parent=None) -> None:
        super().__init__(parent)
        self._mod_name = mod_name
        self._hovering = False
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def show_overlay(self) -> None:
        """Resize to parent and show."""
        if self.parent():
            self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        self.setFocus()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # Dark translucent background
        painter.fillRect(self.rect(), QColor(0, 0, 0, 180))

        # Border when hovering
        if self._hovering:
            painter.setPen(QColor(76, 175, 80))
            border = self.rect().adjusted(20, 20, -20, -20)
            for i in range(3):
                painter.drawRect(border.adjusted(-i, -i, i, i))

        # Main text
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                         f"Drop updated files for\n\"{self._mod_name}\"")

        # Subtext
        painter.setPen(QColor(180, 180, 180))
        painter.setFont(QFont("Segoe UI", 11))
        painter.drawText(
            self.rect().adjusted(0, 80, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignCenter,
            "Drop a folder or zip here  —  Press Escape to cancel")

        painter.end()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            self.cancelled.emit()

    def mousePressEvent(self, event) -> None:
        # Click anywhere to cancel
        self.hide()
        self.cancelled.emit()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hovering = True
            self.update()

    def dragLeaveEvent(self, event) -> None:
        self._hovering = False
        self.update()

    def dropEvent(self, event) -> None:
        self._hovering = False
        self.update()
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            self.hide()
            self.folder_dropped.emit(path)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
