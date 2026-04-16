"""Forked QFluentWidgets SmoothScrollBar with asymmetric top padding.

The stock ScrollBar uses symmetric _padding for both top and bottom,
which can't be changed without breaking the scroll value mapping.
This fork adds _topPadding to push the handle down while keeping
the bottom padding at the stock value, so the scrollbar aligns
with the first mod card instead of the folder group header.
"""

from PySide6.QtCore import Qt, QEasingCurve, QPoint
from PySide6.QtWidgets import QApplication

from qfluentwidgets.components.widgets.scroll_bar import SmoothScrollBar


class CdummScrollBar(SmoothScrollBar):
    """SmoothScrollBar with configurable top offset."""

    def __init__(self, orient, parent, top_padding=14):
        self._topPadding = top_padding
        self._bottomPadding = 14
        super().__init__(orient, parent)
        # Override the stock symmetric padding with bottom-only
        self._padding = self._bottomPadding

    def _adjustHandlePos(self):
        total = max(self.maximum() - self.minimum(), 1)
        delta = int(self.value() / total * self._slideLength())

        if self.orientation() == Qt.Vertical:
            x = self.width() - self.handle.width() - 3
            self.handle.move(x, self._topPadding + delta)
        else:
            y = self.height() - self.handle.height() - 3
            self.handle.move(self._topPadding + delta, y)

    def _grooveLength(self):
        if self.orientation() == Qt.Vertical:
            return self.height() - self._topPadding - self._bottomPadding
        return self.width() - self._topPadding - self._bottomPadding

    def _isSlideResion(self, pos: QPoint):
        if self.orientation() == Qt.Vertical:
            return self._topPadding <= pos.y() <= self.height() - self._bottomPadding
        return self._topPadding <= pos.x() <= self.width() - self._bottomPadding

    def mousePressEvent(self, e):
        # Replicate parent logic but with asymmetric padding
        from PySide6.QtWidgets import QWidget
        QWidget.mousePressEvent(self, e)
        self._isPressed = True
        self._pressedPos = e.pos()

        if self.childAt(e.pos()) is self.handle or not self._isSlideResion(e.pos()):
            return

        if self.orientation() == Qt.Vertical:
            if e.pos().y() > self.handle.geometry().bottom():
                value = e.pos().y() - self.handle.height() - self._topPadding
            else:
                value = e.pos().y() - self._topPadding
        else:
            if e.pos().x() > self.handle.geometry().right():
                value = e.pos().x() - self.handle.width() - self._topPadding
            else:
                value = e.pos().x() - self._topPadding

        self.setValue(int(value / max(self._slideLength(), 1) * self.maximum()))
        self.sliderPressed.emit()
