"""Activity Log panel — shows a persistent, color-coded history of all CDUMM actions."""

import logging
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextBrowser, QComboBox,
    QLineEdit, QPushButton, QLabel,
)

from cdumm.engine.activity_log import ActivityLog, CATEGORY_COLORS

logger = logging.getLogger(__name__)


class ActivityPanel(QWidget):
    """Scrollable, color-coded activity log with session filtering and search."""

    def __init__(self, activity_log: ActivityLog, parent=None):
        super().__init__(parent)
        self._log = activity_log
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Header
        header = QLabel("Activity Log")
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #ECEFF4;")
        layout.addWidget(header)

        # Toolbar row: session filter + search
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        toolbar.addWidget(QLabel("Session:"))
        self._session_combo = QComboBox()
        self._session_combo.setMinimumWidth(200)
        self._session_combo.currentIndexChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self._session_combo)

        toolbar.addSpacing(12)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search logs...")
        self._search_input.returnPressed.connect(self._on_search)
        toolbar.addWidget(self._search_input)

        search_btn = QPushButton("Search")
        search_btn.setFixedWidth(70)
        search_btn.clicked.connect(self._on_search)
        toolbar.addWidget(search_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._on_clear_search)
        toolbar.addWidget(clear_btn)

        toolbar.addSpacing(12)

        export_btn = QPushButton("Export Log")
        export_btn.setFixedWidth(90)
        export_btn.clicked.connect(self._on_export)
        toolbar.addWidget(export_btn)

        layout.addLayout(toolbar)

        # Legend
        legend = QHBoxLayout()
        legend.setSpacing(12)
        for cat, color in CATEGORY_COLORS.items():
            dot = QLabel(f'<span style="color:{color};">\u25CF</span> {cat}')
            dot.setStyleSheet("font-size: 11px; color: #788090;")
            legend.addWidget(dot)
        legend.addStretch()
        layout.addLayout(legend)

        # Log browser
        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(False)
        self._browser.setStyleSheet(
            "QTextBrowser { background: #0D0F12; border: 1px solid #2E3440; "
            "border-radius: 6px; padding: 8px; font-family: 'Consolas', 'Cascadia Mono', monospace; "
            "font-size: 12px; color: #D8DEE9; }"
        )
        layout.addWidget(self._browser)

    def refresh(self):
        """Reload session list and show latest session."""
        self._session_combo.blockSignals(True)
        self._session_combo.clear()
        self._session_combo.addItem("All Sessions", None)

        sessions = self._log.get_sessions(limit=30)
        for s in sessions:
            label = f"Session {s['id']} — {s['started_at']} (v{s['version']}, {s['count']} entries)"
            self._session_combo.addItem(label, s["id"])

        # Select latest session by default
        if len(sessions) > 0:
            self._session_combo.setCurrentIndex(1)
        self._session_combo.blockSignals(False)
        self._on_filter_changed()

    def _on_filter_changed(self):
        session_id = self._session_combo.currentData()
        entries = self._log.get_entries(session_id=session_id)
        self._render_entries(entries)

    def _on_search(self):
        query = self._search_input.text().strip()
        if not query:
            self._on_filter_changed()
            return
        entries = self._log.search(query)
        self._render_entries(entries)

    def _on_clear_search(self):
        self._search_input.clear()
        self._on_filter_changed()

    def _on_export(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Activity Log", "cdumm_activity_log.txt",
            "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        entries = self._log.get_entries()
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                detail = f" — {e['detail']}" if e.get('detail') else ""
                f.write(f"[{e['timestamp']}] [{e['category'].upper()}] {e['message']}{detail}\n")

    def _render_entries(self, entries: list[dict]):
        html_parts = ['<table cellspacing="0" cellpadding="2" style="width:100%;">']

        if not entries:
            html_parts.append(
                '<tr><td style="color:#788090; padding:20px; text-align:center;">'
                'No log entries</td></tr>')
        else:
            for entry in entries:
                cat = entry["category"]
                color = CATEGORY_COLORS.get(cat, "#788090")
                ts = entry["timestamp"]
                msg = entry["message"]
                detail = entry.get("detail") or ""

                # Time column + colored category badge + message
                html_parts.append(
                    f'<tr>'
                    f'<td style="color:#4C566A; white-space:nowrap; padding-right:8px; '
                    f'vertical-align:top;">{ts}</td>'
                    f'<td style="color:{color}; font-weight:bold; white-space:nowrap; '
                    f'padding-right:8px; vertical-align:top;">[{cat.upper()}]</td>'
                    f'<td style="color:#D8DEE9;">{msg}'
                )
                if detail:
                    html_parts.append(
                        f'<br><span style="color:#788090; font-size:11px;">{detail}</span>'
                    )
                html_parts.append('</td></tr>')

        html_parts.append('</table>')
        self._browser.setHtml("\n".join(html_parts))
