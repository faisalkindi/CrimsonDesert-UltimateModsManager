import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

def _drop_default():
    from cdumm.gui.theme import get_color
    return (
        f"border: 3px dashed {get_color('border')}; border-radius: 10px; "
        f"padding: 28px; color: {get_color('text_secondary')}; background: {get_color('bg_deep')}; "
        f"font-size: 16px; font-weight: 700;"
    )

def _drop_hover():
    from cdumm.gui.theme import get_color
    return (
        f"border: 3px dashed {get_color('accent')}; border-radius: 10px; "
        f"padding: 28px; color: {get_color('accent')}; background: {get_color('bg_dark')}; "
        f"font-size: 16px; font-weight: 700;"
    )


class ImportWidget(QWidget):
    """Drag-and-drop area for mod import."""

    file_dropped = Signal(Path)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setMaximumHeight(140)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        from cdumm.i18n import tr
        self._label = QLabel(tr("import.drop_hint"))
        self._label.setObjectName("dropLabel")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._label.setStyleSheet(_drop_hover())

    def dragLeaveEvent(self, event) -> None:
        self._label.setStyleSheet("")  # revert to QSS default

    def dropEvent(self, event) -> None:
        self._label.setStyleSheet("")  # revert to QSS default
        urls = event.mimeData().urls()
        for url in urls:
            path = Path(url.toLocalFile())
            logger.info("File dropped for import: %s", path)
            self.file_dropped.emit(path)
