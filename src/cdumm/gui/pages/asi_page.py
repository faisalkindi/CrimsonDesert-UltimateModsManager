"""ASI Plugins page for CDUMM v3 — card-based layout matching PAZ Mods page.

Rebuilt to use the same visual treatment as ModsPage: summary bar at top,
section header with search, select-all checkbox, card list with multi-select,
right-click context menu, and Ctrl+Click/Shift+Click selection.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QEasingCurve, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    FluentIcon,
    IconWidget,
    PushButton,
    RoundMenu,
    SearchLineEdit,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    getFont,
    isDarkTheme,
    setCustomStyleSheet,
)

from cdumm.asi.asi_manager import AsiManager, AsiPlugin
from cdumm.i18n import tr

logger = logging.getLogger(__name__)


# ======================================================================
# Color tables (shared with mod_card.py conventions)
# ======================================================================

_STATUS_COLORS = {
    "enabled": {
        "light": {"bg": "#E8F5E9", "text": "#2E7D32", "border": "#A5D6A7"},
        "dark":  {"bg": "#1A2E1A", "text": "#81C784", "border": "#2E5E2E"},
    },
    "disabled": {
        "light": {"bg": "#F5F5F5", "text": "#757575", "border": "#E0E0E0"},
        "dark":  {"bg": "#252830", "text": "#6B7280", "border": "#3A3E48"},
    },
}

_SIZE_COLORS = {
    "light": {"bg": "#E8EAF6", "text": "#283593", "border": "#9FA8DA"},
    "dark":  {"bg": "#1A2040", "text": "#9FA8DA", "border": "#2D3A6E"},
}

_CARD_COLORS = {
    "light": {"bg": "#FFFFFF", "border": "#E5E7EB", "hover": "#F9FAFB"},
    "dark":  {"bg": "#1C2028", "border": "#2D3340", "hover": "#242A34"},
}


def _theme_key() -> str:
    return "dark" if isDarkTheme() else "light"


def _pill_qss(colors: dict) -> str:
    return (
        f"background: {colors['bg']};"
        f"color: {colors['text']};"
        f"border: 1px solid {colors['border']};"
        "border-radius: 11px;"
        "padding: 4px 14px;"
        "font-size: 12px;"
        "font-weight: 600;"
    )


def _humanize_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ======================================================================
# AsiStatusBadge
# ======================================================================

class _AsiStatusBadge(QLabel):
    """Colored pill badge showing ASI plugin status (Enabled/Disabled)."""

    def __init__(self, status: str, parent=None):
        super().__init__(parent)
        self._status = status.lower()
        self.setFixedHeight(22)
        self.setText(tr(f"status.{self._status}"))
        self._apply_style()

    def set_status(self, status: str) -> None:
        self._status = status.lower()
        self.setText(tr(f"status.{self._status}"))
        self._apply_style()

    def _apply_style(self) -> None:
        key = _theme_key()
        colors = _STATUS_COLORS.get(self._status, _STATUS_COLORS["disabled"])
        self.setStyleSheet(_pill_qss(colors[key]))

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_style()


# ======================================================================
# AsiCard
# ======================================================================

class AsiCard(CardWidget):
    """Card representing a single ASI plugin.

    Same fixed-column layout as ModCard for visual consistency:
      Col 0: Checkbox  (24px)
      Col 1: Order     (30px)
      Col 2: Name+Author (stretch)
      Col 3: Status    (85px)
      Col 4: Size pill  (70px)
      Col 5: Config icon (18px, visible if INI exists)
    """

    toggled = Signal(str, bool)       # plugin name, checked
    config_clicked = Signal(str)      # plugin name
    context_menu_requested = Signal(str, object)  # plugin name, QPoint
    card_clicked = Signal(str, object)  # plugin name, QMouseEvent
    renamed = Signal(str, str)  # old plugin name, new name

    def __init__(
        self,
        plugin: AsiPlugin,
        order: int,
        parent=None,
    ):
        super().__init__(parent)
        self._plugin = plugin
        self._selected = False

        self._apply_flat_style()

        # -- Root layout --
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(0)

        # Col 0: Checkbox (fixed)
        self._checkbox = QCheckBox()
        self._checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._checkbox.setFixedWidth(24)
        self._checkbox.setChecked(plugin.enabled)
        self._apply_checkbox_style()
        self._checkbox.toggled.connect(self._on_toggled)
        root.addWidget(self._checkbox)
        root.addSpacing(8)

        # Col 1: Order (fixed)
        self._order_label = CaptionLabel(f"#{order}")
        self._order_label.setFixedWidth(30)
        self._apply_order_style()
        root.addWidget(self._order_label)
        root.addSpacing(8)

        # Col 2: Name + info (stretch)
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self._name_label = StrongBodyLabel(plugin.name)
        name_row.addWidget(self._name_label)
        # Inline rename editor (hidden by default)
        self._name_edit = QLineEdit(plugin.name)
        self._name_edit.setVisible(False)
        self._name_edit.returnPressed.connect(self._finish_rename)
        self._name_edit.editingFinished.connect(self._finish_rename)
        name_row.addWidget(self._name_edit)

        # Config gear icon (visible only if INI exists)
        self._gear = IconWidget(FluentIcon.SETTING, self)
        self._gear.setFixedSize(18, 18)
        has_ini = plugin.ini_path and plugin.ini_path.exists()
        if has_ini:
            self._gear.setCursor(Qt.CursorShape.PointingHandCursor)
            self._gear.mousePressEvent = self._on_gear_clicked
        else:
            self._gear.setVisible(False)
        name_row.addWidget(self._gear)
        name_row.addStretch()
        info.addLayout(name_row)

        # Author line — show hook count if any, otherwise empty
        if plugin.hook_targets:
            author_text = f"{len(plugin.hook_targets)} hook target(s)"
        else:
            author_text = ""
        self._author_label = CaptionLabel(author_text)
        info.addWidget(self._author_label)
        root.addLayout(info, 1)
        root.addSpacing(12)

        # Col 3: Status badge (fixed width)
        status_str = "enabled" if plugin.enabled else "disabled"
        self._status_badge = _AsiStatusBadge(status_str)
        self._status_badge.setMinimumWidth(85)
        self._status_badge.setMaximumWidth(85)
        self._status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_badge)
        root.addSpacing(8)

        # Col 4: File size pill (fixed width)
        try:
            size = plugin.path.stat().st_size
            size_text = _humanize_size(size)
        except OSError:
            size_text = "?"
        self._size_pill = QLabel(size_text)
        self._size_pill.setFixedHeight(22)
        self._size_pill.setMinimumWidth(70)
        self._size_pill.setMaximumWidth(70)
        self._size_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_size_style()
        root.addWidget(self._size_pill)

    # -- Public API --

    @property
    def plugin_name(self) -> str:
        return self._plugin.name

    @property
    def plugin(self) -> AsiPlugin:
        return self._plugin

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        if selected:
            self._apply_selected_style()
        else:
            self._apply_flat_style()

    def set_checked(self, checked: bool) -> None:
        self._checkbox.blockSignals(True)
        self._checkbox.setChecked(checked)
        self._checkbox.blockSignals(False)

    def set_status(self, status: str) -> None:
        self._status_badge.set_status(status)

    def start_rename(self) -> None:
        """Switch to inline edit mode for the name."""
        self._name_edit.setText(self._name_label.text())
        self._name_label.setVisible(False)
        self._name_edit.setVisible(True)
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _finish_rename(self) -> None:
        """Commit inline rename and switch back to label."""
        if not self._name_edit.isVisible():
            return
        new_name = self._name_edit.text().strip()
        old_name = self._name_label.text()
        self._name_edit.setVisible(False)
        self._name_label.setVisible(True)
        if new_name and new_name != old_name:
            self._name_label.setText(new_name)
            self.renamed.emit(old_name, new_name)

    # -- Hover --

    def enterEvent(self, event):  # noqa: N802
        super().enterEvent(event)
        if not self._selected:
            light_qss = f"CardWidget{{border: 1px solid {_CARD_COLORS['light']['border']}; background: {_CARD_COLORS['light']['hover']};}}"
            dark_qss = f"CardWidget{{border: 1px solid {_CARD_COLORS['dark']['border']}; background: {_CARD_COLORS['dark']['hover']};}}"
            setCustomStyleSheet(self, light_qss, dark_qss)

    def leaveEvent(self, event):  # noqa: N802
        super().leaveEvent(event)
        if self._selected:
            self._apply_selected_style()
        else:
            self._apply_flat_style()

    # -- Theme change --

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_flat_style()
            self._apply_checkbox_style()
            self._apply_order_style()
            self._apply_size_style()
            self._status_badge._apply_style()

    # -- Internal styles --

    def _apply_checkbox_style(self) -> None:
        dark = isDarkTheme()
        checked_color = "#5CB8F0" if dark else "#2878D0"
        unchecked_border = "#5A6270" if dark else "#9CA3AF"
        self._checkbox.setStyleSheet(
            "QCheckBox::indicator { width: 16px; height: 16px; }"
            f"QCheckBox::indicator:checked {{ background: {checked_color}; border: 2px solid {checked_color}; border-radius: 4px; }}"
            f"QCheckBox::indicator:unchecked {{ background: transparent; border: 2px solid {unchecked_border}; border-radius: 4px; }}"
        )

    def _apply_order_style(self) -> None:
        color = "#9CA3AF" if isDarkTheme() else "#6B7280"
        self._order_label.setStyleSheet(f"color: {color};")

    def _apply_flat_style(self) -> None:
        light_qss = f"CardWidget{{border: 1px solid {_CARD_COLORS['light']['border']};}}"
        dark_qss = f"CardWidget{{border: 1px solid {_CARD_COLORS['dark']['border']};}}"
        setCustomStyleSheet(self, light_qss, dark_qss)
        self._updateBackgroundColor()

    def _apply_selected_style(self) -> None:
        light_qss = "CardWidget{border: 2px solid #2878D0;}"
        dark_qss = "CardWidget{border: 2px solid #5CB8F0;}"
        setCustomStyleSheet(self, light_qss, dark_qss)
        self._updateBackgroundColor()

    def _normalBackgroundColor(self):
        if getattr(self, '_selected', False):
            return QColor("#1A2A3E") if isDarkTheme() else QColor("#D4E8FC")
        return super()._normalBackgroundColor()

    def _hoverBackgroundColor(self):
        if getattr(self, '_selected', False):
            return QColor("#1E3048") if isDarkTheme() else QColor("#C5DEFA")
        return super()._hoverBackgroundColor()

    def _apply_size_style(self) -> None:
        key = _theme_key()
        self._size_pill.setStyleSheet(_pill_qss(_SIZE_COLORS[key]))

    # -- Signal handlers --

    def _on_toggled(self, checked: bool) -> None:
        self.toggled.emit(self._plugin.name, checked)

    def _on_gear_clicked(self, _event) -> None:
        self.config_clicked.emit(self._plugin.name)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.card_clicked.emit(self._plugin.name, event)

    def contextMenuEvent(self, event):  # noqa: N802
        self.context_menu_requested.emit(self._plugin.name, event.globalPos())


# ======================================================================
# AsiSummaryBar
# ======================================================================

class _AsiSummaryBar(QWidget):
    """Horizontal bar showing ASI plugin statistics and loader status.

    Matches SummaryBar layout but with ASI-specific stats:
    Total | Enabled | Disabled | [Loader status] | [Refresh button]
    """

    refresh_clicked = Signal()

    # (tr_key, dot color)
    _STAT_DEFS = [
        ("stats.total", "#2878D0"),
        ("asi.status_enabled", "#22C55E"),
        ("asi.status_disabled", "#9CA3AF"),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("AsiSummaryBar")
        self.setFixedHeight(48)
        self._apply_bar_style()

        from PySide6.QtGui import QFont

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 0, 24, 0)
        root.setSpacing(20)

        # Stat items
        self._number_labels: list[StrongBodyLabel] = []
        self._caption_labels: list[CaptionLabel] = []
        for tr_key, dot_color in self._STAT_DEFS:
            item = QHBoxLayout()
            item.setSpacing(6)
            item.setContentsMargins(0, 0, 0, 0)

            dot = QLabel()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(
                f"background: {dot_color}; border-radius: 4px; border: none;"
            )
            item.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

            number = StrongBodyLabel("0")
            number.setFont(getFont(18, QFont.Weight.DemiBold))
            item.addWidget(number, 0, Qt.AlignmentFlag.AlignVCenter)
            self._number_labels.append(number)

            caption = CaptionLabel(tr(tr_key))
            item.addWidget(caption, 0, Qt.AlignmentFlag.AlignVCenter)
            self._caption_labels.append(caption)

            root.addLayout(item)

        # Loader status indicator
        self._loader_dot = QLabel()
        self._loader_dot.setFixedSize(8, 8)
        self._loader_dot.setStyleSheet(
            "background: #9CA3AF; border-radius: 4px; border: none;"
        )

        self._loader_label = CaptionLabel("ASI Loader: Unknown")

        loader_item = QHBoxLayout()
        loader_item.setSpacing(6)
        loader_item.setContentsMargins(0, 0, 0, 0)
        loader_item.addWidget(self._loader_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        loader_item.addWidget(self._loader_label, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(loader_item)

        root.addStretch(1)

        # Refresh button
        from PySide6.QtGui import QFont as _QFont
        self._refresh_btn = PushButton(FluentIcon.SYNC, tr("asi.refresh"))
        self._refresh_btn.setFixedHeight(34)
        _rbf = self._refresh_btn.font()
        _rbf.setPixelSize(13)
        _rbf.setWeight(_QFont.Weight.Bold)
        self._refresh_btn.setFont(_rbf)
        self._refresh_btn.clicked.connect(self.refresh_clicked)
        setCustomStyleSheet(self._refresh_btn,
            "PushButton { background: #F0F4FF; color: #2878D0; border: 1px solid #B8D4F0; border-radius: 17px; padding: 0 16px; padding-bottom: 6px; }"
            "PushButton:hover { background: #E0ECFF; }"
            "PushButton:pressed { background: #D0E0F8; }",
            "PushButton { background: #1A2840; color: #5CB8F0; border: 1px solid #2A4060; border-radius: 17px; padding: 0 16px; padding-bottom: 6px; }"
            "PushButton:hover { background: #223450; }"
            "PushButton:pressed { background: #2A3C58; }")
        root.addWidget(self._refresh_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def update_stats(self, total: int = 0, enabled: int = 0, disabled: int = 0) -> None:
        values = (total, enabled, disabled)
        for label, value in zip(self._number_labels, values):
            label.setText(str(value))

    def set_loader_status(self, installed: bool) -> None:
        self._loader_installed = installed
        if installed:
            self._loader_dot.setStyleSheet(
                "background: #22C55E; border-radius: 4px; border: none;"
            )
            self._loader_label.setText(tr("asi.loader_installed"))
        else:
            self._loader_dot.setStyleSheet(
                "background: #EF4444; border-radius: 4px; border: none;"
            )
            self._loader_label.setText(tr("asi.loader_missing"))

    def retranslate_ui(self) -> None:
        """Update text with current translations."""
        for caption, (tr_key, _) in zip(self._caption_labels, self._STAT_DEFS):
            caption.setText(tr(tr_key))
        self._refresh_btn.setText(tr("asi.refresh"))
        if hasattr(self, '_loader_installed'):
            self.set_loader_status(self._loader_installed)

    def _apply_bar_style(self) -> None:
        dark = isDarkTheme()
        self._bar_bg = "#14171E" if dark else "#FAFBFC"
        self._bar_border = "#2D3340" if dark else "#E5E7EB"
        self.setAutoFillBackground(False)
        self.update()

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QColor, QPainter, QPen
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self._bar_bg if hasattr(self, '_bar_bg') else "#FAFBFC"))
        border_color = self._bar_border if hasattr(self, '_bar_border') else "#E5E7EB"
        painter.setPen(QPen(QColor(border_color), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_bar_style()


# ======================================================================
# AsiPluginsPage
# ======================================================================

class AsiPluginsPage(QWidget):
    """ASI Plugins page — card list with summary bar, search, multi-select.

    Matches ModsPage treatment: the page does NOT inherit ScrollArea; it
    contains one internally so that the summary bar stays pinned at top.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("AsiPluginsPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

        # Engine references
        self._asi_manager: AsiManager | None = None
        self._game_dir: Path | None = None

        # Card tracking
        self._cards: list[AsiCard] = []
        self._initial_load_done = False
        self._plugins: list[AsiPlugin] = []
        self._last_clicked_index: int | None = None

        self._build_ui()

        # Ctrl+A shortcut
        QShortcut(QKeySequence.StandardKey.SelectAll, self, self._on_ctrl_a)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # Summary bar (pinned top)
        self._summary_bar = _AsiSummaryBar(self)
        self._summary_bar.refresh_clicked.connect(self.refresh)
        main.addWidget(self._summary_bar)

        # Body
        body = QVBoxLayout()
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(10)

        # Section header row: title + search
        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        self._section_title = SubtitleLabel(tr("asi.title"))
        header_row.addWidget(self._section_title)
        header_row.addStretch()

        self._search_edit = SearchLineEdit(self)
        self._search_edit.setPlaceholderText(tr("asi.search_placeholder"))
        self._search_edit.setFixedWidth(220)
        self._search_edit.textChanged.connect(self._on_search)
        header_row.addWidget(self._search_edit)
        body.addLayout(header_row)

        # Select-all row
        select_row = QHBoxLayout()
        select_row.setContentsMargins(14, 0, 0, 0)
        self._select_all_cb = CheckBox(tr("asi.select_all"))
        self._select_all_cb.setTristate(True)
        self._select_all_cb.clicked.connect(self._on_select_all)
        select_row.addWidget(self._select_all_cb)
        select_row.addStretch()
        body.addLayout(select_row)

        # Scrollable card list
        self._scroll = SmoothScrollArea(self)
        self._scroll.setScrollAnimation(
            Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint
        )
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(SmoothScrollArea.Shape.NoFrame)

        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll_layout.setSpacing(4)

        # Empty state label
        self._empty_label = BodyLabel(
            "No ASI plugins found. Drop .asi files into the game's bin64 directory."
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.hide()
        self._scroll_layout.addWidget(self._empty_label)

        self._scroll_layout.addStretch()

        self._scroll.setWidget(self._scroll_content)
        self._scroll.enableTransparentBackground()  # MUST be after setWidget
        body.addWidget(self._scroll, 1)

        main.addLayout(body, 1)

    # ------------------------------------------------------------------
    # Engine wiring
    # ------------------------------------------------------------------

    def set_managers(self, game_dir: Path | None = None, **kwargs) -> None:
        """Receive engine references from CdummWindow."""
        self._game_dir = game_dir
        if game_dir:
            self._asi_manager = AsiManager(game_dir / "bin64")
        self.refresh()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Rescan ASI plugins and rebuild the card list."""
        # Clear existing cards
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._plugins.clear()

        # Remove all widgets from scroll layout except empty_label and stretch
        while self._scroll_layout.count() > 2:
            item = self._scroll_layout.takeAt(0)
            # already cleaned up above

        if not self._asi_manager:
            self._summary_bar.set_loader_status(False)
            self._summary_bar.update_stats(0, 0, 0)
            self._empty_label.setText("Game directory not configured.")
            self._empty_label.show()
            return

        # Install/update bundled loader
        self._install_bundled_loader()

        # Loader status
        has_loader = self._asi_manager.has_loader()
        self._summary_bar.set_loader_status(has_loader)

        # Scan plugins
        self._plugins = self._asi_manager.scan()

        if not self._plugins:
            self._empty_label.show()
            self._summary_bar.update_stats(0, 0, 0)
            self._sync_select_all()
            return

        self._empty_label.hide()

        for order, plugin in enumerate(self._plugins, start=1):
            card = AsiCard(plugin, order, parent=self._scroll_content)
            card.toggled.connect(self._on_card_toggled)
            card.config_clicked.connect(self._on_config_clicked)
            card.context_menu_requested.connect(self._show_context_menu)
            card.renamed.connect(self._on_asi_renamed)
            card.card_clicked.connect(self._on_card_clicked)
            self._cards.append(card)
            # Insert before the stretch
            self._scroll_layout.insertWidget(
                self._scroll_layout.count() - 1, card
            )

        self._update_stats()
        self._sync_select_all()

        # Staggered entrance animation only on first load
        if not self._initial_load_done:
            from cdumm.gui.components.card_animations import staggered_fade_in
            self._entrance_anim = staggered_fade_in(self._cards)
            self._initial_load_done = True

    def retranslate_ui(self) -> None:
        """Update text with current translations."""
        self._section_title.setText(tr("asi.title"))
        self._search_edit.setPlaceholderText(tr("asi.search_placeholder"))
        self._select_all_cb.setText(tr("asi.select_all"))
        self._summary_bar.retranslate_ui()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def _update_stats(self) -> None:
        total = len(self._cards)
        enabled = sum(1 for c in self._cards if c._checkbox.isChecked())
        disabled = total - enabled
        self._summary_bar.update_stats(total, enabled, disabled)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search(self, text: str) -> None:
        needle = text.strip().lower()
        for card in self._cards:
            if not needle:
                card.setVisible(True)
            else:
                card.setVisible(needle in card._name_label.text().lower())
        self._sync_select_all()

    # ------------------------------------------------------------------
    # Select all
    # ------------------------------------------------------------------

    def _on_select_all(self) -> None:
        checked = self._select_all_cb.isChecked()
        for card in self._cards:
            if card.isVisible():
                card.set_checked(checked)
                self._toggle_plugin(card.plugin, checked)
                card.set_status("enabled" if checked else "disabled")
        self._update_stats()

    def _sync_select_all(self) -> None:
        visible = [c for c in self._cards if c.isVisible()]
        if not visible:
            self._select_all_cb.blockSignals(True)
            self._select_all_cb.setCheckState(Qt.CheckState.Unchecked)
            self._select_all_cb.blockSignals(False)
            return

        checked_count = sum(1 for c in visible if c._checkbox.isChecked())
        self._select_all_cb.blockSignals(True)
        if checked_count == 0:
            self._select_all_cb.setCheckState(Qt.CheckState.Unchecked)
        elif checked_count == len(visible):
            self._select_all_cb.setCheckState(Qt.CheckState.Checked)
        else:
            self._select_all_cb.setCheckState(Qt.CheckState.PartiallyChecked)
        self._select_all_cb.blockSignals(False)

    # ------------------------------------------------------------------
    # Card toggle (enable/disable plugin)
    # ------------------------------------------------------------------

    def _on_card_toggled(self, plugin_name: str, enabled: bool) -> None:
        plugin = self._find_plugin(plugin_name)
        if plugin:
            self._toggle_plugin(plugin, enabled)
            for card in self._cards:
                if card.plugin_name == plugin_name:
                    card.set_status("enabled" if enabled else "disabled")
                    break
        self._sync_select_all()
        self._update_stats()

    def _toggle_plugin(self, plugin: AsiPlugin, enable: bool) -> None:
        if not self._asi_manager:
            return
        try:
            if enable:
                self._asi_manager.enable(plugin)
            else:
                self._asi_manager.disable(plugin)
        except Exception as e:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.error(
                title="Error",
                content=str(e),
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )

    # ------------------------------------------------------------------
    # Config (open INI)
    # ------------------------------------------------------------------

    def _on_config_clicked(self, plugin_name: str) -> None:
        plugin = self._find_plugin(plugin_name)
        if plugin and self._asi_manager:
            self._asi_manager.open_config(plugin)

    # ------------------------------------------------------------------
    # Ctrl+Click / Shift+Click selection
    # ------------------------------------------------------------------

    def _on_card_clicked(self, plugin_name: str, event) -> None:
        visible = [c for c in self._cards if c.isVisible()]
        if not visible:
            return

        clicked_idx = None
        for i, card in enumerate(visible):
            if card.plugin_name == plugin_name:
                clicked_idx = i
                break
        if clicked_idx is None:
            return

        mods = event.modifiers()

        if mods & Qt.KeyboardModifier.ShiftModifier and self._last_clicked_index is not None:
            start = min(self._last_clicked_index, clicked_idx)
            end = max(self._last_clicked_index, clicked_idx)
            for i, c in enumerate(visible):
                c.set_selected(start <= i <= end)
        elif mods & Qt.KeyboardModifier.ControlModifier:
            visible[clicked_idx].set_selected(not visible[clicked_idx].is_selected)
        else:
            for c in visible:
                c.set_selected(False)
            visible[clicked_idx].set_selected(True)

        self._last_clicked_index = clicked_idx

    def _on_ctrl_a(self) -> None:
        visible = [c for c in self._cards if c.isVisible()]
        all_selected = all(c.is_selected for c in visible)
        for c in visible:
            c.set_selected(not all_selected)

    def _deselect_all_cards(self) -> None:
        for card in self._cards:
            card.set_selected(False)

    def _get_selected_names(self) -> list[str]:
        return [c.plugin_name for c in self._cards if c.is_selected]

    # ------------------------------------------------------------------
    # Context menu (right-click)
    # ------------------------------------------------------------------

    def _show_context_menu(self, plugin_name: str, global_pos) -> None:
        if not self._asi_manager:
            return

        # Explorer behavior: if right-clicked card not selected, select only it
        selected_names = self._get_selected_names()
        if plugin_name not in selected_names:
            self._deselect_all_cards()
            for c in self._cards:
                if c.plugin_name == plugin_name:
                    c.set_selected(True)
                    break
            selected_names = [plugin_name]

        multi = len(selected_names) > 1
        plugin = self._find_plugin(plugin_name)
        if not plugin:
            return

        menu = RoundMenu(parent=self)

        if multi:
            menu.addAction(Action(
                FluentIcon.ACCEPT, f"Enable {len(selected_names)} plugins",
                triggered=lambda: self._ctx_batch_toggle(selected_names, True),
            ))
            menu.addAction(Action(
                FluentIcon.REMOVE, f"Disable {len(selected_names)} plugins",
                triggered=lambda: self._ctx_batch_toggle(selected_names, False),
            ))
            menu.addSeparator()
            menu.addAction(Action(
                FluentIcon.DELETE, f"Uninstall {len(selected_names)} plugins",
                triggered=lambda: self._ctx_batch_uninstall(selected_names),
            ))
        else:
            # Single select
            if plugin.enabled:
                menu.addAction(Action(
                    FluentIcon.REMOVE, "Disable",
                    triggered=lambda: self._ctx_toggle(plugin_name, False),
                ))
            else:
                menu.addAction(Action(
                    FluentIcon.ACCEPT, "Enable",
                    triggered=lambda: self._ctx_toggle(plugin_name, True),
                ))

            menu.addSeparator()

            # Open config (if INI exists)
            if plugin.ini_path and plugin.ini_path.exists():
                menu.addAction(Action(
                    FluentIcon.EDIT, "Edit Config",
                    triggered=lambda: self._on_config_clicked(plugin_name),
                ))

            # Rename
            menu.addAction(Action(
                FluentIcon.EDIT, "Rename",
                triggered=lambda: self._ctx_rename(plugin_name),
            ))

            # Open folder
            menu.addAction(Action(
                FluentIcon.FOLDER, "Open Folder",
                triggered=lambda: self._ctx_open_folder(plugin),
            ))

            menu.addSeparator()

            # Uninstall
            menu.addAction(Action(
                FluentIcon.DELETE, "Uninstall",
                triggered=lambda: self._ctx_uninstall(plugin_name),
            ))

        menu.exec(global_pos)

    def _ctx_toggle(self, plugin_name: str, enabled: bool) -> None:
        plugin = self._find_plugin(plugin_name)
        if not plugin:
            return
        self._toggle_plugin(plugin, enabled)
        for card in self._cards:
            if card.plugin_name == plugin_name:
                card.set_checked(enabled)
                card.set_status("enabled" if enabled else "disabled")
                break
        self._sync_select_all()
        self._update_stats()

    def _ctx_batch_toggle(self, names: list[str], enabled: bool) -> None:
        for name in names:
            plugin = self._find_plugin(name)
            if not plugin:
                continue
            self._toggle_plugin(plugin, enabled)
            for card in self._cards:
                if card.plugin_name == name:
                    card.set_checked(enabled)
                    card.set_status("enabled" if enabled else "disabled")
                    break
        self._sync_select_all()
        self._update_stats()

    def _ctx_rename(self, plugin_name: str) -> None:
        """Start inline rename on the card."""
        for card in self._cards:
            if card.plugin_name == plugin_name:
                card.start_rename()
                break

    def _on_asi_renamed(self, old_name: str, new_name: str) -> None:
        """Persist the ASI plugin rename on disk."""
        plugin = self._find_plugin(old_name)
        if not plugin:
            return
        ext = ".asi" if plugin.enabled else ".asi.disabled"
        new_path = plugin.path.parent / (new_name + ext)
        try:
            plugin.path.rename(new_path)
        except OSError as e:
            logger.warning("Failed to rename plugin: %s", e)
            self.refresh()
            return
        try:
            if plugin.ini_path and plugin.ini_path.exists():
                new_ini = plugin.path.parent / (new_name + ".ini")
                plugin.ini_path.rename(new_ini)
        except OSError as e:
            logger.warning("Failed to rename plugin INI: %s", e)
        self.refresh()

    def _ctx_open_folder(self, plugin: AsiPlugin) -> None:
        folder = str(plugin.path.parent)
        os.startfile(folder)

    def _ctx_uninstall(self, plugin_name: str) -> None:
        from qfluentwidgets import MessageBox

        plugin = self._find_plugin(plugin_name)
        if not plugin or not self._asi_manager:
            return

        box = MessageBox(
            "Uninstall Plugin",
            f'Remove "{plugin.name}"? This cannot be undone.',
            self.window(),
        )
        if box.exec():
            self._asi_manager.uninstall(plugin)
            self.refresh()

    def _ctx_batch_uninstall(self, names: list[str]) -> None:
        from qfluentwidgets import MessageBox

        if not self._asi_manager:
            return

        box = MessageBox(
            "Uninstall Plugins",
            f"Remove {len(names)} selected plugins? This cannot be undone.",
            self.window(),
        )
        if box.exec():
            for name in names:
                plugin = self._find_plugin(name)
                if plugin:
                    self._asi_manager.uninstall(plugin)
            self.refresh()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_plugin(self, name: str) -> AsiPlugin | None:
        for p in self._plugins:
            if p.name == name:
                return p
        return None

    def _install_bundled_loader(self) -> None:
        """Install or update the bundled ASI loader (winmm.dll) to bin64."""
        import hashlib
        import shutil
        import sys

        if not self._asi_manager:
            return

        if getattr(sys, "frozen", False):
            bundled = Path(sys._MEIPASS) / "asi_loader" / "winmm.dll"
        else:
            bundled = Path(__file__).resolve().parents[4] / "asi_loader" / "winmm.dll"
        if not bundled.exists():
            return

        dst = self._asi_manager._bin64 / "winmm.dll"
        _BUNDLED_HASH = (
            "d257f4639a831e31e10e2d912032604ae088cdefd2c2da5fe6f06ba49616f16a"
            "bc5795b010687e62b88bcb38508f561e5d61ffa4bb79211fe35bda1e1c4c4efa"
        )
        if dst.exists():
            dst_hash = hashlib.sha512(dst.read_bytes()).hexdigest()
            if dst_hash == _BUNDLED_HASH:
                return
            logger.info("Updating ASI loader: %s (hash mismatch)", dst)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, dst)
            logger.info("Installed/updated bundled ASI loader: %s", dst)
        except Exception as e:
            logger.warning("Failed to install ASI loader: %s", e)
