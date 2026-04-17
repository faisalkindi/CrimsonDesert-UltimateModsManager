"""Fluent conflicts dialog — two sections: actionable vs auto-resolved.

Most "conflicts" the detector reports are informational (different
byte ranges in the same archive, different PAMT directories, etc.) — they
compose cleanly regardless of mod order. Only ``byte_range`` + ``semantic``
conflicts actually care about load order.

Hiding that distinction behind a flat tree of 84 "1 issue(s)" rows was
misleading. This dialog splits them into two sections with count badges
so the user sees at a glance which conflicts need their attention.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSizeGrip, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, FluentIcon, IconInfoBadge, InfoBadge, InfoLevel,
    MessageBoxBase, SimpleCardWidget, SingleDirectionScrollArea, SubtitleLabel,
    getFont,
)

from cdumm.gui.conflict_view import ACTIONABLE_LEVELS, ConflictView
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
    """Fluent modal — split by actionable vs auto-resolved."""

    def __init__(self, conflicts: "list[Conflict]",
                 mods_by_id: dict[int, dict], parent=None) -> None:
        super().__init__(parent=parent)

        actionable = [c for c in conflicts if c.level in ACTIONABLE_LEVELS]
        auto = [c for c in conflicts if c.level not in ACTIONABLE_LEVELS]

        # ── Title + total count + caption ─────────────────────────────
        self._build_title(len(conflicts))
        self._build_caption(bool(actionable), bool(auto))
        self.viewLayout.addSpacing(6)

        if not conflicts:
            self._build_empty_state()
        else:
            # Section 1 — actionable (priority affects winner). Stretch
            # factor = row count (capped) so the two trees share vertical
            # space proportional to their content. Each tree scrolls
            # internally when rows exceed its allocated height — no outer
            # scroll area needed.
            if actionable:
                self._build_section(
                    tr("conflicts.section_actionable"),
                    len(actionable), actionable,
                    FluentIcon.INFO, InfoLevel.ATTENTION,
                    caption=tr("conflicts.section_actionable_desc"),
                    stretch=min(max(len(actionable), 2), 4))
            # Section 2 — auto-resolved (informational)
            if auto:
                self._build_section(
                    tr("conflicts.section_auto"),
                    len(auto), auto,
                    FluentIcon.ACCEPT, InfoLevel.SUCCESS,
                    caption=tr("conflicts.section_auto_desc"),
                    stretch=min(max(len(auto), 2), 12))
            # Load-order card — only shown when reordering actually matters
            if actionable:
                self._build_load_order_card(actionable, mods_by_id)

        # ── Footer: single Close button ───────────────────────────────
        self.yesButton.setText(self._close_label())
        self.cancelButton.hide()

        # Size so Close is always visible on open. The centerWidget is
        # placed with Qt.AlignCenter in the mask's hBoxLayout, so it
        # renders at its minimumSize. Cap that size to the parent window
        # minus a margin — a 780px minimum on a 720px-tall parent would
        # overflow the mask and clip the Close button at the bottom.
        desired_w = 1080
        desired_h = 920 if conflicts else 280
        floor_w = 720
        floor_h = 420 if conflicts else 200
        if parent is not None:
            desired_w = min(desired_w, max(floor_w, parent.width() - 60))
            desired_h = min(desired_h, max(floor_h, parent.height() - 60))
        self.widget.setMinimumWidth(desired_w)
        self.widget.setMinimumHeight(desired_h)

        # User can drag the bottom-right corner to enlarge the dialog.
        # Qt's QSizeGrip resizes the ``widget`` QFrame it's parented to,
        # and MaskDialogBase's hBoxLayout re-centres on every change.
        if conflicts:
            grip_row = QHBoxLayout()
            grip_row.setContentsMargins(0, 0, 0, 0)
            grip_row.addStretch(1)
            grip = QSizeGrip(self.widget)
            grip.setFixedSize(16, 16)
            grip_row.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom
                               | Qt.AlignmentFlag.AlignRight)
            self.viewLayout.addLayout(grip_row)

    # ------------------------------------------------------------------
    # Sub-builders
    # ------------------------------------------------------------------

    def _build_title(self, total: int) -> None:
        row = QHBoxLayout()
        row.setSpacing(10)
        title = SubtitleLabel(tr("conflicts.title"))
        tf = title.font()
        tf.setPixelSize(22)
        tf.setWeight(QFont.Weight.Bold)
        title.setFont(tf)
        row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        if total:
            badge = InfoBadge.attension(str(total), self.widget)
            badge.setFixedHeight(22)
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        self.viewLayout.addLayout(row)

    def _build_caption(self, has_actionable: bool, has_auto: bool) -> None:
        if has_actionable and has_auto:
            text = tr("conflicts.caption_mixed")
        elif has_actionable:
            text = tr("conflicts.caption_actionable_only")
        elif has_auto:
            text = tr("conflicts.caption_auto_only")
        else:
            text = tr("conflicts.empty_body")
        caption = CaptionLabel(text)
        cf = caption.font()
        cf.setPixelSize(13)
        caption.setFont(cf)
        caption.setWordWrap(True)
        self.viewLayout.addWidget(caption)

    def _build_empty_state(self) -> None:
        empty = BodyLabel(tr("conflicts.empty_title"))
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ef = empty.font()
        ef.setPixelSize(14)
        empty.setFont(ef)
        empty.setContentsMargins(0, 40, 0, 40)
        self.viewLayout.addWidget(empty)

    def _build_section(self, title_text: str, count: int,
                       items: "list[Conflict]",
                       icon: FluentIcon, level: InfoLevel,
                       caption: str, stretch: int) -> None:
        """One section with coloured badge header + explanation + tree."""
        self.viewLayout.addSpacing(12)

        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        badge = IconInfoBadge.make(icon, self.widget, level=level)
        badge.setFixedSize(20, 20)
        header_row.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        label = BodyLabel(title_text)
        lf = label.font()
        lf.setPixelSize(15)
        lf.setWeight(QFont.Weight.DemiBold)
        label.setFont(lf)
        header_row.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
        count_badge = InfoBadge.make(str(count), self.widget, level=level)
        count_badge.setFixedHeight(20)
        header_row.addWidget(count_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        header_row.addStretch(1)
        self.viewLayout.addLayout(header_row)

        # Small descriptive caption under the section header
        desc = CaptionLabel(caption)
        df = desc.font()
        df.setPixelSize(12)
        desc.setFont(df)
        desc.setWordWrap(True)
        desc.setContentsMargins(30, 0, 0, 6)
        self.viewLayout.addWidget(desc)

        tree = ConflictView(self.widget)
        # Collapsed pair rows by default — auto-expand doubles row count
        # (pair + child per conflict) and crowds the dialog. User can
        # click a pair to see its file-level children.
        tree.update_conflicts(items, auto_expand=False)
        # Minimum keeps at least ~2 rows always visible; stretch lets the
        # tree grow when the dialog is taller. The tree's own internal
        # scrollbar handles overflow — no outer scroll area needed.
        tree.setMinimumHeight(140)
        self.viewLayout.addWidget(tree, stretch)

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

        self.viewLayout.addSpacing(12)

        card = SimpleCardWidget(self.widget)
        card.setBorderRadius(8)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 10)
        card_layout.setSpacing(6)

        header = BodyLabel(tr("conflicts.load_order"))
        header.setFont(getFont(14, QFont.Weight.DemiBold))
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
            rank.setFont(getFont(12, QFont.Weight.Bold))
            row.addWidget(rank, 0, Qt.AlignmentFlag.AlignVCenter)
            name = mods_by_id.get(mid, {}).get("name", f"(id {mid})")
            name_label = BodyLabel(name)
            name_label.setFont(getFont(14))
            row.addWidget(name_label, 1, Qt.AlignmentFlag.AlignVCenter)
            wrap = QWidget()
            wrap.setLayout(row)
            inner_layout.addWidget(wrap)
        inner_layout.addStretch(1)

        # Use SingleDirectionScrollArea (not raw QScrollArea) so the
        # scrollbar matches SmoothScrollArea used by every other page
        # in the app — identical Fluent SmoothScrollBar widget.
        scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Vertical)
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(SingleDirectionScrollArea.Shape.NoFrame)
        scroll.setMaximumHeight(150)
        scroll.enableTransparentBackground()
        card_layout.addWidget(scroll)
        self.viewLayout.addWidget(card)

    # ------------------------------------------------------------------

    @staticmethod
    def _close_label() -> str:
        label = tr("main.close")
        return label if label != "main.close" else "Close"
