"""Fluent conflicts dialog — shows active pairs + load-order ranking.

Built on ``MessageBoxBase`` so it picks up the same Fluent chrome, mask,
shadow, and font that every other dialog in the app uses (preset picker,
mod contents, profile editor, etc.). Rendering a raw ``QDialog`` here
turns the window into an OS-default slab that looks nothing like the
rest of CDUMM — the user called it out correctly the first time it
shipped that way.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, InfoBadge, MessageBoxBase,
    SimpleCardWidget, SmoothScrollDelegate, SubtitleLabel, isDarkTheme,
)

from cdumm.gui.conflict_view import ConflictView
from cdumm.i18n import tr

if TYPE_CHECKING:
    from cdumm.engine.conflict_detector import Conflict


_RANK_PILL_QSS = (
    "QLabel {"
    "  background: rgba(40, 120, 208, 48);"
    "  color: #2878D0;"
    "  border-radius: 12px;"
    "  font-weight: 700;"
    "  padding: 0 6px;"
    "}"
)


class ConflictsDialog(MessageBoxBase):
    """Fluent modal listing every active conflict in load-order."""

    def __init__(self, conflicts: "list[Conflict]",
                 mods_by_id: dict[int, dict], parent=None) -> None:
        super().__init__(parent=parent)

        # ── Title row — subtitle label + attention badge for count ────
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = SubtitleLabel(tr("conflicts.title"))
        tf = title.font()
        tf.setPixelSize(22)
        tf.setWeight(QFont.Weight.Bold)
        title.setFont(tf)
        title_row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        if conflicts:
            badge = InfoBadge.attension(str(len(conflicts)), self.widget)
            badge.setFixedHeight(22)
            title_row.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        self.viewLayout.addLayout(title_row)

        # Explanatory caption
        caption_text = (
            tr("conflicts.empty_body") if not conflicts
            else tr("conflicts.hint"))
        caption = CaptionLabel(caption_text)
        cf = caption.font()
        cf.setPixelSize(13)
        caption.setFont(cf)
        caption.setWordWrap(True)
        self.viewLayout.addWidget(caption)
        self.viewLayout.addSpacing(4)

        if not conflicts:
            # Empty state — one line, centred, same card surface
            empty = BodyLabel(tr("conflicts.empty_title"))
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_font = empty.font()
            empty_font.setPixelSize(14)
            empty.setFont(empty_font)
            empty.setContentsMargins(0, 40, 0, 40)
            self.viewLayout.addWidget(empty)
        else:
            # Conflict tree (with smooth-scroll delegate for Fluent bars)
            self._tree_view = ConflictView(self.widget)
            self._tree_view.update_conflicts(conflicts)
            self.viewLayout.addWidget(self._tree_view, 1)

            # Load-order card
            self._build_load_order_card(conflicts, mods_by_id)

        # ── Footer buttons — reuse yes/cancel for Close/None ─────────
        self.yesButton.setText(
            self._close_label())
        self.cancelButton.hide()

        self.widget.setMinimumWidth(780)
        self.widget.setMinimumHeight(540 if conflicts else 220)

    # ------------------------------------------------------------------

    @staticmethod
    def _close_label() -> str:
        label = tr("main.close")
        return label if label != "main.close" else "Close"

    # ------------------------------------------------------------------

    def _build_load_order_card(self, conflicts: "list[Conflict]",
                               mods_by_id: dict[int, dict]) -> None:
        priority_mods: list[int] = []
        seen_ids: set[int] = set()
        for c in conflicts:
            for mid in (c.mod_a_id, c.mod_b_id):
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    priority_mods.append(mid)
        priority_mods.sort(
            key=lambda mid: mods_by_id.get(mid, {}).get("priority", 0),
            reverse=True)
        if not priority_mods:
            return

        card = SimpleCardWidget(self.widget)
        card.setBorderRadius(8)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 10)
        card_layout.setSpacing(6)

        header = BodyLabel(tr("conflicts.load_order"))
        hf = header.font()
        hf.setBold(True)
        header.setFont(hf)
        card_layout.addWidget(header)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 4, 0, 0)
        inner_layout.setSpacing(4)
        for i, mid in enumerate(priority_mods):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            rank = QLabel(f"#{i + 1}")
            rank.setAlignment(Qt.AlignmentFlag.AlignCenter)
            rank.setFixedHeight(24)
            rank.setFixedWidth(48)
            rank.setStyleSheet(_RANK_PILL_QSS)
            row.addWidget(rank, 0, Qt.AlignmentFlag.AlignVCenter)
            name = mods_by_id.get(mid, {}).get("name", f"(id {mid})")
            name_label = BodyLabel(name)
            row.addWidget(name_label, 1, Qt.AlignmentFlag.AlignVCenter)
            wrap = QWidget()
            wrap.setLayout(row)
            inner_layout.addWidget(wrap)
        inner_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMaximumHeight(150)
        # Fluent smooth-scroll treatment for the load-order list
        self._order_scroll_delegate = SmoothScrollDelegate(scroll, useAni=True)
        # Transparent inner so the SimpleCardWidget surface shows through
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }")
        card_layout.addWidget(scroll)
        self.viewLayout.addWidget(card)
