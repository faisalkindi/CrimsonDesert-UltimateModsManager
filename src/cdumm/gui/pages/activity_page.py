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

from cdumm.engine.activity_log import ActivityLog, CATEGORY_COLORS, category_color
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

        # Category badge — theme-aware color + stronger tint in dark mode.
        category = entry.get("category", "info")
        self._category = category
        dark = isDarkTheme()
        color = category_color(category, dark=dark)
        alpha = "33" if dark else "20"  # ~20% vs ~12.5% — darker UI needs more fill
        badge_text = tr(f"activity.cat_{category}") if category in CATEGORY_COLORS else category.upper()
        badge = CaptionLabel(badge_text, self)
        badge.setFixedWidth(80)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"background: {color}{alpha}; color: {color}; "
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

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            dark = isDarkTheme()
            text_color = "#9BA4B5" if dark else "#606A7B"
            self._ts_label.setTextColor(text_color, text_color)
            if self._detail_label:
                self._detail_label.setTextColor(text_color, text_color)
            # Re-pick badge color for the new theme — light and dark use different hues.
            c = category_color(self._category, dark=dark)
            alpha = "33" if dark else "20"
            self._badge.setStyleSheet(
                f"background: {c}{alpha}; color: {c}; "
                f"border-radius: 6px; padding: 4px 8px; font-weight: 700; font-size: 12px;"
            )

    def retranslate_badge(self) -> None:
        """Refresh the badge label after a language change."""
        if self._category in CATEGORY_COLORS:
            self._badge.setText(tr(f"activity.cat_{self._category}"))


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
            btn = PushButton(tr(f"activity.cat_{category}"), self._container)
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            bf = btn.font()
            bf.setPixelSize(11)
            bf.setWeight(QFont.Weight.DemiBold)
            btn.setFont(bf)
            self._apply_chip_style(btn, color, active=False, category=category)
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
        for cat, btn in self._filter_buttons.items():
            btn.setText(tr(f"activity.cat_{cat}"))
        # Also retranslate every visible entry badge
        for card in self._cards:
            if hasattr(card, "retranslate_badge"):
                card.retranslate_badge()

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
            self._apply_chip_style(btn, color, active=(cat == self._active_filter),
                                   category=cat)

        self.refresh()

    # Per-category chip colors: (light_text, dark_text, light_border, dark_border)
    _CHIP_CATEGORY_COLORS = {
        "apply":    ("#2E7D32", "#81C784", "#A5D6A7", "#2E7D32"),
        "revert":   ("#E65100", "#FFB74D", "#FFCC80", "#E65100"),
        "import":   ("#1565C0", "#64B5F6", "#90CAF9", "#1565C0"),
        "remove":   ("#C62828", "#EF5350", "#EF9A9A", "#C62828"),
        "verify":   ("#00796B", "#4DB6AC", "#80CBC4", "#00796B"),
        "error":    ("#C62828", "#EF5350", "#EF9A9A", "#C62828"),
    }
    # Fallback for categories not in the map above
    _CHIP_DEFAULT_COLORS = ("#4A5568", "#A0AEC0", "#E2E8F0", "#4A5568")

    @staticmethod
    def _apply_chip_style(btn: PushButton, color: str, active: bool,
                          category: str | None = None) -> None:
        from qfluentwidgets import setCustomStyleSheet
        if active:
            light = (
                f"PushButton {{ background: {color}; color: white; "
                "border: none; border-radius: 15px; padding: 0 16px; padding-bottom: 6px; }"
                f"PushButton:hover {{ background: {color}; opacity: 0.85; }}"
            )
            dark = (
                f"PushButton {{ background: {color}; color: white; "
                "border: none; border-radius: 15px; padding: 0 16px; padding-bottom: 6px; }"
                f"PushButton:hover {{ background: {color}; opacity: 0.85; }}"
            )
            setCustomStyleSheet(btn, light, dark)
        else:
            # Look up per-category color, fall back to grey. Prefer the explicit
            # English category key over the (now-localized) button text.
            cat_key = (category or btn.text()).lower()
            lt, dt, lb, db = ActivityPage._CHIP_CATEGORY_COLORS.get(
                cat_key, ActivityPage._CHIP_DEFAULT_COLORS)
            light = (
                f"PushButton {{ background: {lt}14; color: {lt}; "
                f"border: 1px solid {lb}; border-radius: 15px; padding: 0 16px; padding-bottom: 6px; }}"
                f"PushButton:hover {{ background: {lt}28; }}"
            )
            dark = (
                f"PushButton {{ background: {dt}14; color: {dt}; "
                f"border: 1px solid {db}; border-radius: 15px; padding: 0 16px; padding-bottom: 6px; }}"
                f"PushButton:hover {{ background: {dt}28; }}"
            )
            setCustomStyleSheet(btn, light, dark)

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
