"""Bug Report page — dedicated sidebar page for generating a diagnostic report.

Renders the same auto-collected report that BugReportDialog shows (system info,
mod state, recent logs) but as a full page the user can return to any time via
the sidebar. The user picks a severity, types a description, then clicks Copy or
Save and pastes the result into their NexusMods / GitHub bug thread.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    TextEdit,
)

from cdumm.i18n import tr

logger = logging.getLogger(__name__)


class _PrivateBinUploadWorker(QThread):
    """Upload the encrypted report on a background thread."""

    finished_ok = Signal(str)      # paste URL
    failed = Signal(str)           # error message

    def __init__(self, server: str, text: str, expire_code: str, parent=None) -> None:
        super().__init__(parent)
        self._server = server
        self._text = text
        self._expire = expire_code

    def run(self) -> None:
        try:
            from privatebin import Expiration, PrivateBin
            exp_map = {
                "5min": Expiration.FIVE_MIN,
                "10min": Expiration.TEN_MIN,
                "1hour": Expiration.ONE_HOUR,
                "1day": Expiration.ONE_DAY,
                "1week": Expiration.ONE_WEEK,
                "1month": Expiration.ONE_MONTH,
                "1year": Expiration.ONE_YEAR,
            }
            expiration = exp_map.get(self._expire, Expiration.ONE_WEEK)
            with PrivateBin(self._server) as client:
                receipt = client.create(self._text, expiration=expiration)
            # str(url) masks the passphrase with '********'. unmask() returns the
            # real decryption URL the user needs to share.
            self.finished_ok.emit(receipt.url.unmask())
        except Exception as e:
            logger.warning("PrivateBin upload failed: %s", e)
            self.failed.emit(str(e))


class BugReportPage(SmoothScrollArea):
    """Full-page version of BugReportDialog."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("BugReportPage")
        self.setWidgetResizable(True)
        self.setFrameShape(SmoothScrollArea.Shape.NoFrame)

        self._db = None
        self._game_dir: Path | None = None
        self._app_data_dir: Path | None = None

        container = QWidget()
        self.setWidget(container)
        # Without this, the scroll viewport keeps Qt's default light-grey
        # background even in dark mode, clashing with the themed CardWidgets
        # and labels inside. MUST be called after setWidget().
        self.enableTransparentBackground()

        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(24, 20, 24, 20)
        self._layout.setSpacing(10)

        # --- Header ---
        self._title = SubtitleLabel(tr("bug_page.title"), container)
        tf = self._title.font()
        tf.setPixelSize(24)
        self._title.setFont(tf)
        self._layout.addWidget(self._title)

        self._subtitle = CaptionLabel(tr("bug_page.subtitle"), container)
        self._subtitle.setWordWrap(True)
        self._layout.addWidget(self._subtitle)
        self._layout.addSpacing(8)

        # --- Severity + Description card ---
        desc_card = CardWidget(container)
        dc_layout = QVBoxLayout(desc_card)
        dc_layout.setContentsMargins(18, 14, 18, 14)
        dc_layout.setSpacing(8)

        sev_row = QHBoxLayout()
        sev_row.addWidget(StrongBodyLabel(tr("bug.severity"), desc_card))
        self._severity = ComboBox(desc_card)
        self._severity.addItems([
            tr("bug.crash"), tr("bug.wrong"),
            tr("bug.visual"), tr("bug.other"),
        ])
        self._severity.setFixedWidth(260)
        self._severity.setCurrentIndex(1)  # 'Wrong behavior' is the most common
        sev_row.addWidget(self._severity)
        sev_row.addStretch()
        dc_layout.addLayout(sev_row)

        self._what_label = CaptionLabel(tr("bug.what_happened"), desc_card)
        dc_layout.addWidget(self._what_label)

        self._desc_edit = TextEdit(desc_card)
        self._desc_edit.setPlaceholderText(tr("bug.placeholder"))
        self._desc_edit.setMinimumHeight(90)
        self._desc_edit.setMaximumHeight(140)
        dc_layout.addWidget(self._desc_edit)

        self._layout.addWidget(desc_card)

        # --- Action buttons row ---
        action_row = QHBoxLayout()
        action_row.setSpacing(10)

        self._regen_btn = PushButton(FluentIcon.SYNC, tr("bug_page.regenerate"), container)
        self._regen_btn.clicked.connect(self._regenerate)
        action_row.addWidget(self._regen_btn)

        action_row.addStretch()

        self._upload_btn = PrimaryPushButton(FluentIcon.SHARE, tr("bug.upload"), container)
        self._upload_btn.clicked.connect(self._upload_report)
        action_row.addWidget(self._upload_btn)

        self._copy_btn = PushButton(FluentIcon.COPY, tr("bug.copy"), container)
        self._copy_btn.clicked.connect(self._copy_report)
        action_row.addWidget(self._copy_btn)

        self._save_btn = PushButton(FluentIcon.SAVE, tr("bug.save"), container)
        self._save_btn.clicked.connect(self._save_report)
        action_row.addWidget(self._save_btn)

        self._layout.addLayout(action_row)

        self._upload_worker: _PrivateBinUploadWorker | None = None

        # --- Report preview card ---
        preview_card = CardWidget(container)
        pc_layout = QVBoxLayout(preview_card)
        pc_layout.setContentsMargins(18, 14, 18, 14)
        pc_layout.setSpacing(6)

        self._preview_label = StrongBodyLabel(tr("bug.preview"), preview_card)
        pc_layout.addWidget(self._preview_label)

        self._preview = PlainTextEdit(preview_card)
        self._preview.setReadOnly(True)
        _mono = QFont("Consolas")
        _mono.setStyleHint(QFont.StyleHint.Monospace)
        self._preview.setFont(_mono)
        self._preview.setMinimumHeight(320)
        pc_layout.addWidget(self._preview)

        self._layout.addWidget(preview_card, stretch=1)

        # Live-update preview when user edits severity or description
        self._severity.currentTextChanged.connect(lambda _: self._refresh_preview())
        self._desc_edit.textChanged.connect(self._refresh_preview)

        self._base_report = ""

    # ------------------------------------------------------------------
    # Managers / API
    # ------------------------------------------------------------------

    def set_managers(self, db=None, game_dir: Path | None = None,
                     app_data_dir: Path | None = None, **kwargs) -> None:
        self._db = db
        self._game_dir = game_dir
        # Prefer explicit param, fall back to window attribute if omitted
        if app_data_dir is None:
            w = self.window()
            app_data_dir = getattr(w, "_app_data_dir", None)
        self._app_data_dir = app_data_dir
        self._regenerate()

    def refresh(self) -> None:
        self._regenerate()

    def retranslate_ui(self) -> None:
        self._title.setText(tr("bug_page.title"))
        self._subtitle.setText(tr("bug_page.subtitle"))
        self._what_label.setText(tr("bug.what_happened"))
        self._desc_edit.setPlaceholderText(tr("bug.placeholder"))
        self._regen_btn.setText(tr("bug_page.regenerate"))
        self._upload_btn.setText(tr("bug.upload"))
        self._copy_btn.setText(tr("bug.copy"))
        self._save_btn.setText(tr("bug.save"))
        self._preview_label.setText(tr("bug.preview"))

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    def _regenerate(self) -> None:
        try:
            from cdumm.gui.bug_report import generate_bug_report
            self._base_report = generate_bug_report(
                self._db, self._game_dir, self._app_data_dir)
        except Exception as e:
            logger.warning("generate_bug_report failed: %s", e)
            self._base_report = (
                "(Failed to generate report — see cdumm.log for details.)\n"
                f"Error: {e}\n"
            )
        self._refresh_preview()

    def _compose_report(self) -> str:
        sev = self._severity.currentText()
        desc = self._desc_edit.toPlainText().strip()
        header = f"--- SEVERITY: {sev} ---\n"
        if desc:
            header += f"\n--- USER DESCRIPTION ---\n{desc}\n"
        header += "\n"
        return header + self._base_report

    def _refresh_preview(self) -> None:
        self._preview.setPlainText(self._compose_report())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _copy_report(self) -> None:
        QApplication.clipboard().setText(self._compose_report())
        InfoBar.success(
            title=tr("main.copied"),
            content=tr("bug.copied"),
            duration=3000, position=InfoBarPosition.TOP, parent=self,
        )

    # ------------------------------------------------------------------
    # PrivateBin upload
    # ------------------------------------------------------------------

    def _get_config(self):
        from cdumm.storage.config import Config
        if self._db is None:
            return None
        return Config(self._db)

    def _upload_report(self) -> None:
        cfg = self._get_config()

        # Settings
        if cfg is not None:
            instance = cfg.get("privatebin_instance") or "https://privatebin.net/"
            expire = cfg.get("privatebin_expire") or "1week"
            acknowledged = cfg.get("privatebin_privacy_ack") == "1"
        else:
            instance = "https://privatebin.net/"
            expire = "1week"
            acknowledged = False

        # First-use privacy dialog
        if not acknowledged:
            dlg = MessageBox(tr("bug.privacy_title"), tr("bug.privacy_body"), self.window())
            dlg.yesButton.setText(tr("bug.privacy_continue"))
            dlg.cancelButton.setText(tr("bug.privacy_cancel"))
            if not getattr(dlg, "exec")():
                return
            if cfg is not None:
                cfg.set("privatebin_privacy_ack", "1")

        # Scrub Windows usernames before upload
        from cdumm.gui.bug_report import scrub_windows_paths
        text = scrub_windows_paths(self._compose_report())

        self._upload_btn.setEnabled(False)
        InfoBar.info(
            title=tr("bug.upload"),
            content=tr("bug.uploading"),
            duration=2500, position=InfoBarPosition.TOP, parent=self,
        )

        # Kill any stale worker (shouldn't happen — button is disabled)
        if self._upload_worker is not None and self._upload_worker.isRunning():
            return

        self._upload_worker = _PrivateBinUploadWorker(instance, text, expire, self)
        self._upload_worker.finished_ok.connect(self._on_upload_ok)
        self._upload_worker.failed.connect(self._on_upload_failed)
        self._upload_worker.finished.connect(self._on_upload_cleanup)
        self._upload_worker.start()

    def _on_upload_ok(self, url: str) -> None:
        QApplication.clipboard().setText(url)
        InfoBar.success(
            title=tr("main.copied"),
            content=tr("bug.upload_ok"),
            duration=6000, position=InfoBarPosition.TOP, parent=self,
        )
        # Open the GitHub issue page so the user can paste the URL right away.
        QDesktopServices.openUrl(QUrl(
            "https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/issues/new/choose"
        ))

    def _on_upload_failed(self, err: str) -> None:
        # Fall back to copying the raw (scrubbed) text so the user still has something to share.
        from cdumm.gui.bug_report import scrub_windows_paths
        QApplication.clipboard().setText(scrub_windows_paths(self._compose_report()))
        InfoBar.warning(
            title=tr("main.error"),
            content=tr("bug.upload_failed", error=err[:200]),
            duration=7000, position=InfoBarPosition.TOP, parent=self,
        )

    def _on_upload_cleanup(self) -> None:
        self._upload_btn.setEnabled(True)
        self._upload_worker = None

    def _save_report(self) -> None:
        from cdumm.storage.config import default_export_dir
        default_name = (
            f"cdumm_bug_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        default_path = default_export_dir(getattr(self, "_db", None)) / default_name
        path, _ = QFileDialog.getSaveFileName(
            self, tr("bug_page.save_dialog_title"),
            str(default_path), "Text Files (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(self._compose_report(), encoding="utf-8")
            InfoBar.success(
                title=tr("main.saved"),
                content=tr("bug.saved", path=path),
                duration=4000, position=InfoBarPosition.TOP, parent=self,
            )
        except Exception as e:
            InfoBar.error(
                title=tr("main.error"),
                content=str(e),
                duration=5000, position=InfoBarPosition.TOP, parent=self,
            )
