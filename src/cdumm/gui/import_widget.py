import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)


class ImportWidget(QWidget):
    """Drag-and-drop area for mod import."""

    file_dropped = Signal(Path)  # Emitted when a valid file/folder is dropped

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(80)

        layout = QVBoxLayout(self)
        self._label = QLabel("Drag and drop a mod here to import\n(zip, folder, .bat, .py, .bsdiff)")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "border: 2px dashed #999; border-radius: 8px; padding: 16px; color: #666;"
        )
        layout.addWidget(self._label)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._label.setStyleSheet(
                "border: 2px dashed #4CAF50; border-radius: 8px; padding: 16px; "
                "color: #4CAF50; background-color: #f0fff0;"
            )

    def dragLeaveEvent(self, event) -> None:
        self._label.setStyleSheet(
            "border: 2px dashed #999; border-radius: 8px; padding: 16px; color: #666;"
        )

    def dropEvent(self, event) -> None:
        self._label.setStyleSheet(
            "border: 2px dashed #999; border-radius: 8px; padding: 16px; color: #666;"
        )
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            logger.info("File dropped for import: %s", path)
            self.file_dropped.emit(path)
