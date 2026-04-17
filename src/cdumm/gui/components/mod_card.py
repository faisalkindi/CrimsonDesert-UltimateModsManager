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


def _is_draggable_card(widget) -> bool:
    """Check if a widget is a draggable card (ModCard or AsiCard)."""
    return isinstance(widget, ModCard) or (hasattr(widget, 'mod_id') and hasattr(widget, 'plugin_name'))


# ── Color tables ────────────────────────────────────────────────────────────

_STATUS_COLORS = {
    "active": {
        "light": {"bg": "#E8F5E9", "text": "#2E7D32", "border": "#A5D6A7"},
        "dark":  {"bg": "#1A2E1A", "text": "#81C784", "border": "#2E5E2E"},
    },
    "inactive": {
        "light": {"bg": "#F5F5F5", "text": "#757575", "border": "#E0E0E0"},
        "dark":  {"bg": "#252830", "text": "#6B7280", "border": "#3A3E48"},
    },
    # Legacy aliases
    "installed": {
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
    "not applied": {
        "light": {"bg": "#FFF3E0", "text": "#E65100", "border": "#FFCC80"},
        "dark":  {"bg": "#3E2A10", "text": "#FFB74D", "border": "#6E4A1A"},
    },
    "disabled": {
        "light": {"bg": "#F5F5F5", "text": "#757575", "border": "#E0E0E0"},
        "dark":  {"bg": "#252830", "text": "#6B7280", "border": "#3A3E48"},
    },
}

_PENDING_COLORS = {
    "apply to activate": {
        "light": {"bg": "#FFF3E0", "text": "#E65100", "border": "#FFCC80"},
        "dark":  {"bg": "#3E2A10", "text": "#FFB74D", "border": "#6E4A1A"},
    },
    "apply to deactivate": {
        "light": {"bg": "#FFEBEE", "text": "#C62828", "border": "#EF9A9A"},
        "dark":  {"bg": "#2E1A1A", "text": "#EF5350", "border": "#5E2E2E"},
    },
}

_VERSION_COLORS = {
    "light": {"bg": "#F3F4F6", "text": "#6B7280", "border": "#D1D5DB"},
    "dark":  {"bg": "#1F2937", "text": "#9CA3AF", "border": "#374151"},
}

_FILES_COLORS = {
    "light": {"bg": "#E8EAF6", "text": "#283593", "border": "#9FA8DA"},
    "dark":  {"bg": "#1A2040", "text": "#9FA8DA", "border": "#2D3A6E"},
}

_NEW_COLORS = {
    "light": {"bg": "#BBDEFB", "text": "#0D47A1", "border": "#64B5F6"},
    "dark":  {"bg": "#1A2744", "text": "#64B5F6", "border": "#1E3A5F"},
}

_CARD_COLORS = {
    "light": {"bg": "#FFFFFF", "border": "#E5E7EB", "hover": "#F9FAFB"},
    "dark":  {"bg": "#1C2028", "border": "#3A4250", "hover": "#2D3340"},
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
        is_new: bool = False,
        enabled: bool = True,
        target_language: str | None = None,
        conflict_mode: str = "normal",
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
        self._checkbox.setChecked(enabled)
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
        # Wrap long names across two lines instead of clipping. The card's
        # QVBoxLayout will grow vertically to fit.
        self._name_label.setWordWrap(True)
        self._name_label.setMinimumWidth(1)
        name_row.addWidget(self._name_label, 1)
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

        self._author_label = CaptionLabel(f"by {author}" if author else "")
        if not author:
            self._author_label.hide()
        info.addWidget(self._author_label)
        root.addLayout(info, 1)
        root.addSpacing(12)

        # Language badge (shown for language mods)
        if target_language:
            lang_badge = QLabel(target_language.upper())
            lang_badge.setFixedHeight(26)
            lang_badge.setMinimumWidth(45)
            lang_badge.setMaximumWidth(55)
            lang_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _lang_colors = {"bg": "#6366F1", "fg": "#FFFFFF"} if isDarkTheme() else {"bg": "#818CF8", "fg": "#FFFFFF"}
            lang_badge.setStyleSheet(_pill_qss(_lang_colors))
            root.addWidget(lang_badge)
            root.addSpacing(6)

        # Override badge (shown for mods with conflict_mode: override)
        if conflict_mode == "override":
            ovr_badge = QLabel(tr("mod_card.override"))
            ovr_badge.setFixedHeight(26)
            ovr_badge.setMinimumWidth(75)
            ovr_badge.setMaximumWidth(75)
            ovr_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _ovr_colors = {"bg": "#DC2626", "fg": "#FFFFFF"} if isDarkTheme() else {"bg": "#EF4444", "fg": "#FFFFFF"}
            ovr_badge.setStyleSheet(_pill_qss(_ovr_colors))
            root.addWidget(ovr_badge)
            root.addSpacing(6)

        # "NEW" bubble (shown for recently imported mods, before status)
        if is_new:
            new_badge = QLabel(tr("mod_card.new"))
            new_badge.setFixedHeight(26)
            new_badge.setMinimumWidth(55)
            new_badge.setMaximumWidth(55)
            new_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            new_badge.setStyleSheet(_pill_qss(_NEW_COLORS[_theme_key()]))
            root.addWidget(new_badge)
            root.addSpacing(6)

        # Col 3: Status badge — game state (fixed width)
        self._status_badge = StatusBadge(status)
        self._status_badge.setMinimumWidth(105)
        self._status_badge.setMaximumWidth(105)
        self._status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_badge)
        root.addSpacing(4)

        # Col 3b: Pending action badge (hidden when no pending change)
        self._pending_badge = QLabel("")
        self._pending_badge.setFixedHeight(26)
        self._pending_badge.setMinimumWidth(140)
        self._pending_badge.setMaximumWidth(140)
        self._pending_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pending_badge.setVisible(False)
        root.addWidget(self._pending_badge)
        root.addSpacing(6)

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

    # Map English color-lookup keys to translation keys for the badge text.
    _PENDING_TR_KEYS = {
        "apply to activate":   "mod_list.pending_activate",
        "apply to deactivate": "mod_list.pending_deactivate",
    }

    def set_pending(self, pending: str | None) -> None:
        """Set the pending action badge. None/empty hides it.

        Accepts the English color-lookup key (e.g. "apply to activate") for
        colour selection and translates the display text via i18n. Passing any
        other string falls back to showing it verbatim with the default colour.
        """
        if not pending:
            self._pending_badge.setVisible(False)
            self._pending_badge.setText("")
            # Remember the key so retranslation can restore the text
            self._pending_key = None
            return
        lookup = pending.lower()
        self._pending_key = lookup
        colors = _PENDING_COLORS.get(lookup, _PENDING_COLORS.get("apply needed"))
        tr_key = ModCard._PENDING_TR_KEYS.get(lookup)
        from cdumm.i18n import tr as _tr
        display = _tr(tr_key) if tr_key else pending
        self._pending_badge.setText(display)
        self._pending_badge.setStyleSheet(_pill_qss(colors[_theme_key()]))
        self._pending_badge.setVisible(True)

    def retranslate_pending(self) -> None:
        """Re-render the pending badge in the current language."""
        if getattr(self, "_pending_key", None):
            self.set_pending(self._pending_key)

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
        from cdumm.i18n import tr as _tr
        Flyout.create(
            title=_tr("flyout.note"),
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

    def set_update_available(self, has_update: bool, nexus_url: str = "") -> None:
        """Color the version pill green (up to date) or red (update available)."""
        self._nexus_url = nexus_url
        if has_update:
            c = {"bg": "#FEE2E2", "text": "#DC2626", "border": "#FCA5A5"} if _theme_key() == "light" else {"bg": "#3B1111", "text": "#FCA5A5", "border": "#7F1D1D"}
            self._version_pill.setCursor(Qt.CursorShape.PointingHandCursor)
            from cdumm.i18n import tr as _tr
            self._version_pill.setToolTip(_tr("tooltip.update_available"))
            self._version_pill.mousePressEvent = self._on_version_clicked
        else:
            c = {"bg": "#DCFCE7", "text": "#16A34A", "border": "#86EFAC"} if _theme_key() == "light" else {"bg": "#0A2E14", "text": "#86EFAC", "border": "#166534"}
            from cdumm.i18n import tr as _tr
            self._version_pill.setToolTip(_tr("tooltip.up_to_date"))
        self._version_pill.setStyleSheet(_pill_qss(c))

    def _on_version_clicked(self, event):
        url = getattr(self, "_nexus_url", "")
        if url:
            import webbrowser
            webbrowser.open(url)

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

        # Clear any entrance-animation opacity effect before rendering
        # so the drag preview is crisp, not hazy
        if self.graphicsEffect() is not None:
            self.setGraphicsEffect(None)

        # Render a pixmap of this card as drag preview
        pixmap = QPixmap(self.size())
        bg = QColor(_CARD_COLORS[_theme_key()]["bg"])
        pixmap.fill(bg)
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
    folder_dropped = Signal(int, int)  # dragged_group_id, target_group_id

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

        # Select all button for this group (hidden — top-level Select All is sufficient)
        self._select_all_btn = CaptionLabel(tr("mod_card.select_all"))
        self._select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_all_btn.mousePressEvent = lambda _e: self._on_select_all_clicked()
        self._apply_select_all_style()
        self._select_all_btn.setVisible(False)
        header_layout.addWidget(self._select_all_btn)

        header_layout.addStretch()

        self._header.mousePressEvent = self._on_header_click
        self._header.mouseMoveEvent = self._on_header_move
        root.addWidget(self._header)

        # ── Content container (accepts drops) ───────────────────────────
        self._content = _DroppableContainer(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(8, 0, 0, 0)
        self._content_layout.setSpacing(4)

        # Drag indicator — positioned absolutely over content, NOT in layout
        self._drag_indicator = DragTargetIndicator(self._content)
        self._drag_indicator.setVisible(False)
        self._drag_indicator.raise_()  # always on top
        self._drop_slot = -1  # logical slot index for drop

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

    def add_mod_card(self, card) -> None:
        self._content_layout.addWidget(card)

    def set_count(self, n: int) -> None:
        self._count_label.setText(f"({n})")

    def is_expanded(self) -> bool:
        return self._expanded

    def get_mod_ids(self) -> list[int]:
        """Return the current mod_id order from the layout.

        Guards against Qt's ``itemAt(i)`` returning ``None`` while the
        layout is in the middle of a reparent / re-add (seen during rapid
        drag-drop reorders of many cards at once).
        """
        ids = []
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if _is_draggable_card(widget):
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

    def _iter_cards(self):
        """Yield every draggable card in the content layout, safely.

        Null-guards ``itemAt(i)`` because Qt can return ``None`` briefly
        while the layout is being rebuilt (reparent + re-add during a
        batch drag). Used by every hot-path scan (drop-index, indicator,
        drop-handler) to avoid None-deref crashes.
        """
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if _is_draggable_card(widget):
                yield widget

    def _find_drop_index(self, pos_y: int) -> int:
        """Find the logical drop slot based on vertical position.

        Returns a slot number: 0 = before first card, 1 = between card 0
        and card 1, etc. The indicator is NOT in the layout, so card
        positions are stable and don't cause feedback loops.
        """
        cards = list(self._iter_cards())

        for slot, card in enumerate(cards):
            card_mid = card.y() + card.height() // 2
            if pos_y < card_mid:
                return slot
        return len(cards)

    def _show_indicator(self, slot: int) -> None:
        """Position the indicator line at the given slot using absolute coords."""
        if slot == self._drop_slot and self._drag_indicator.isVisible():
            return

        self._drop_slot = slot

        # Find the y position for this slot
        cards = list(self._iter_cards())

        if not cards:
            self._drag_indicator.setVisible(False)
            return

        if slot <= 0:
            y = cards[0].y() - 2
        elif slot >= len(cards):
            last = cards[-1]
            y = last.y() + last.height() + 1
        else:
            y = cards[slot].y() - 2

        margins = self._content_layout.contentsMargins()
        self._drag_indicator.setGeometry(
            margins.left(), y,
            self._content.width() - margins.left() - margins.right(), 3,
        )
        self._drag_indicator.setVisible(True)
        self._drag_indicator.raise_()

    def _hide_indicator(self) -> None:
        self._drag_indicator.setVisible(False)
        self._drop_slot = -1

    def _find_card_anywhere(self, mod_id: int):
        """Find a card by mod_id in this group or any sibling group.

        Null-item guards protect against layout-in-flight during multi-drop.
        """
        # Check this group first
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if _is_draggable_card(w) and w.mod_id == mod_id:
                return w
        # Check sibling groups
        parent = self.parent()
        if parent:
            for child in parent.findChildren(FolderGroup):
                if child is self:
                    continue
                for i in range(child._content_layout.count()):
                    item = child._content_layout.itemAt(i)
                    if item is None:
                        continue
                    w = item.widget()
                    if _is_draggable_card(w) and w.mod_id == mod_id:
                        return w
        return None

    def _handle_drop_batch(self, mod_ids: list[int]) -> None:
        """Move one or more cards to the drop slot. Single atomic operation.

        Defensive against layout-mutation-in-progress: every ``itemAt`` call
        is null-checked before ``.widget()`` is invoked. This eliminates a
        reported crash where dragging a batch of cards into a folder mid-
        reparent would dereference a None layout item.
        """
        self._drag_indicator.setVisible(False)

        # Convert logical slot to layout index
        cards_in_layout = []
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if _is_draggable_card(w):
                cards_in_layout.append(i)

        slot = max(0, self._drop_slot)
        if slot < len(cards_in_layout):
            indicator_index = cards_in_layout[slot]
        elif cards_in_layout:
            indicator_index = cards_in_layout[-1] + 1
        else:
            indicator_index = self._content_layout.count()

        # Collect all cards and remove them from their current groups
        cards = []
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
        elif event.button() == Qt.MouseButton.LeftButton:
            self._folder_drag_start = event.pos()
            self._folder_drag_started = False
            self.toggle()

    def _on_header_move(self, event) -> None:
        """Start a folder drag if the header is dragged far enough."""
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._group_id is None:
            return  # Can't drag the Ungrouped folder
        if not hasattr(self, '_folder_drag_start'):
            return
        distance = (event.pos() - self._folder_drag_start).manhattanLength()
        if distance < 20:
            return

        self._folder_drag_started = True
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData("application/x-cdumm-folder-id", str(self._group_id).encode())
        drag.setMimeData(mime)

        # Render header as drag pixmap
        pixmap = QPixmap(self._header.size())
        bg = QColor(_CARD_COLORS[_theme_key()]["bg"])
        pixmap.fill(bg)
        self._header.render(pixmap)
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.pos())

        drag.exec(Qt.DropAction.MoveAction)

    # ── Drag-drop onto group header ────────────────────────────────────

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            event.acceptProposedAction()
        elif event.mimeData().hasFormat("application/x-cdumm-folder-id"):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            event.acceptProposedAction()
        elif event.mimeData().hasFormat("application/x-cdumm-folder-id"):
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        """Accept drops on the group header — mod cards or folder reorder."""
        # Folder reorder: a folder was dropped onto this folder's header
        if event.mimeData().hasFormat("application/x-cdumm-folder-id"):
            dragged_id = int(event.mimeData().data("application/x-cdumm-folder-id").data().decode())
            if self._group_id is not None and dragged_id != self._group_id:
                self.folder_dropped.emit(dragged_id, self._group_id)
            event.acceptProposedAction()
            return

        # Mod card drop
        if not event.mimeData().hasFormat("application/x-cdumm-mod-id"):
            return
        if event.mimeData().hasFormat("application/x-cdumm-mod-ids"):
            ids_bytes = event.mimeData().data("application/x-cdumm-mod-ids").data()
            mod_ids = [int(x) for x in ids_bytes.decode().split(",") if x]
        else:
            mod_id_bytes = event.mimeData().data("application/x-cdumm-mod-id").data()
            mod_ids = [int(mod_id_bytes.decode())]
        # Drop at end of group — null-safe via _iter_cards.
        card_count = sum(1 for _ in self._iter_cards())
        self._drop_slot = card_count
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
