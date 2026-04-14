"""CDUMM v3 mod list components: StatusBadge, ModCard, FolderGroup, DragTargetIndicator."""

from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve, QMimeData
from PySide6.QtGui import QColor, QDrag, QFont, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CardWidget,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    StrongBodyLabel,
    isDarkTheme,
    setCustomStyleSheet,
)

from cdumm.i18n import tr


# ── Color tables ────────────────────────────────────────────────────────────

_STATUS_COLORS = {
    "installed": {
        "light": {"bg": "#E8F5E9", "text": "#2E7D32", "border": "#A5D6A7"},
        "dark":  {"bg": "#1A2E1A", "text": "#81C784", "border": "#2E5E2E"},
    },
    "active": {  # legacy alias
        "light": {"bg": "#E8F5E9", "text": "#2E7D32", "border": "#A5D6A7"},
        "dark":  {"bg": "#1A2E1A", "text": "#81C784", "border": "#2E5E2E"},
    },
    "loaded": {
        "light": {"bg": "#FFF3E0", "text": "#E65100", "border": "#FFCC80"},
        "dark":  {"bg": "#3E2A10", "text": "#FFB74D", "border": "#6E4A1A"},
    },
    "unloaded": {
        "light": {"bg": "#F5F5F5", "text": "#757575", "border": "#E0E0E0"},
        "dark":  {"bg": "#252830", "text": "#6B7280", "border": "#3A3E48"},
    },
    # Legacy aliases
    "not applied": {
        "light": {"bg": "#FFF3E0", "text": "#E65100", "border": "#FFCC80"},
        "dark":  {"bg": "#3E2A10", "text": "#FFB74D", "border": "#6E4A1A"},
    },
    "disabled": {
        "light": {"bg": "#F5F5F5", "text": "#757575", "border": "#E0E0E0"},
        "dark":  {"bg": "#252830", "text": "#6B7280", "border": "#3A3E48"},
    },
}

_VERSION_COLORS = {
    "light": {"bg": "#F3E8FF", "text": "#6A1B9A", "border": "#CE93D8"},
    "dark":  {"bg": "#2A1F3D", "text": "#CE93D8", "border": "#4A2D6E"},
}

_FILES_COLORS = {
    "light": {"bg": "#E8EAF6", "text": "#283593", "border": "#9FA8DA"},
    "dark":  {"bg": "#1A2040", "text": "#9FA8DA", "border": "#2D3A6E"},
}

