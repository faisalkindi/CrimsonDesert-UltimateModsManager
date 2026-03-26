import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from cdmm.storage.game_finder import find_game_directories, validate_game_directory

logger = logging.getLogger(__name__)


class SetupDialog(QDialog):
    """First-run dialog for selecting the Crimson Desert game directory."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Game Directory Setup")
        self.setMinimumWidth(500)
        self._selected_path: Path | None = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Select your Crimson Desert installation folder:"))

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("E:\\SteamLibrary\\steamapps\\common\\Crimson Desert")
        self._path_edit.textChanged.connect(self._on_path_changed)
        path_row.addWidget(self._path_edit)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        btn_row = QHBoxLayout()
        self._ok_btn = QPushButton("OK")
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        # Try auto-detection
        self._try_auto_detect()

    def _try_auto_detect(self) -> None:
        candidates = find_game_directories()
        if candidates:
            self._path_edit.setText(str(candidates[0]))
            self._status_label.setText(f"Auto-detected: {candidates[0]}")
            logger.info("Auto-detected game directory: %s", candidates[0])

    def _on_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Crimson Desert Folder")
        if folder:
            self._path_edit.setText(folder)

    def _on_path_changed(self, text: str) -> None:
        path = Path(text)
        if validate_game_directory(path):
            self._selected_path = path
            self._ok_btn.setEnabled(True)
            self._status_label.setText("Valid Crimson Desert installation found.")
            self._status_label.setStyleSheet("color: green;")
        else:
            self._selected_path = None
            self._ok_btn.setEnabled(False)
            if text:
                self._status_label.setText("bin64/CrimsonDesert.exe not found at this path.")
                self._status_label.setStyleSheet("color: red;")
            else:
                self._status_label.setText("")

    @property
    def game_directory(self) -> Path | None:
        return self._selected_path
