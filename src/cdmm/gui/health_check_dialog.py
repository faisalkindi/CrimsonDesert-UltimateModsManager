"""Health check results dialog — shows mod validation issues."""
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget,
)

from cdmm.engine.mod_health_check import HealthIssue, generate_bug_report

logger = logging.getLogger(__name__)

SEVERITY_COLORS = {
    "critical": "#FF4444",
    "warning": "#FFAA00",
    "info": "#4488FF",
}

SEVERITY_LABELS = {
    "critical": "CRITICAL",
    "warning": "WARNING",
    "info": "INFO",
}


class HealthCheckDialog(QDialog):
    """Dialog showing mod health check results with bug report export."""

    def __init__(self, issues: list[HealthIssue], mod_name: str,
                 mod_files: dict, parent=None):
        super().__init__(parent)
        self._issues = issues
        self._mod_name = mod_name
        self._mod_files = mod_files
        self._user_choice = "cancel"  # "apply", "cancel"

        self.setWindowTitle(f"Mod Health Check: {mod_name}")
        self.setMinimumSize(700, 500)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Summary
        critical = sum(1 for i in self._issues if i.severity == "critical")
        warnings = sum(1 for i in self._issues if i.severity == "warning")
        info = sum(1 for i in self._issues if i.severity == "info")

        if critical:
            summary = QLabel(
                f"<b style='color:#FF4444'>{critical} critical issue(s) found</b>"
                f"{f', {warnings} warning(s)' if warnings else ''}"
                f" — this mod will likely crash the game."
            )
        elif warnings:
            summary = QLabel(
                f"<b style='color:#FFAA00'>{warnings} warning(s) found</b>"
                f" — mod may not work correctly."
            )
        else:
            summary = QLabel(
                f"<b style='color:#44FF44'>No issues found</b>"
                f"{f' ({info} info note(s))' if info else ''}"
            )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        # Issue list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)

        for issue in self._issues:
            issue_widget = self._create_issue_widget(issue)
            scroll_layout.addWidget(issue_widget)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        # Buttons
        btn_layout = QHBoxLayout()

        copy_btn = QPushButton("Copy Bug Report")
        copy_btn.clicked.connect(self._copy_report)
        btn_layout.addWidget(copy_btn)

        btn_layout.addStretch()

        if critical:
            apply_btn = QPushButton("Apply Anyway (risky)")
            apply_btn.setStyleSheet("color: #FF8888;")
        else:
            apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._on_apply)
        btn_layout.addWidget(apply_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _create_issue_widget(self, issue: HealthIssue) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 4, 8, 4)

        color = SEVERITY_COLORS.get(issue.severity, "#FFFFFF")
        label_text = SEVERITY_LABELS.get(issue.severity, "")

        header = QLabel(
            f"<span style='color:{color}; font-weight:bold'>[{label_text}]</span> "
            f"<b>{issue.code}: {issue.check_name}</b>"
            f"<br><span style='color:#888'>File: {issue.file_path}</span>"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        desc = QLabel(issue.description)
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #CCC; margin-left: 16px;")
        layout.addWidget(desc)

        if issue.fix_description:
            fix = QLabel(f"<i>Auto-fix available: {issue.fix_description}</i>")
            fix.setWordWrap(True)
            fix.setStyleSheet("color: #88CC88; margin-left: 16px;")
            layout.addWidget(fix)

        widget.setStyleSheet(
            f"QWidget {{ border-left: 3px solid {color}; "
            f"margin-bottom: 4px; padding: 4px; }}"
        )
        return widget

    def _copy_report(self):
        from PySide6.QtWidgets import QApplication
        report = generate_bug_report(self._issues, self._mod_name, self._mod_files)
        QApplication.clipboard().setText(report)
        self.parent().statusBar().showMessage("Bug report copied to clipboard!", 5000)

    def _on_apply(self):
        self._user_choice = "apply"
        self.accept()

    @property
    def user_choice(self) -> str:
        return self._user_choice
