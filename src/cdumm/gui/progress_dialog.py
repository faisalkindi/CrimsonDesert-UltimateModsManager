"""Proper progress dialog that reliably shows percentage and status text."""
import logging

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)


class ProgressDialog(QDialog):
    """Modal progress dialog with percentage bar and status message.

    Unlike QProgressDialog, this always shows immediately and reliably
    updates from worker thread signals via proper Slot decorators.
    """

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(450)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.WindowTitleHint | Qt.WindowType.CustomizeWindowHint
        )

        layout = QVBoxLayout(self)

        self._status_label = QLabel("Starting...")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p% complete")
        layout.addWidget(self._progress_bar)

        self._detail_label = QLabel("")
        self._detail_label.setStyleSheet("color: #666; font-size: 11px;")
        self._detail_label.setWordWrap(True)
        layout.addWidget(self._detail_label)

    @Slot(int, str)
    def update_progress(self, percent: int, message: str) -> None:
        """Thread-safe progress update via Qt signal/slot."""
        self._progress_bar.setValue(percent)
        self._status_label.setText(message)
        self._detail_label.setText(f"{percent}% complete")

    set_progress = update_progress

    @Slot()
    def on_finished(self) -> None:
        self._progress_bar.setValue(100)
        self._status_label.setText("Complete!")
        self.accept()

    @Slot(str)
    def on_error(self, error: str) -> None:
        self._status_label.setText(f"Error: {error}")
        self._status_label.setStyleSheet("color: red;")
        self._detail_label.setText("Operation failed")
        # Don't auto-close — let user read the error
