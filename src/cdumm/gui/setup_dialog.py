import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    setCustomStyleSheet,
)

from cdumm.storage.game_finder import find_game_directories, validate_game_directory

logger = logging.getLogger(__name__)


class SetupDialog(MessageBoxBase):
    """First-run dialog for selecting the Crimson Desert game directory."""

    def __init__(self, parent=None) -> None:
        # MessageBoxBase requires a parent; create a temporary invisible
        # widget when called from main.py before any window exists.
        self._temp_parent = None
        if parent is None:
            self._temp_parent = QWidget()
            parent = self._temp_parent
        super().__init__(parent)

        self._selected_path: Path | None = None

        self.titleLabel = SubtitleLabel("Game Directory Setup")
        self.viewLayout.addWidget(self.titleLabel)

        self.viewLayout.addWidget(
            BodyLabel("Select your Crimson Desert installation folder:"))

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Steam or Xbox Game Pass install folder")
        self._path_edit.textChanged.connect(self._on_path_changed)
        path_row.addWidget(self._path_edit)

        browse_btn = PushButton("Browse...")
        browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(browse_btn)
        self.viewLayout.addLayout(path_row)

        self._status_label = CaptionLabel("")
        self.viewLayout.addWidget(self._status_label)

        # Configure default buttons
        self.yesButton.setText("OK")
        self.yesButton.setEnabled(False)
        self.cancelButton.setText("Cancel")

        self.widget.setMinimumWidth(500)

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
            self.yesButton.setEnabled(True)
            self._status_label.setText("Valid Crimson Desert installation found.")
            setCustomStyleSheet(
                self._status_label,
                "CaptionLabel { color: #16a34a; }",
                "CaptionLabel { color: #4ade80; }",
            )
        else:
            self._selected_path = None
            self.yesButton.setEnabled(False)
            if text:
                self._status_label.setText("bin64/CrimsonDesert.exe not found at this path.")
                setCustomStyleSheet(
                    self._status_label,
                    "CaptionLabel { color: #dc2626; }",
                    "CaptionLabel { color: #f87171; }",
                )
            else:
                self._status_label.setText("")

    @property
    def game_directory(self) -> Path | None:
        return self._selected_path