_CARD_COLORS = {
    "light": {"bg": "#FFFFFF", "border": "#E5E7EB", "hover": "#F9FAFB"},
    "dark":  {"bg": "#1C2028", "border": "#2D3340", "hover": "#242A34"},
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _theme_key() -> str:
    return "dark" if isDarkTheme() else "light"


def _pill_qss(colors: dict) -> str:
    """Build pill-badge stylesheet from a {bg, text, border} dict."""
    return (
        f"background: {colors['bg']};"
        f"color: {colors['text']};"
        f"border: 1px solid {colors['border']};"
        "border-radius: 11px;"
        "padding: 4px 14px;"
        "font-size: 12px;"
        "font-weight: 600;"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. StatusBadge
# ═══════════════════════════════════════════════════════════════════════════

class StatusBadge(QLabel):
    """Colored pill badge showing mod status (active / not applied / disabled)."""

    def __init__(self, status: str, parent=None):
        super().__init__(parent)
        self._status = status.lower()
        self.setFixedHeight(26)
        self.setText(tr(f"status.{self._status}"))
        self._apply_style()
        self._fade_anim = None

    # ── public ──────────────────────────────────────────────────────────

    def set_status(self, status: str, animate: bool = True) -> None:
        new_status = status.lower()
        if new_status == self._status:
            return
        if animate:
            self._fade_to_status(new_status)
        else:
            self._status = new_status
            self.setText(tr(f"status.{self._status}"))
            self._apply_style()

    def _fade_to_status(self, new_status: str) -> None:
        """Fade out, swap text/color, fade in."""
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(1.0)
        self.setGraphicsEffect(effect)

        # Fade out
        fade_out = QPropertyAnimation(effect, b"opacity")
        fade_out.setDuration(120)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)
        fade_out.setEasingCurve(QEasingCurve.Type.InCubic)

        def _swap():
            self._status = new_status
            self.setText(tr(f"status.{self._status}"))
            self._apply_style()
            # Fade in
            fade_in = QPropertyAnimation(effect, b"opacity")
            fade_in.setDuration(150)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
            fade_in.finished.connect(lambda: self.setGraphicsEffect(None))
            self._fade_anim = fade_in
            fade_in.start()

        fade_out.finished.connect(_swap)
        self._fade_anim = fade_out
        fade_out.start()

    # ── internal ────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        key = _theme_key()
        colors = _STATUS_COLORS.get(self._status, _STATUS_COLORS["disabled"])
        self.setStyleSheet(_pill_qss(colors[key]))

    def changeEvent(self, event):  # noqa: N802
        """Re-apply colors when the system theme changes."""
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_style()


# ═══════════════════════════════════════════════════════════════════════════
# 2. ModCard
# ═══════════════════════════════════════════════════════════════════════════

class ModCard(CardWidget):
    """Card representing a single mod in the mod list.

    Uses fixed column widths so that all cards align perfectly:
      Col 0: Checkbox  (24px)
      Col 1: Order     (30px)
      Col 2: Name+Author (stretch — absorbs remaining space)
      Col 3: Status    (85px)
      Col 4: Version   (55px)
      Col 5: Gear icon (28px, always present for alignment)
    """

    toggled = Signal(int, bool)
    config_clicked = Signal(int)
    context_menu_requested = Signal(int, object)  # mod_id, QPoint (global pos)
    card_clicked = Signal(int, object)  # mod_id, QMouseEvent (for Ctrl/Shift detection)
    renamed = Signal(int, str)  # mod_id, new_name

    def __init__(
        self,
        mod_id: int,
        order: int,
        name: str,
        author: str,
        version: str,
        status: str,
        file_count: int,
        has_config: bool = False,
        has_notes: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._mod_id = mod_id
        self._has_config = has_config
        self._has_notes = has_notes
        self._selected = False

        self._apply_flat_style()

        # ── Root layout ─────────────────────────────────────────────────
        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(0)

        # Col 0: Checkbox (fixed)
        self._checkbox = QCheckBox()
        self._checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._checkbox.setFixedWidth(26)
        self._checkbox.setChecked(status not in ("unloaded", "disabled"))
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

        # Col 2: Name + Author (stretch — takes all remaining space)
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)

        # Name row: name label + inline rename edit + gear icon
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self._name_label = StrongBodyLabel(name)
        name_row.addWidget(self._name_label)
        # Inline rename editor (hidden by default)
        self._name_edit = QLineEdit(name)
        self._name_edit.setVisible(False)
        self._name_edit.returnPressed.connect(self._finish_rename)
        self._name_edit.editingFinished.connect(self._finish_rename)
        name_row.addWidget(self._name_edit)
        # Gear icon next to name (only visible if configurable)
        self._gear = IconWidget(FluentIcon.SETTING, self)
        self._gear.setFixedSize(20, 20)
        if has_config:
            self._gear.setCursor(Qt.CursorShape.PointingHandCursor)
            self._gear.mousePressEvent = self._on_gear_clicked
        else:
            self._gear.setVisible(False)
        name_row.addWidget(self._gear)
        # Note icon (visible if mod has notes, clickable to view)
        self._note_icon = IconWidget(FluentIcon.DOCUMENT, self)
        self._note_icon.setFixedSize(20, 20)
        self._note_icon.setVisible(has_notes)
        self._note_icon.setCursor(Qt.CursorShape.PointingHandCursor)
        self._note_icon.mousePressEvent = self._on_note_clicked
        self._note_text = ""
        name_row.addWidget(self._note_icon)
        name_row.addStretch()
        info.addLayout(name_row)

        self._author_label = CaptionLabel(f"by {author}")
        info.addWidget(self._author_label)
        root.addLayout(info, 1)
        root.addSpacing(12)

        # Col 3: Status badge (fixed width)
        self._status_badge = StatusBadge(status)
        self._status_badge.setMinimumWidth(95)
        self._status_badge.setMaximumWidth(95)
        self._status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_badge)
        root.addSpacing(10)

        # Col 4: Version pill (fixed width)
        self._version_pill = QLabel(version)
        self._version_pill.setFixedHeight(26)
        self._version_pill.setMinimumWidth(60)
        self._version_pill.setMaximumWidth(60)
        self._version_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_version_style()
        root.addWidget(self._version_pill)

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def mod_id(self) -> int:
        return self._mod_id

    def set_status(self, status: str) -> None:
        self._status_badge.set_status(status)

    def set_has_notes(self, has_notes: bool, note_text: str = "") -> None:
        self._note_icon.setVisible(has_notes)
        self._note_text = note_text

    def set_note_text(self, text: str) -> None:
        self._note_text = text
        self._note_icon.setVisible(bool(text))

    def _on_note_clicked(self, _event) -> None:
        if not self._note_text:
            return
        from qfluentwidgets import Flyout, FlyoutAnimationType
        Flyout.create(
            title="Note",
            content=self._note_text,
            target=self._note_icon,
            parent=self.window(),
            isClosable=True,
            aniType=FlyoutAnimationType.PULL_UP,
        )

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

    def start_rename(self) -> None:
        """Switch to inline edit mode for the name."""
        self._name_edit.setText(self._name_label.text())
        self._name_label.setVisible(False)
        self._name_edit.setVisible(True)
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _finish_rename(self) -> None:
        """Commit the inline rename and switch back to label."""
        if not self._name_edit.isVisible():
            return
        new_name = self._name_edit.text().strip()
        old_name = self._name_label.text()
        self._name_edit.setVisible(False)
        self._name_label.setVisible(True)
        if new_name and new_name != old_name:
            self._name_label.setText(new_name)
            self.renamed.emit(self._mod_id, new_name)

    def set_checked(self, checked: bool) -> None:
        self._checkbox.blockSignals(True)
        self._checkbox.setChecked(checked)
        self._checkbox.blockSignals(False)

    # ── Hover ───────────────────────────────────────────────────────────

    def enterEvent(self, event):  # noqa: N802
        # Let CardWidget's built-in BackgroundAnimationWidget handle smooth
        # hover transitions. Only override stylesheet for selected state.
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        super().leaveEvent(event)
        if self._selected:
            self._apply_selected_style()

    # ── Theme change ────────────────────────────────────────────────────

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_flat_style()
            self._apply_checkbox_style()
            self._apply_order_style()
            self._apply_version_style()
            self._status_badge._apply_style()

    # ── Internal ────────────────────────────────────────────────────────

    def _apply_checkbox_style(self) -> None:
        dark = isDarkTheme()
        checked_color = "#5CB8F0" if dark else "#2878D0"
        unchecked_border = "#5A6270" if dark else "#9CA3AF"
        self._checkbox.setStyleSheet(
            "QCheckBox::indicator { width: 18px; height: 18px; }"
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
        """Override BackgroundAnimationWidget to show selection color."""
        if getattr(self, '_selected', False):
            return QColor("#1A2A3E") if isDarkTheme() else QColor("#D4E8FC")
        return super()._normalBackgroundColor()

    def _hoverBackgroundColor(self):
        if getattr(self, '_selected', False):
            return QColor("#1E3048") if isDarkTheme() else QColor("#C5DEFA")
        return super()._hoverBackgroundColor()

    def _apply_version_style(self) -> None:
        key = _theme_key()
        self._version_pill.setStyleSheet(_pill_qss(_VERSION_COLORS[key]))

    def _on_toggled(self, checked: bool) -> None:
        self.toggled.emit(self._mod_id, checked)

    def _on_gear_clicked(self, _event) -> None:
        self.config_clicked.emit(self._mod_id)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
            self._drag_started = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and not getattr(self, '_drag_started', False):
            self.card_clicked.emit(self._mod_id, event)


    def mouseMoveEvent(self, event):  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if not hasattr(self, '_drag_start_pos'):
            return
        distance = (event.pos() - self._drag_start_pos).manhattanLength()
        if distance < 20:
            return

        self._drag_started = True
        drag = QDrag(self)
        mime = QMimeData()

        # If this card is selected, drag ALL selected cards
        drag_ids = [self._mod_id]
        if self._selected:
            # Walk up to find the page's selected ids
            p = self.parent()
            while p:
                if hasattr(p, '_get_selected_mod_ids'):
                    sel = p._get_selected_mod_ids()
                    if sel and self._mod_id in sel:
                        drag_ids = sel
                    break
                p = p.parent()

        mime.setData("application/x-cdumm-mod-ids", ",".join(str(i) for i in drag_ids).encode())
        mime.setData("application/x-cdumm-mod-id", str(self._mod_id).encode())
        drag.setMimeData(mime)

        # Render a pixmap of this card as drag preview
        pixmap = QPixmap(self.size())
        pixmap.fill(Qt.GlobalColor.transparent)
        self.render(pixmap)
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.pos())

        drag.exec(Qt.DropAction.MoveAction)

    def contextMenuEvent(self, event):  # noqa: N802
        self.context_menu_requested.emit(self._mod_id, event.globalPos())


