"""Right-side sliding configuration panel for mod settings."""

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    PrimaryPushButton,
    SubtitleLabel,
    isDarkTheme,
)

from cdumm.i18n import tr


# ── Colour helpers ────────────────────────────────────────────────────

def _bg() -> str:
    return "#14171E" if isDarkTheme() else "#FAFBFC"


def _left_border() -> str:
    return "#2D3340" if isDarkTheme() else "#E5E7EB"


def _section_color() -> str:
    return "#5CB8F0" if isDarkTheme() else "#2878D0"


def _row_border() -> str:
    return "#252830" if isDarkTheme() else "#F3F4F6"


# ── Badge helper ──────────────────────────────────────────────────────

def _make_badge(text: str, bg: str = "#2878D0", fg: str = "#FFFFFF") -> QLabel:
    badge = QLabel(text)
    badge.setStyleSheet(
        f"background: {bg}; color: {fg}; border-radius: 4px; "
        f"padding: 2px 8px; font-size: 11px; font-weight: 600;"
    )
    badge.setFixedHeight(20)
    return badge


# ======================================================================
# ConfigPanel
# ======================================================================

class ConfigPanel(QWidget):
    """Animated right-side panel for mod configuration.

    Width animates between 0 (closed) and 310 (open).
    """

    panel_closed = Signal()
    apply_clicked = Signal(int, list)  # mod_id, [{"label": str, "enabled": bool}]

    _PANEL_WIDTH = 310
    _ANIM_DURATION = 250

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMaximumWidth(0)
        self.setMinimumWidth(0)
        self.setVisible(False)

        self._mod_id: int = 0
        self._initial_states: dict[int, bool] = {}
        self._toggles: dict[int, QCheckBox] = {}
        self._labels: dict[int, str] = {}

        self._anim = QPropertyAnimation(self, b"maximumWidth")
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.setDuration(self._ANIM_DURATION)

        self._build_ui()
        self._apply_theme()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        # ── Close button ──────────────────────────────────────────────
        close_row = QHBoxLayout()
        close_row.addStretch()
        self._close_btn = QPushButton("\u2715")
        self._close_btn.setFixedSize(28, 28)
        self._close_btn.clicked.connect(self.close_panel)
        close_row.addWidget(self._close_btn)
        root.addLayout(close_row)

        # ── Mod title + author ────────────────────────────────────────
        self._title_label = SubtitleLabel("")
        self._title_label.setWordWrap(True)
        root.addWidget(self._title_label)

        self._author_label = CaptionLabel("")
        self._author_label.setStyleSheet("font-size: 12px; color: #8B95A5;")
        root.addWidget(self._author_label)

        # ── Stat badges ───────────────────────────────────────────────
        self._badge_row = QHBoxLayout()
        self._badge_row.setSpacing(6)
        self._badge_row.addStretch()
        root.addLayout(self._badge_row)

        # ── Scrollable body ───────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)

        # ── Apply button ──────────────────────────────────────────────
        self._apply_btn = PrimaryPushButton(tr("config_panel.apply_changes"))
        self._apply_btn.setVisible(False)
        self._apply_btn.clicked.connect(self._on_apply)
        root.addWidget(self._apply_btn)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_mod(
        self,
        mod_id: int,
        name: str,
        author: str,
        version: str,
        status: str,
        file_count: int,
        patches: list[dict],
        conflicts: list[str],
    ) -> None:
        """Populate the panel with mod data and animate it open."""
        self._mod_id = mod_id
        self._initial_states.clear()
        self._toggles.clear()
        self._labels.clear()
        self._apply_btn.setVisible(False)

        # Header
        self._title_label.setText(name)
        self._author_label.setText(f"by {author}" if author else "")

        # Badges
        self._clear_badges()
        self._badge_row.insertWidget(0, _make_badge(status))
        self._badge_row.insertWidget(1, _make_badge(f"v{version}", "#444C5C"))
        self._badge_row.insertWidget(
            2, _make_badge(f"{file_count} files", "#444C5C"),
        )

        # Rebuild body
        self._clear_body()

        # CONFIGURATION section
        if patches:
            self._add_section_header(tr("config_panel.section_config"))
            for i, p in enumerate(patches):
                self._add_config_row(i, p["label"], p.get("description", ""), p["enabled"])

        # CONFLICTS section
        if conflicts:
            self._add_section_header(tr("config_panel.section_conflicts"))
            for desc in conflicts:
                lbl = CaptionLabel(desc)
                lbl.setWordWrap(True)
                lbl.setStyleSheet("color: #D04848; padding: 4px 0;")
                self._body_layout.addWidget(lbl)

        self._body_layout.addStretch()

        # Apply theme-aware background
        self._apply_theme()

        # Animate open (width + opacity)
        self.setVisible(True)
        self._anim.stop()
        try:
            self._anim.finished.disconnect(self._emit_closed)
        except RuntimeError:
            pass  # not connected
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(self._PANEL_WIDTH)

        # Opacity fade-in
        # Create a fresh effect each time (previous one is deleted by setGraphicsEffect(None))
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(0.0)

        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_anim.setDuration(self._ANIM_DURATION)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(lambda: self.setGraphicsEffect(None))

        self._anim.start()
        self._fade_anim.start()

    def close_panel(self) -> None:
        """Animate the panel closed and emit ``panel_closed``."""
        self._anim.stop()
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(0)
        self._anim.finished.connect(self._emit_closed, Qt.ConnectionType.UniqueConnection)
        self._anim.start()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_closed(self) -> None:
        if self.maximumWidth() == 0:
            self.setVisible(False)
            self.panel_closed.emit()

    def _apply_theme(self) -> None:
        dark = isDarkTheme()
        text_color = "#E0E0E0" if dark else "#1A1A2E"
        caption_color = "#9BA4B5" if dark else "#8B95A5"
        close_color = "#9BA4B5" if dark else "#6B7280"
        close_hover = "#5CB8F0" if dark else "#2878D0"
        self.setStyleSheet(
            f"ConfigPanel {{ background: {_bg()}; "
            f"border-left: 1px solid {_left_border()}; }}"
            f"ConfigPanel QLabel {{ color: {text_color}; }}"
        )
        self._title_label.setStyleSheet(f"color: {text_color}; font-size: 16px; font-weight: bold;")
        self._author_label.setStyleSheet(f"color: {caption_color}; font-size: 12px;")
        self._close_btn.setStyleSheet(
            f"QPushButton {{ border: none; font-size: 16px; color: {close_color}; }}"
            f"QPushButton:hover {{ color: {close_hover}; }}"
        )

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_theme()

    def _clear_badges(self) -> None:
        while self._badge_row.count() > 1:  # keep the stretch
            item = self._badge_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _clear_body(self) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                ConfigPanel._clear_layout(item.layout())

    def _add_section_header(self, text: str) -> None:
        header = CaptionLabel(text)
        header.setStyleSheet(
            f"color: {_section_color()}; font-weight: 700; "
            f"letter-spacing: 0.5px; padding: 12px 0 6px 0; "
            f"text-transform: uppercase; font-size: 11px;"
        )
        self._body_layout.addWidget(header)

    def _add_config_row(self, index: int, label: str, description: str, enabled: bool) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 8)

        # Label + description column
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = CaptionLabel(label)
        name_lbl.setStyleSheet("font-weight: 600; font-size: 13px;")
        text_col.addWidget(name_lbl)
        if description:
            desc_lbl = CaptionLabel(description)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet("color: #8B95A5; font-size: 11px;")
            text_col.addWidget(desc_lbl)
        row.addLayout(text_col, 1)

        # Toggle checkbox
        toggle = QCheckBox()
        toggle.setChecked(enabled)
        unchecked_border = "#5A6270" if isDarkTheme() else "#9CA3AF"
        toggle.setStyleSheet(
            "QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px; }"
            f"QCheckBox::indicator:checked {{ background: #2878D0; border: 2px solid #2878D0; border-radius: 4px; }}"
            f"QCheckBox::indicator:unchecked {{ background: transparent; border: 2px solid {unchecked_border}; border-radius: 4px; }}"
        )
        toggle.toggled.connect(self._on_toggle_changed)
        row.addWidget(toggle, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._toggles[index] = toggle
        self._labels[index] = label
        self._initial_states[index] = enabled

        # Wrap row in a widget for the bottom border
        container = QWidget()
        container.setLayout(row)
        container.setStyleSheet(
            f"border-bottom: 1px solid {_row_border()}; background: transparent;"
        )
        self._body_layout.addWidget(container)

    def _on_toggle_changed(self) -> None:
        """Show the Apply button when any toggle differs from its initial state."""
        changed = any(
            cb.isChecked() != self._initial_states[idx]
            for idx, cb in self._toggles.items()
        )
        self._apply_btn.setVisible(changed)

    def _on_apply(self) -> None:
        result = [
            {"label": self._labels[idx], "enabled": cb.isChecked()}
            for idx, cb in sorted(self._toggles.items())
        ]
        self.apply_clicked.emit(self._mod_id, result)
