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

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSizeGrip, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, FluentIcon, IconInfoBadge, InfoBadge, InfoLevel,
    MessageBoxBase, SimpleCardWidget, SingleDirectionScrollArea, SubtitleLabel,
    TransparentToolButton, getFont,
)

from cdumm.gui.conflict_view import ACTIONABLE_LEVELS, ConflictView
from cdumm.i18n import tr

if TYPE_CHECKING:
    from cdumm.engine.conflict_detector import Conflict, ConflictDetector
    from cdumm.engine.mod_manager import ModManager


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

    # Emitted whenever the user reorders priorities inside the dialog so
    # the parent (mods page) can refresh its cards after close.
    order_changed = Signal()

    def __init__(self, conflicts: "list[Conflict]",
                 mods_by_id: dict[int, dict], parent=None,
                 mod_manager: "ModManager | None" = None,
                 conflict_detector: "ConflictDetector | None" = None
                 ) -> None:
        super().__init__(parent=parent)

        self._mod_manager = mod_manager
        self._conflict_detector = conflict_detector
        self._mods_by_id = mods_by_id
        self._conflicts = conflicts

        actionable = [c for c in conflicts if c.level in ACTIONABLE_LEVELS]
        auto = [c for c in conflicts if c.level not in ACTIONABLE_LEVELS]

        # ── Title + total count + caption ─────────────────────────────
        self._build_title(len(conflicts))
        self._build_caption(bool(actionable), bool(auto))
        self.viewLayout.addSpacing(6)

        # Content container — wraps sections + load-order card so we can
        # rebuild in-place after the user reorders priority, without
        # disturbing the title, caption, grip, or Close button.
        self._content_host = QWidget()
        self._content_layout = QVBoxLayout(self._content_host)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        self.viewLayout.addWidget(self._content_host, 1)

        self._build_content()

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
    # Content lifecycle
    # ------------------------------------------------------------------

    def _build_content(self) -> None:
        """Populate the content host with sections + load-order card."""
        actionable = [c for c in self._conflicts if c.level in ACTIONABLE_LEVELS]
        auto = [c for c in self._conflicts if c.level not in ACTIONABLE_LEVELS]

        if not self._conflicts:
            self._build_empty_state()
            return

        if actionable:
            self._build_section(
                tr("conflicts.section_actionable"),
                len(actionable), actionable,
                FluentIcon.INFO, InfoLevel.ATTENTION,
                caption=tr("conflicts.section_actionable_desc"),
                stretch=min(max(len(actionable), 2), 4))
        if auto:
            self._build_section(
                tr("conflicts.section_auto"),
                len(auto), auto,
                FluentIcon.ACCEPT, InfoLevel.SUCCESS,
                caption=tr("conflicts.section_auto_desc"),
                stretch=min(max(len(auto), 2), 12))
        if actionable:
            self._build_load_order_card(actionable, self._mods_by_id)

    def _rebuild_content(self) -> None:
        """Refresh conflicts + mods from DB and rebuild the content host."""
        if self._conflict_detector is not None:
            try:
                self._conflicts = list(self._conflict_detector.detect_all())
            except Exception:
                pass
        if self._mod_manager is not None:
            try:
                self._mods_by_id = {
                    m["id"]: m for m in self._mod_manager.list_mods(mod_type="paz")
                }
            except Exception:
                pass

        # Clear the content host — takeAt(0) until empty, deleteLater each
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            else:
                sub = item.layout()
                if sub is not None:
                    while sub.count():
                        s = sub.takeAt(0)
                        if s and s.widget():
                            s.widget().deleteLater()

        self._build_content()
        self.order_changed.emit()

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
        self._content_layout.addWidget(empty)

    def _build_section(self, title_text: str, count: int,
                       items: "list[Conflict]",
                       icon: FluentIcon, level: InfoLevel,
                       caption: str, stretch: int) -> None:
        """One section with coloured badge header + explanation + tree."""
        self._content_layout.addSpacing(12)

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
        self._content_layout.addLayout(header_row)

        # Small descriptive caption under the section header
        desc = CaptionLabel(caption)
        df = desc.font()
        df.setPixelSize(12)
        desc.setFont(df)
        desc.setWordWrap(True)
        desc.setContentsMargins(30, 0, 0, 6)
        self._content_layout.addWidget(desc)

        tree = ConflictView(self.widget)
        # Collapsed pair rows by default — auto-expand doubles row count
        # (pair + child per conflict) and crowds the dialog. User can
        # click a pair to see its file-level children.
        tree.update_conflicts(items, auto_expand=False)
        # Minimum keeps at least ~2 rows always visible; stretch lets the
        # tree grow when the dialog is taller. The tree's own internal
        # scrollbar handles overflow — no outer scroll area needed.
        tree.setMinimumHeight(140)
        self._content_layout.addWidget(tree, stretch)

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

        self._content_layout.addSpacing(12)

        card = SimpleCardWidget(self.widget)
        card.setBorderRadius(8)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 10)
        card_layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        header = BodyLabel(tr("conflicts.load_order"))
        header.setFont(getFont(14, QFont.Weight.DemiBold))
        header_row.addWidget(header, 0, Qt.AlignmentFlag.AlignVCenter)
        hint = CaptionLabel(tr("conflicts.load_order_hint"))
        hint.setFont(getFont(11))
        header_row.addWidget(hint, 1, Qt.AlignmentFlag.AlignVCenter)
        card_layout.addLayout(header_row)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 4, 0, 0)
        inner_layout.setSpacing(4)
        last_idx = len(priority_mods) - 1
        can_reorder = self._mod_manager is not None
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

            # Up/down reorder buttons — disabled at the top/bottom edge
            # or when no mod_manager was supplied (read-only preview).
            if can_reorder:
                prev_mid = priority_mods[i - 1] if i > 0 else None
                next_mid = priority_mods[i + 1] if i < last_idx else None
                up_btn = TransparentToolButton(FluentIcon.UP)
                up_btn.setFixedSize(28, 28)
                up_btn.setEnabled(prev_mid is not None)
                up_btn.setToolTip(tr("conflicts.move_up"))
                if prev_mid is not None:
                    up_btn.clicked.connect(
                        lambda _=False, a=mid, b=prev_mid: self._swap(a, b))
                row.addWidget(up_btn, 0, Qt.AlignmentFlag.AlignVCenter)

                down_btn = TransparentToolButton(FluentIcon.DOWN)
                down_btn.setFixedSize(28, 28)
                down_btn.setEnabled(next_mid is not None)
                down_btn.setToolTip(tr("conflicts.move_down"))
                if next_mid is not None:
                    down_btn.clicked.connect(
                        lambda _=False, a=mid, b=next_mid: self._swap(a, b))
                row.addWidget(down_btn, 0, Qt.AlignmentFlag.AlignVCenter)

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
        scroll.setMaximumHeight(180)
        scroll.enableTransparentBackground()
        card_layout.addWidget(scroll)
        self._content_layout.addWidget(card)

    # ------------------------------------------------------------------

    def _swap(self, mod_a_id: int, mod_b_id: int) -> None:
        """Swap the priority values of two mods and rebuild content."""
        if self._mod_manager is None:
            return
        try:
            self._mod_manager._swap_priority(mod_a_id, mod_b_id)
        except Exception:
            return
        self._rebuild_content()

    # ------------------------------------------------------------------

    @staticmethod
    def _close_label() -> str:
        label = tr("main.close")
        return label if label != "main.close" else "Close"
