"""Bug report dialog — collects logs, system info, and mod state for diagnostics."""
import logging
import platform
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)

from cdumm.i18n import tr
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)

from cdumm import __version__ as APP_VERSION


def generate_bug_report(db: Database | None, game_dir: Path | None,
                        app_data_dir: Path | None) -> str:
    """Build a full bug report string with all diagnostic info."""
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Header
    lines.append("=" * 60)
    lines.append("CRIMSON DESERT ULTIMATE MODS MANAGER -- BUG REPORT")
    lines.append("=" * 60)
    lines.append(f"Generated: {now}")
    lines.append(f"App Version: {APP_VERSION}")
    lines.append("")

    # System info
    lines.append("--- SYSTEM ---")
    lines.append(f"OS: {platform.platform()}")
    lines.append(f"Python: {sys.version}")
    lines.append(f"Frozen: {getattr(sys, 'frozen', False)}")
    if game_dir:
        lines.append(f"Game Dir: {game_dir}")
        lines.append(f"Game Dir Exists: {game_dir.exists()}")
    if app_data_dir:
        lines.append(f"App Data: {app_data_dir}")
        # Disk usage
        try:
            total = sum(f.stat().st_size for f in app_data_dir.rglob("*") if f.is_file())
            lines.append(f"App Data Size: {total / 1048576:.1f} MB")
        except Exception:
            pass
    lines.append("")

    # Database info
    if db:
        lines.append("--- MODS ---")
        try:
            cursor = db.connection.execute(
                "SELECT id, name, mod_type, enabled, priority FROM mods ORDER BY priority"
            )
            mods = cursor.fetchall()
            if mods:
                for mod_id, name, mod_type, enabled, priority in mods:
                    state = "ON" if enabled else "OFF"
                    lines.append(f"  #{priority} [{state}] {name} (id={mod_id}, type={mod_type})")

                    # Delta count
                    dc = db.connection.execute(
                        "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ?", (mod_id,)
                    ).fetchone()[0]
                    lines.append(f"       Deltas: {dc}")
            else:
                lines.append("  (no mods installed)")
        except Exception as e:
            lines.append(f"  Error reading mods: {e}")
        lines.append("")

        # Conflicts
        lines.append("--- CONFLICTS ---")
        try:
            cursor = db.connection.execute(
                "SELECT c.level, c.file_path, c.explanation, c.winner_id, "
                "ma.name, mb.name "
                "FROM conflicts c "
                "JOIN mods ma ON c.mod_a_id = ma.id "
                "JOIN mods mb ON c.mod_b_id = mb.id"
            )
            conflicts = cursor.fetchall()
            if conflicts:
                for level, fpath, explanation, winner_id, name_a, name_b in conflicts:
                    lines.append(f"  [{level}] {name_a} vs {name_b}")
                    lines.append(f"    File: {fpath}")
                    lines.append(f"    {explanation}")
            else:
                lines.append("  (no conflicts)")
        except Exception as e:
            lines.append(f"  Error reading conflicts: {e}")
        lines.append("")

        # Snapshot
        lines.append("--- SNAPSHOT ---")
        try:
            count = db.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            lines.append(f"  Files tracked: {count}")
        except Exception as e:
            lines.append(f"  Error: {e}")
        lines.append("")

    # Log tail
    lines.append("--- LOG (last 100 lines) ---")
    if app_data_dir:
        log_path = app_data_dir / "cdumm.log"
        if log_path.exists():
            try:
                log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = log_lines[-100:] if len(log_lines) > 100 else log_lines
                for ll in tail:
                    lines.append(f"  {ll}")
            except Exception as e:
                lines.append(f"  Error reading log: {e}")
        else:
            lines.append("  (log file not found)")
    lines.append("")
    lines.append("=" * 60)
    lines.append("END OF BUG REPORT")
    lines.append("=" * 60)

    return "\n".join(lines)


