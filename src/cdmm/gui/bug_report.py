"""Bug report dialog — collects logs, system info, and mod state for diagnostics."""
import logging
import platform
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from cdmm.storage.database import Database

logger = logging.getLogger(__name__)

APP_VERSION = "1.0.0"


def generate_bug_report(db: Database | None, game_dir: Path | None,
                        app_data_dir: Path | None) -> str:
    """Build a full bug report string with all diagnostic info."""
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Header
    lines.append("=" * 60)
    lines.append("CRIMSON DESERT ULTIMATE MODS MANAGER — BUG REPORT")
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
        log_path = app_data_dir / "cdmm.log"
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


class BugReportDialog(QDialog):
    """Dialog that shows the bug report and lets user copy or save it."""

    def __init__(self, report_text: str, parent=None, is_crash: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bug Report")
        self.setMinimumSize(700, 550)
        self._base_report = report_text

        layout = QVBoxLayout(self)

        if is_crash:
            layout.addWidget(QLabel(
                "The app didn't close normally last time. Please describe what "
                "you were doing when it happened, then copy or save this report."
            ))
        else:
            layout.addWidget(QLabel(
                "Describe the problem below, then copy or save the report.\n"
                "Attach it to your Nexus Mods bug report page."
            ))

        # Severity
        sev_row = QHBoxLayout()
        sev_row.addWidget(QLabel("Severity:"))
        self._severity = QComboBox()
        self._severity.addItems(["Crash (app closed/froze)", "Bug (wrong behavior)",
                                  "Visual (UI issue)", "Other"])
        if is_crash:
            self._severity.setCurrentIndex(0)
        sev_row.addWidget(self._severity)
        sev_row.addStretch()
        layout.addLayout(sev_row)

        # User description field
        layout.addWidget(QLabel("What happened? (steps to reproduce):"))
        self._desc_edit = QTextEdit()
        self._desc_edit.setMaximumHeight(80)
        self._desc_edit.setPlaceholderText(
            "Example: I dropped a zip file, the progress bar reached 68%, then the app froze...")
        layout.addWidget(self._desc_edit)

        # Update preview when user types or changes severity
        self._severity.currentTextChanged.connect(lambda: self._update_preview())

        # Report preview
        layout.addWidget(QLabel("Report preview:"))
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setPlainText(report_text)
        self._text_edit.setFontFamily("Consolas")
        layout.addWidget(self._text_edit)

        # Update preview when user types
        self._desc_edit.textChanged.connect(self._update_preview)

        btn_row = QHBoxLayout()

        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.clicked.connect(self._copy)
        btn_row.addWidget(copy_btn)

        save_btn = QPushButton("Save as File")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        btn_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

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
        QMessageBox.information(self, "Copied", "Bug report copied to clipboard.")

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Bug Report",
            f"cdmm_bug_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt)",
        )
        if path:
            Path(path).write_text(self._get_full_report(), encoding="utf-8")
            QMessageBox.information(self, "Saved", f"Bug report saved to:\n{path}")
