"""Tools page for CDUMM v3 Fluent window."""

from __future__ import annotations

import logging

from PySide6.QtCore import QEasingCurve, Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    FluentIcon,
    PrimaryPushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
)

from cdumm.i18n import tr

logger = logging.getLogger(__name__)

# Tool definitions: (id, icon, title, description)
TOOL_DEFINITIONS = [
    (
        "verify_state",
        FluentIcon.CERTIFICATE,
        "Verify Game State",
        "Check which game files are modded, vanilla, or unexpected. "
        "Shows a detailed breakdown of file integrity.",
    ),
    (
        "check_mods",
        FluentIcon.SEARCH,
        "Check Mods for Issues",
        "Run deep validation on all enabled mods. Detects broken deltas, "
        "missing files, PAMT mismatches, and other problems.",
    ),
    (
        "find_problem_mod",
        FluentIcon.CARE_DOWN_SOLID,
        "Find Problem Mod",
        "Binary search through enabled mods to identify which one causes "
        "a crash or issue. Requires at least 2 enabled mods.",
    ),
    (
        "test_mod",
        FluentIcon.DEVELOPER_TOOLS,
        "Test Mod",
        "Validate a mod archive (.zip) before importing it. Checks file "
        "structure, PAZ targets, and potential conflicts.",
    ),
    (
        "fix_everything",
        FluentIcon.BROOM,
        "Fix Everything",
        "One-click repair: revert all game files, clear old backups, "
        "remove orphan directories, and optionally rescan. Use after "
        "Steam verify for best results.",
    ),
    (
        "rescan_snapshots",
        FluentIcon.SYNC,
        "Rescan Game Files",
        "Create a fresh vanilla snapshot from current game files. "
        "Run this after verifying game files through Steam.",
    ),
]


class _ToolCard(CardWidget):
    """Card representing a diagnostic tool with a Run button."""

    clicked = Signal(str)  # emits tool_id

    def __init__(self, tool_id: str, icon: FluentIcon, title: str,
                 description: str, parent=None):
        super().__init__(parent)
        self._tool_id = tool_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        # Top row: title + button
        from PySide6.QtWidgets import QHBoxLayout
        top = QHBoxLayout()
        top.setSpacing(12)

        title_label = StrongBodyLabel(title, self)
        top.addWidget(title_label, stretch=1)

        run_btn = PrimaryPushButton(tr("tool.run"), self, icon)
        run_btn.setFixedWidth(100)
        run_btn.clicked.connect(lambda: self.clicked.emit(self._tool_id))
        top.addWidget(run_btn)

        layout.addLayout(top)

        # Description
        desc_label = CaptionLabel(description, self)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)


class ToolsPage(SmoothScrollArea):
    """Page with diagnostic and maintenance tools."""

    tool_requested = Signal(str)  # emits tool name to be handled by CdummWindow

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ToolsPage")
        self.setWidgetResizable(True)

        # Content container
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(36, 20, 36, 20)
        self._layout.setSpacing(8)

        # Header
        title = SubtitleLabel(tr("tool.diagnostic_tools"), self._container)
        self._layout.addWidget(title)

        desc = CaptionLabel(
            "Tools for verifying game state, diagnosing mod issues, "
            "and repairing problems.",
            self._container,
        )
        desc.setWordWrap(True)
        self._layout.addWidget(desc)

        # Tool cards
        self._layout.addSpacing(8)
        for tool_id, icon, name, description in TOOL_DEFINITIONS:
            card = _ToolCard(tool_id, icon, name, description, self._container)
            card.clicked.connect(self._on_tool_clicked)
            self._layout.addWidget(card)

        self._layout.addStretch()

        self.setWidget(self._container)
        self.enableTransparentBackground()
        self.setScrollAnimation(Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint)

    def set_managers(self, **kwargs) -> None:
        """Accept engine references (not needed for tools, but keeps interface uniform)."""
        pass

    def refresh(self) -> None:
        """No-op -- tool cards are static."""
        pass

    def _on_tool_clicked(self, tool_id: str) -> None:
        """Forward tool request to the window."""
        self.tool_requested.emit(tool_id)
