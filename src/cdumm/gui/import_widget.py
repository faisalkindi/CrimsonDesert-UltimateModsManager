import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

DROP_DEFAULT = (
    "border: 3px dashed #2E3440; border-radius: 10px; "
    "padding: 28px; color: #788090; background: #090B0E; "
    "font-size: 16px; font-weight: 700;"
)

DROP_HOVER = (
    "border: 3px dashed #D4A43C; border-radius: 10px; "
    "padding: 28px; color: #D4A43C; background: #16140E; "
    "font-size: 16px; font-weight: 700;"
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
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(DROP_DEFAULT)
        layout.addWidget(self._label)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._label.setStyleSheet(DROP_HOVER)

    def dragLeaveEvent(self, event) -> None:
        self._label.setStyleSheet(DROP_DEFAULT)

    def dropEvent(self, event) -> None:
        self._label.setStyleSheet(DROP_DEFAULT)
        urls = event.mimeData().urls()
        for url in urls:
            path = Path(url.toLocalFile())
            logger.info("File dropped for import: %s", path)
            self.file_dropped.emit(path)
