import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
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

from cdumm.i18n import tr
from cdumm.storage.game_finder import (
    find_game_directories,
    resolve_game_directory,
    validate_game_directory,
)

logger = logging.getLogger(__name__)


class SetupDialog(MessageBoxBase):
    """First-run dialog for selecting the Crimson Desert game directory."""

    def __init__(self, parent=None) -> None:
        # MaskDialogBase (the qfluentwidgets parent of MessageBoxBase)
        # calls ``self.setGeometry(0, 0, parent.width(), parent.height())``
        # in __init__. When SetupDialog is constructed before any
        # real window exists (the main.py recovery flow when a saved
        # game_dir doesn't validate on relaunch), the previous
        # "invisible QWidget temp parent" gave width=0/height=0 on
        # macOS — the dialog rendered with zero geometry and macOS's
        # window server refused to raise it. CptUndies (Nexus
        # 2026-05-04, mod 2253) reported the symptom as a silent
        # hang: process alive, modal exec waiting, no GUI visible.
        #
        # Fix: build the temp parent as a transparent, frameless,
        # on-top widget sized to the primary screen, and SHOW it
        # before super().__init__ so MaskDialogBase reads real
        # width/height. The translucent mask layer that
        # MaskDialogBase paints over the parent already covers this
        # widget, so the user only sees the dialog itself — the
        # transparent backing is invisible.
        self._temp_parent: QWidget | None = None
        if parent is None:
            screen = QApplication.primaryScreen().availableGeometry()
            self._temp_parent = QWidget()
            self._temp_parent.setAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground)
            self._temp_parent.setWindowFlags(
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
            )
            self._temp_parent.setGeometry(screen)
            self._temp_parent.show()
            parent = self._temp_parent
        super().__init__(parent)

        self._selected_path: Path | None = None

        self.titleLabel = SubtitleLabel(tr("setup.title"))
        self.viewLayout.addWidget(self.titleLabel)

        self.viewLayout.addWidget(
            BodyLabel(tr("setup.select")))

        from qfluentwidgets import isDarkTheme
        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(tr("setup.placeholder"))
        if isDarkTheme():
            self._path_edit.setStyleSheet(
                "QLineEdit { background: #1C2028; color: #E2E8F0; "
                "border: 1px solid #2D3340; border-radius: 6px; padding: 8px; }")
        else:
            self._path_edit.setStyleSheet(
                "QLineEdit { background: #FAFBFC; color: #1A202C; "
                "border: 1px solid #E2E8F0; border-radius: 6px; padding: 8px; }")
        self._path_edit.textChanged.connect(self._on_path_changed)
        path_row.addWidget(self._path_edit)

        browse_btn = PushButton(tr("setup.browse"))
        browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(browse_btn)
        self.viewLayout.addLayout(path_row)

        self._status_label = CaptionLabel("")
        self.viewLayout.addWidget(self._status_label)

        # Configure default buttons
        self.yesButton.setText(tr("setup.ok"))
        self.yesButton.setEnabled(False)
        self.cancelButton.setText(tr("main.cancel"))

        self.widget.setMinimumWidth(500)

        # Try auto-detection
        self._try_auto_detect()

    def _try_auto_detect(self) -> None:
        candidates = find_game_directories()
        if candidates:
            self._path_edit.setText(str(candidates[0]))
            self._status_label.setText(tr("setup.auto_detected", path=candidates[0]))
            logger.info("Auto-detected game directory: %s", candidates[0])

    def _on_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("setup.browse_dialog_title"))
        if folder:
            self._path_edit.setText(folder)

    def _on_path_changed(self, text: str) -> None:
        path = Path(text)
        if validate_game_directory(path):
            # On macOS the user normally picks ``Crimson Desert.app``
            # but the app operates on the inner packages/ directory.
            # ``resolve_game_directory`` walks in for us; on Windows /
            # Linux it returns the path unchanged.
            self._selected_path = resolve_game_directory(path) or path
            self.yesButton.setEnabled(True)
            self._status_label.setText(tr("setup.valid"))
            setCustomStyleSheet(
                self._status_label,
                "CaptionLabel { color: #16a34a; }",
                "CaptionLabel { color: #4ade80; }",
            )
        else:
            self._selected_path = None
            self.yesButton.setEnabled(False)
            if text:
                self._status_label.setText(tr("setup.invalid"))
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

    def done(self, result: int) -> None:  # noqa: D401 — Qt slot
        """Tear down the temp parent on close (accept OR reject).

        Qt's ``QDialog`` calls ``done()`` for both ``accept()`` and
        ``reject()``, so a single override covers cleanup regardless
        of how the user dismissed the dialog. Without this, the
        screen-sized transparent temp parent would linger as an
        invisible top-level widget after the modal returned and
        keep the Qt event loop holding refs.
        """
        super().done(result)
        if self._temp_parent is not None:
            self._temp_parent.hide()
            self._temp_parent.deleteLater()
            self._temp_parent = None