# ═══════════════════════════════════════════════════════════════════════════
# 3. DragTargetIndicator
# ═══════════════════════════════════════════════════════════════════════════

class DragTargetIndicator(QLabel):
    """Thin blue line shown during drag-reorder to indicate drop position."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(3)
        self.setVisible(False)
        self._apply_style()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "background: #2878D0; border-radius: 1px; margin: 0 8px;"
        )

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_style()


# ═══════════════════════════════════════════════════════════════════════════
# 4. FolderGroup
# ═══════════════════════════════════════════════════════════════════════════

class FolderGroup(QWidget):
    """Collapsible group that contains ModCards with drag-reorder support.

    Signals
    -------
    order_changed(list)
        Emitted after a drag-reorder with the new list of mod_ids.
    header_context_menu(str, QPoint)
        Emitted on right-click of the group header (group_name, global_pos).
    mod_moved_to_group(int, object)
        Emitted when a mod card is moved into this group (mod_id, group_id).
    """

    order_changed = Signal(list)
    header_context_menu = Signal(str, object)  # group_name, QPoint
    select_all_in_group = Signal(object)  # group_id
    mod_moved_to_group = Signal(int, object)  # mod_id, target_group_id

    def __init__(self, name: str, group_id: int | None = None, parent=None):
        super().__init__(parent)
        self._expanded = True
        self._group_name = name
        self._group_id = group_id

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Accept drops on header (for dragging cards onto group name)
        self.setAcceptDrops(True)

        # ── Header row ──────────────────────────────────────────────────
        self._header = QWidget()
        self._header.setCursor(self._header.cursor())
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(10)

        self._arrow = QLabel("▾")
        self._apply_arrow_style()
        self._arrow.setFixedWidth(28)
        header_layout.addWidget(self._arrow)

        self._name_label = StrongBodyLabel(name)
        nf = self._name_label.font()
        nf.setPixelSize(15)
        nf.setWeight(QFont.Weight.DemiBold)
        self._name_label.setFont(nf)
        header_layout.addWidget(self._name_label)

        self._count_label = CaptionLabel("(0)")
        cf = self._count_label.font()
        cf.setPixelSize(13)
        self._count_label.setFont(cf)
        header_layout.addWidget(self._count_label)

        # Select all button for this group
        self._select_all_btn = CaptionLabel("Select All")
        self._select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_all_btn.mousePressEvent = lambda _e: self._on_select_all_clicked()
        self._apply_select_all_style()
        header_layout.addWidget(self._select_all_btn)

        header_layout.addStretch()

        self._header.mousePressEvent = self._on_header_click
        root.addWidget(self._header)

        # ── Content container (accepts drops) ───────────────────────────
        self._content = _DroppableContainer(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(8, 0, 0, 0)
        self._content_layout.setSpacing(4)

        # Drag indicator
        self._drag_indicator = DragTargetIndicator(self._content)
        self._content_layout.addWidget(self._drag_indicator)
        self._drag_indicator.setVisible(False)

        root.addWidget(self._content)

        self._animation: QPropertyAnimation | None = None

    # ── Styling ─────────────────────────────────────────────────────────

    def _apply_arrow_style(self) -> None:
        color = "#9CA3AF" if isDarkTheme() else "#4B5563"
        self._arrow.setStyleSheet(
            f"font-size: 24px; background: transparent; border: none; color: {color};"
        )

    def _apply_select_all_style(self) -> None:
        from qfluentwidgets import setCustomStyleSheet
        setCustomStyleSheet(self._select_all_btn,
            "CaptionLabel { color: #2878D0; }",
            "CaptionLabel { color: #5CB8F0; }")
        sf = self._select_all_btn.font()
        sf.setPixelSize(12)
        self._select_all_btn.setFont(sf)

    def _on_select_all_clicked(self) -> None:
        self.select_all_in_group.emit(self._group_id)

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_arrow_style()
            self._apply_select_all_style()

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def group_id(self) -> int | None:
        return self._group_id

    @property
    def group_name(self) -> str:
        return self._group_name

    def add_mod_card(self, card: ModCard) -> None:
        self._content_layout.addWidget(card)

    def set_count(self, n: int) -> None:
        self._count_label.setText(f"({n})")

    def is_expanded(self) -> bool:
        return self._expanded

    def get_mod_ids(self) -> list[int]:
        """Return the current mod_id order from the layout."""
        ids = []
        for i in range(self._content_layout.count()):
            widget = self._content_layout.itemAt(i).widget()
            if isinstance(widget, ModCard):
                ids.append(widget.mod_id)
        return ids

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._arrow.setText("▾" if self._expanded else "▸")

        # Animate the content container's maximumHeight
        target_height = self._content.sizeHint().height() if self._expanded else 0

        if self._animation and self._animation.state() == QPropertyAnimation.State.Running:
            self._animation.stop()

        self._animation = QPropertyAnimation(self._content, b"maximumHeight")
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.setStartValue(self._content.maximumHeight())
        self._animation.setEndValue(target_height)

        if self._expanded:
            # After expanding, remove height constraint so new cards show
            self._animation.finished.connect(
                lambda: self._content.setMaximumHeight(16777215),
                Qt.ConnectionType.UniqueConnection,
            )

        self._animation.start()

    # ── Drag-reorder helpers (called by _DroppableContainer) ────────────

    def _find_drop_index(self, pos_y: int) -> int:
        """Find the layout index to insert at based on vertical position."""
        for i in range(self._content_layout.count()):
            widget = self._content_layout.itemAt(i).widget()
            if widget is None or isinstance(widget, DragTargetIndicator):
                continue
            widget_mid = widget.y() + widget.height() // 2
            if pos_y < widget_mid:
                return i
        return self._content_layout.count()

    def _show_indicator(self, index: int) -> None:
        """Move the drag indicator to the given layout index."""
        # Remove from current position
        self._content_layout.removeWidget(self._drag_indicator)
        # Insert at new position
        self._content_layout.insertWidget(index, self._drag_indicator)
        self._drag_indicator.setVisible(True)

    def _hide_indicator(self) -> None:
        self._drag_indicator.setVisible(False)

    def _find_card_anywhere(self, mod_id: int) -> ModCard | None:
        """Find a ModCard by mod_id in this group or any sibling group."""
        # Check this group first
        for i in range(self._content_layout.count()):
            w = self._content_layout.itemAt(i).widget()
            if isinstance(w, ModCard) and w.mod_id == mod_id:
                return w
        # Check sibling groups
        parent = self.parent()
        if parent:
            for child in parent.findChildren(FolderGroup):
                if child is self:
                    continue
                for i in range(child._content_layout.count()):
                    w = child._content_layout.itemAt(i).widget()
                    if isinstance(w, ModCard) and w.mod_id == mod_id:
                        return w
        return None

    def _handle_drop_batch(self, mod_ids: list[int]) -> None:
        """Move one or more cards to the indicator position. Single atomic operation."""
        indicator_index = self._content_layout.indexOf(self._drag_indicator)
        self._drag_indicator.setVisible(False)

        # Collect all cards and remove them from their current groups
        cards: list[ModCard] = []
        source_groups: set[FolderGroup] = set()
        for mid in mod_ids:
            card = self._find_card_anywhere(mid)
            if card is None:
                continue
            # Find which group owns this card and remove it
            owner = card.parent()
            while owner and not isinstance(owner, FolderGroup):
                owner = owner.parent()
            if owner and isinstance(owner, FolderGroup):
                owner._content_layout.removeWidget(card)
                source_groups.add(owner)
            cards.append(card)

        if not cards:
            return

        # Insert all cards at the indicator position (in order)
        insert_at = min(indicator_index, self._content_layout.count())
        for i, card in enumerate(cards):
            self._content_layout.insertWidget(insert_at + i, card)

        # Update counts on all affected groups
        for fg in source_groups | {self}:
            fg.set_count(len(fg.get_mod_ids()))

        # Emit signals only for mods that were actually moved
        self.order_changed.emit(self.get_mod_ids())
        for card in cards:
            self.mod_moved_to_group.emit(card.mod_id, self._group_id)

    # ── Header events ───────────────────────────────────────────────────

    def _on_header_click(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.header_context_menu.emit(self._group_name, event.globalPosition().toPoint())
        else:
            self.toggle()

    # ── Drag-drop onto group header ────────────────────────────────────

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        """Accept drops on the group header — appends cards to end of group."""
        if not event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            return
        if event.mimeData().hasFormat("application/x-cdumm-mod-ids"):
            ids_bytes = event.mimeData().data("application/x-cdumm-mod-ids").data()
            mod_ids = [int(x) for x in ids_bytes.decode().split(",") if x]
        else:
            mod_id_bytes = event.mimeData().data("application/x-cdumm-mod-id").data()
            mod_ids = [int(mod_id_bytes.decode())]
        # Show indicator at the end, then do the batch drop
        self._show_indicator(self._content_layout.count())
        self._handle_drop_batch(mod_ids)
        event.acceptProposedAction()

    def contextMenuEvent(self, event):  # noqa: N802
        # Only emit for header area
        header_rect = self._header.geometry()
        if header_rect.contains(event.pos()):
            self.header_context_menu.emit(self._group_name, event.globalPos())
        else:
            super().contextMenuEvent(event)


class _DroppableContainer(QWidget):
    """Internal widget that accepts drag-drop of ModCards for reorder."""

    def __init__(self, folder_group: FolderGroup):
        super().__init__(folder_group)
        self._group = folder_group
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            event.acceptProposedAction()
            drop_index = self._group._find_drop_index(event.position().y())
            self._group._show_indicator(drop_index)

    def dragLeaveEvent(self, event):  # noqa: N802
        self._group._hide_indicator()

    def dropEvent(self, event):  # noqa: N802
        if not event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            return
        # Check for multi-id drop
        if event.mimeData().hasFormat("application/x-cdumm-mod-ids"):
            ids_bytes = event.mimeData().data("application/x-cdumm-mod-ids").data()
            mod_ids = [int(x) for x in ids_bytes.decode().split(",") if x]
        else:
            mod_id_bytes = event.mimeData().data("application/x-cdumm-mod-id").data()
            mod_ids = [int(mod_id_bytes.decode())]
        self._group._handle_drop_batch(mod_ids)
        event.acceptProposedAction()