class BugReportDialog(MessageBoxBase):
    """Fluent-style bug report dialog."""

    def __init__(self, report_text: str, parent=None, is_crash: bool = False) -> None:
        super().__init__(parent)
        self._base_report = report_text

        self.titleLabel = SubtitleLabel(tr("bug.title"))
        self.viewLayout.addWidget(self.titleLabel)

        if is_crash:
            desc = BodyLabel(
                "The app didn't close normally last time. Please describe what "
                "you were doing when it happened, then copy or save this report."
            )
        else:
            desc = BodyLabel(
                "Describe the problem below, then copy or save the report.\n"
                "Attach it to your Nexus Mods bug report page."
            )
        desc.setWordWrap(True)
        self.viewLayout.addWidget(desc)

        # Severity
        sev_row = QHBoxLayout()
        sev_row.addWidget(CaptionLabel(tr("bug.severity")))
        self._severity = ComboBox()
        self._severity.addItems([
            tr("bug.crash"), tr("bug.wrong"),
            tr("bug.visual"), tr("bug.other"),
        ])
        if is_crash:
            self._severity.setCurrentIndex(0)
        self._severity.setFixedWidth(220)
        sev_row.addWidget(self._severity)
        sev_row.addStretch()
        self.viewLayout.addLayout(sev_row)

        # Theme-aware QTextEdit styling (plain Qt, not qfluentwidgets)
        from qfluentwidgets import isDarkTheme
        if isDarkTheme():
            _te_style = ("QTextEdit { background: #1C2028; color: #E2E8F0; "
                         "border: 1px solid #2D3340; border-radius: 6px; padding: 8px; }")
        else:
            _te_style = ("QTextEdit { background: #FAFBFC; color: #1A202C; "
                         "border: 1px solid #E2E8F0; border-radius: 6px; padding: 8px; }")

        # User description field
        self.viewLayout.addWidget(CaptionLabel(tr("bug.what_happened")))
        self._desc_edit = QTextEdit()
        self._desc_edit.setMaximumHeight(80)
        self._desc_edit.setPlaceholderText(tr("bug.placeholder"))
        self._desc_edit.setStyleSheet(_te_style)
        self.viewLayout.addWidget(self._desc_edit)

        # Report preview
        self.viewLayout.addWidget(CaptionLabel(tr("bug.preview")))
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setPlainText(report_text)
        self._text_edit.setFontFamily("Consolas")
        self._text_edit.setMinimumHeight(250)
        self._text_edit.setStyleSheet(_te_style)
        self.viewLayout.addWidget(self._text_edit)

        # Update preview when user types or changes severity
        self._severity.currentTextChanged.connect(lambda _: self._update_preview())
        self._desc_edit.textChanged.connect(self._update_preview)

        # Action buttons
        btn_row = QHBoxLayout()

        copy_btn = PrimaryPushButton(tr("bug.copy"))
        copy_btn.clicked.connect(self._copy)
        btn_row.addWidget(copy_btn)

        save_btn = PushButton(tr("bug.save"))
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        btn_row.addStretch()
        self.viewLayout.addLayout(btn_row)

        # Override default buttons
        self.yesButton.setText(tr("main.close"))
        self.cancelButton.hide()

        self.widget.setMinimumWidth(700)

    def _update_preview(self) -> None:
        self._text_edit.setPlainText(self._get_full_report())

    def _get_full_report(self) -> str:
        severity = self._severity.currentText()
        desc = self._desc_edit.toPlainText().strip()
        header = f"--- SEVERITY: {severity} ---\n"
        if desc:
            header += f"\n--- USER DESCRIPTION ---\n{desc}\n"
        header += "\n"
        return header + self._base_report

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        clipboard.setText(self._get_full_report())
        InfoBar.success(
            title=tr("main.copied"),
            content=tr("bug.copied"),
            duration=3000, position=InfoBarPosition.TOP, parent=self,
        )

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Bug Report",
            f"cdumm_bug_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt)",
        )
        if path:
            Path(path).write_text(self._get_full_report(), encoding="utf-8")
            InfoBar.success(
                title=tr("main.saved"),
                content=tr("bug.saved", path=path),
                duration=4000, position=InfoBarPosition.TOP, parent=self,
            )
