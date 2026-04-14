"""Activity log page for CDUMM v3 Fluent window."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QEasingCurve, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    PillPushButton,
    PushButton,
    SearchLineEdit,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    isDarkTheme,
)

from cdumm.engine.activity_log import ActivityLog, CATEGORY_COLORS
from cdumm.i18n import tr

logger = logging.getLogger(__name__)


class _ActivityEntryCard(CardWidget):
    """Card representing a single activity log entry."""

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self._entry = entry

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(14)

        # Category badge
        category = entry.get("category", "info")
        color = CATEGORY_COLORS.get(category, "#88C0D0")
        badge = CaptionLabel(category.upper(), self)
        badge.setFixedWidth(80)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"background: {color}20; color: {color}; "
            f"border-radius: 6px; padding: 4px 8px; font-weight: 700; font-size: 12px;"
        )
        layout.addWidget(badge)

        # Message + detail
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        text_layout.setContentsMargins(0, 0, 0, 0)

        msg_label = StrongBodyLabel(entry.get("message", ""), self)
        text_layout.addWidget(msg_label)

        detail = entry.get("detail")
        detail_label = None
        if detail:
            detail_label = CaptionLabel(detail, self)
            detail_label.setWordWrap(True)
            detail_label.setTextColor(
                "#9BA4B5" if isDarkTheme() else "#606A7B",
                "#9BA4B5" if isDarkTheme() else "#606A7B",
            )
            text_layout.addWidget(detail_label)

        layout.addLayout(text_layout, stretch=1)

        # Timestamp
        ts_raw = entry.get("timestamp", "")
        ts_display = ts_raw
        try:
            dt = datetime.fromisoformat(ts_raw)
            ts_display = dt.strftime("%b %d, %H:%M")
        except (ValueError, TypeError):
            pass
        self._ts_label = CaptionLabel(ts_display, self)
        self._ts_label.setTextColor(
            "#9BA4B5" if isDarkTheme() else "#606A7B",
            "#9BA4B5" if isDarkTheme() else "#606A7B",
        )
        layout.addWidget(self._ts_label)

        self._badge = badge
        self._detail_label = detail_label
        self._badge_color = color

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            dark = isDarkTheme()
            text_color = "#9BA4B5" if dark else "#606A7B"
            self._ts_label.setTextColor(text_color, text_color)
            if self._detail_label:
                self._detail_label.setTextColor(text_color, text_color)
            c = self._badge_color
            self._badge.setStyleSheet(
                f"background: {c}20; color: {c}; "
                f"border-radius: 4px; padding: 2px 6px; font-weight: 600;"
            )


class ActivityPage(SmoothScrollArea):
    """Page showing the activity log with search/filter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ActivityPage")
        self.setWidgetResizable(True)

        # Engine refs
        self._activity_log: ActivityLog | None = None

        # Content container
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(36, 20, 36, 20)
        self._layout.setSpacing(8)

        # Header row
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        self._title = SubtitleLabel(tr("activity.title"), self._container)
        header_layout.addWidget(self._title)
        header_layout.addStretch()
        from qfluentwidgets import setCustomStyleSheet
        self._refresh_btn = PushButton(tr("activity.refresh"), self._container, FluentIcon.SYNC)
        self._refresh_btn.setFixedHeight(34)
        _rbf = self._refresh_btn.font()
        _rbf.setPixelSize(13)
        _rbf.setWeight(QFont.Weight.Bold)
        self._refresh_btn.setFont(_rbf)
        self._refresh_btn.clicked.connect(self.refresh)
        setCustomStyleSheet(self._refresh_btn,
            "PushButton { background: #F0F4FF; color: #2878D0; border: 1px solid #B8D4F0; border-radius: 17px; padding: 0 16px; padding-bottom: 6px; }"
            "PushButton:hover { background: #E0ECFF; }"
            "PushButton:pressed { background: #D0E0F8; }",
            "PushButton { background: #1A2840; color: #5CB8F0; border: 1px solid #2A4060; border-radius: 17px; padding: 0 16px; padding-bottom: 6px; }"
            "PushButton:hover { background: #223450; }"
            "PushButton:pressed { background: #2A3C58; }")
        header_layout.addWidget(self._refresh_btn)
        self._layout.addLayout(header_layout)

        # Search bar
        self._search = SearchLineEdit(self._container)
        self._search.setPlaceholderText(tr("activity.search_placeholder"))
        self._search.setClearButtonEnabled(True)
        self._search.searchSignal.connect(self._on_search)
        self._search.returnPressed.connect(lambda: self._on_search(self._search.text()))
        self._search.textChanged.connect(self._on_text_changed)
        self._search.clearSignal.connect(self._on_clear_search)
        self._layout.addWidget(self._search)

        # Category filter chips
        self._filter_row = QHBoxLayout()
        self._filter_row.setSpacing(6)
        self._filter_row.setContentsMargins(0, 4, 0, 4)
        self._filter_buttons: dict[str, PushButton] = {}
        self._active_filter: str | None = None

        for category, color in CATEGORY_COLORS.items():
            btn = PushButton(category.upper(), self._container)
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            bf = btn.font()
            bf.setPixelSize(11)
            bf.setWeight(QFont.Weight.DemiBold)
            btn.setFont(bf)
            self._apply_chip_style(btn, color, active=False)
            btn.clicked.connect(
                lambda checked=False, c=category: self._on_filter_chip(c)
            )
            self._filter_row.addWidget(btn)
            self._filter_buttons[category] = btn

        self._filter_row.addStretch()
        self._layout.addLayout(self._filter_row)

        # Stats summary
        self._stats_label = CaptionLabel("", self._container)
        self._layout.addWidget(self._stats_label)

        # Card list area
        self._cards_layout = QVBoxLayout()
        self._cards_layout.setSpacing(4)
        self._layout.addLayout(self._cards_layout)

        # Empty state
        self._empty_label = BodyLabel(
            tr("activity.no_entries_yet"), self._container
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.hide()
        self._layout.addWidget(self._empty_label)

        self._layout.addStretch()

        self.setWidget(self._container)
        self.enableTransparentBackground()
        self.setScrollAnimation(Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint)

        self._cards: list[_ActivityEntryCard] = []

        # Debounce timer for live search
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(250)
        self._search_debounce.timeout.connect(
            lambda: self._on_search(self._search.text())
        )

    def set_managers(self, activity_log: ActivityLog | None = None, **kwargs) -> None:
        """Receive engine references from CdummWindow."""
        self._activity_log = activity_log
        self.refresh()

    def retranslate_ui(self) -> None:
        """Update text with current translations."""
        self._title.setText(tr("activity.title"))
        self._refresh_btn.setText(tr("activity.refresh"))
        self._search.setPlaceholderText(tr("activity.search_placeholder"))

    def refresh(self) -> None:
        """Reload activity log entries and rebuild cards."""
        self._clear_cards()

        if not self._activity_log:
            self._empty_label.setText(tr("activity.not_available"))
            self._empty_label.show()
            self._stats_label.setText("")
            return

        entries = self._activity_log.get_entries()
        if self._active_filter:
            entries = [e for e in entries if e.get("category") == self._active_filter]
        self._populate_cards(entries)

    def _on_search(self, query: str) -> None:
        """Search activity log entries."""
        if not self._activity_log or not query.strip():
            self.refresh()
            return
        self._clear_cards()
        entries = self._activity_log.search(query.strip())
        self._populate_cards(entries, search_query=query)

    def _on_text_changed(self, text: str) -> None:
        """Live search after 2+ characters (debounced), clear when empty."""
        if len(text.strip()) >= 2:
            self._search_debounce.start()
        elif not text.strip():
            self._search_debounce.stop()
            self.refresh()

    def _on_clear_search(self) -> None:
        """Clear search and show all entries."""
        self.refresh()

    def _on_filter_chip(self, category: str) -> None:
        """Toggle category filter."""
        if self._active_filter == category:
            self._active_filter = None
        else:
            self._active_filter = category

        # Update chip visual states
        for cat, btn in self._filter_buttons.items():
            color = CATEGORY_COLORS.get(cat, "#88C0D0")
            self._apply_chip_style(btn, color, active=(cat == self._active_filter))

        self.refresh()

    # Clean chip colors — monochrome blue-gray palette, not category colors
    _CHIP_LIGHT = (
        "PushButton { background: #F0F4F8; color: #4A5568; "
        "border: 1px solid #E2E8F0; border-radius: 15px; padding: 0 16px; padding-bottom: 6px; }"
        "PushButton:hover { background: #E2E8F0; }"
    )
    _CHIP_DARK = (
        "PushButton { background: #2D3748; color: #A0AEC0; "
        "border: 1px solid #4A5568; border-radius: 15px; padding: 0 16px; padding-bottom: 6px; }"
        "PushButton:hover { background: #3A4A5C; }"
    )
    _CHIP_ACTIVE_LIGHT = (
        "PushButton { background: #2878D0; color: white; "
        "border: none; border-radius: 15px; padding: 0 16px; padding-bottom: 6px; }"
        "PushButton:hover { background: #2060B0; }"
    )
    _CHIP_ACTIVE_DARK = (
        "PushButton { background: #3A8FE0; color: white; "
        "border: none; border-radius: 15px; padding: 0 16px; padding-bottom: 6px; }"
        "PushButton:hover { background: #2878D0; }"
    )

    @staticmethod
    def _apply_chip_style(btn: PushButton, color: str, active: bool) -> None:
        from qfluentwidgets import setCustomStyleSheet
        if active:
            setCustomStyleSheet(btn,
                ActivityPage._CHIP_ACTIVE_LIGHT, ActivityPage._CHIP_ACTIVE_DARK)
        else:
            setCustomStyleSheet(btn,
                ActivityPage._CHIP_LIGHT, ActivityPage._CHIP_DARK)

    def _populate_cards(self, entries: list[dict], search_query: str = "") -> None:
        """Build cards from a list of log entries."""
        if not entries:
            if search_query:
                self._empty_label.setText(tr("activity.no_results", query=search_query))
            else:
                self._empty_label.setText(tr("activity.no_entries_yet"))
            self._empty_label.show()
            self._stats_label.setText("")
            return

        self._empty_label.hide()
        if search_query:
            self._stats_label.setText(tr("activity.entries_matching", count=str(len(entries)), query=search_query))
        else:
            self._stats_label.setText(tr("activity.entries_count", count=str(len(entries))))

        for entry in entries:
            card = _ActivityEntryCard(entry, self._container)
            self._cards_layout.addWidget(card)
            self._cards.append(card)

        # Staggered entrance animation
        from cdumm.gui.components.card_animations import staggered_fade_in
        self._entrance_anim = staggered_fade_in(self._cards, stagger=20)

    def _clear_cards(self) -> None:
        """Remove all existing entry cards."""
        for card in self._cards:
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
