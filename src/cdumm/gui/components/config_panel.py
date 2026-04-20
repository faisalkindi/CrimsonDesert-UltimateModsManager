"""Right-side sliding configuration panel for mod settings."""

from __future__ import annotations

import re as _re

_CATEGORY_PREFIX_RE = _re.compile(r"^(?P<cat>[^/]+?)\s*/\s*(?P<rest>.+)$")


def _group_variants_by_category_prefix(
    variants: list[dict],
) -> dict[str, list[int]] | None:
    """Parse variant labels of the form '<Category> / <Rest>' and
    return {category: [variant_index, ...]} preserving discovery order.

    Returns None unless:
      * EVERY variant's label matches the pattern (mixed sets would
        leave unmatched variants orphaned in the UI), AND
      * the total set spans 2+ categories (1 category is just a flat
        list, no collapsing needed).
    """
    groups: dict[str, list[int]] = {}
    for i, v in enumerate(variants):
        label = str(v.get("label", ""))
        m = _CATEGORY_PREFIX_RE.match(label)
        if not m:
            return None
        cat = m.group("cat").strip()
        if not cat:
            return None
        groups.setdefault(cat, []).append(i)
    if len(groups) < 2:
        return None
    return groups


def _strip_category_prefix(label: str) -> str:
    """Return the right-hand side of 'Category / Rest' labels, else
    the label unchanged."""
    if " / " in label:
        return label.split(" / ", 1)[1]
    return label


class _CollapsibleSection:
    """Tiny collapsible block: header button + a body widget that
    toggles visibility on click. Not a widget itself — callers layout
    the header and body separately. Kept deliberately simple so the
    Apply-theme pass on the parent ConfigPanel recolours the header
    label alongside every other text widget.
    """

    def __init__(self, title: str, count: int, *, start_expanded: bool):
        from PySide6.QtWidgets import QPushButton, QWidget, QVBoxLayout
        self._title = title
        self._count = count
        self._expanded = start_expanded
        self.header = QPushButton()
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        # Explicit theme-aware text color — the default QPushButton color
        # didn't inherit the ConfigPanel stylesheet and rendered
        # white-on-white in light mode. isDarkTheme() is sampled at
        # build time; _apply_theme on the parent ConfigPanel re-runs
        # show_variant_mod on theme flips which rebuilds these
        # sections from scratch.
        from qfluentwidgets import isDarkTheme
        _fg = "#E2E8F0" if isDarkTheme() else "#1A202C"
        self.header.setStyleSheet(
            f"QPushButton {{ text-align: left; padding: 10px 8px; "
            f"border: none; background: transparent; color: {_fg}; "
            f"font-weight: bold; font-size: 13px; }} "
            f"QPushButton:hover {{ background: rgba(128,128,128,0.08); "
            f"border-radius: 4px; }}"
        )
        self.body = QWidget()
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(18, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self.header.clicked.connect(self._toggle)
        self._refresh_header()
        self.body.setVisible(self._expanded)

    def add_row(self, widget) -> None:
        self._body_layout.addWidget(widget)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.body.setVisible(self._expanded)
        self._refresh_header()

    def _refresh_header(self) -> None:
        arrow = "\u25BE" if self._expanded else "\u25B8"   # ▾ / ▸
        self.header.setText(f"{arrow}  {self._title}    ({self._count})")


def _is_apply_visible(
    variant_widgets: dict,
    variant_initial: dict,
    label_dirty: set,
) -> bool:
    """Return True if the Apply button should be shown.

    Apply is visible when EITHER:
      * the variant radio differs from the initial snapshot, OR
      * any variant's labels were edited via the Configure picker
        (tracked in label_dirty — a set of variant filenames).

    Previously only the variant-changed branch existed, so a user who
    edited labels and then reverted their variant pick to initial lost
    the Apply button and dropped their label edits. Both conditions
    now count as dirty.
    """
    for idx, widget in variant_widgets.items():
        try:
            if widget.isChecked() != variant_initial.get(idx, False):
                return True
        except AttributeError:
            # test fixture may pass booleans directly
            if widget != variant_initial.get(idx, False):
                return True
    return bool(label_dirty)

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    PrimaryPushButton,
    SingleDirectionScrollArea,
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
    variants_apply_clicked = Signal(int, list)  # mod_id, [{label, filename, enabled, group}]

    _PANEL_WIDTH = 400
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
        self._value_inputs: dict[int, QSpinBox | QDoubleSpinBox] = {}
        self._initial_values: dict[int, int | float] = {}
        # Variant-mode bookkeeping (populated by show_variant_mod).
        self._variant_mode: bool = False
        self._variants_meta: list[dict] = []
        self._variant_widgets: dict[int, QCheckBox | QRadioButton] = {}
        self._variant_initial: dict[int, bool] = {}

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
        scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(SingleDirectionScrollArea.Shape.NoFrame)
        # No horizontal scrollbar — long labels wrap instead of overflowing.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_layout = QVBoxLayout(self._body)
        # 14px right padding so the scrollbar track doesn't sit on top
        # of radio / checkbox indicators at the right edge of each row.
        # Qt's default vertical scrollbar is 12-14px wide on Windows;
        # this clearance keeps the scrollbar and the indicators in
        # separate columns.
        self._body_layout.setContentsMargins(0, 0, 14, 0)
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
        self._value_inputs.clear()
        self._initial_values.clear()
        self._variant_mode = False
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
                ev = p.get("editable_value")
                if ev and isinstance(ev, dict) and "type" in ev:
                    self._add_editable_row(
                        i, p["label"], p.get("description", ""),
                        ev, p.get("custom_value"))
                else:
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
        name_lbl.setWordWrap(True)
        nf = name_lbl.font()
        nf.setPixelSize(13)
        from PySide6.QtGui import QFont
        nf.setWeight(QFont.Weight.DemiBold)
        name_lbl.setFont(nf)
        text_col.addWidget(name_lbl)
        if description:
            desc_lbl = CaptionLabel(description)
            desc_lbl.setWordWrap(True)
            df = desc_lbl.font()
            df.setPixelSize(11)
            desc_lbl.setFont(df)
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

    def _add_editable_row(
        self, index: int, label: str, description: str,
        editable_meta: dict, current_value: int | float | None,
    ) -> None:
        """Add a row with a numeric input for inline value editing."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 8)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = CaptionLabel(label)
        name_lbl.setWordWrap(True)
        nf = name_lbl.font()
        nf.setPixelSize(13)
        from PySide6.QtGui import QFont
        nf.setWeight(QFont.Weight.DemiBold)
        name_lbl.setFont(nf)
        text_col.addWidget(name_lbl)
        if description:
            desc_lbl = CaptionLabel(description)
            desc_lbl.setWordWrap(True)
            df = desc_lbl.font()
            df.setPixelSize(11)
            desc_lbl.setFont(df)
            text_col.addWidget(desc_lbl)
        # Show value range
        val_min = editable_meta.get("min", 0)
        val_max = editable_meta.get("max", 999999)
        # Validate min <= max
        if val_min > val_max:
            val_min, val_max = val_max, val_min
        range_lbl = CaptionLabel(f"Range: {val_min} – {val_max}")
        rf = range_lbl.font()
        rf.setPixelSize(10)
        range_lbl.setFont(rf)
        range_lbl.setStyleSheet(f"color: {_section_color()}; opacity: 0.7;")
        text_col.addWidget(range_lbl)
        row.addLayout(text_col, 1)

        # Value input
        val_type = editable_meta.get("type", "int32_le")
        default_val = editable_meta.get("default", val_min)
        if current_value is not None:
            default_val = current_value

        if val_type == "float32_le":
            spinbox = QDoubleSpinBox()
            spinbox.setDecimals(3)
            spinbox.setMinimum(float(val_min))
            spinbox.setMaximum(float(val_max))
            spinbox.setValue(float(default_val))
        else:
            spinbox = QSpinBox()
            spinbox.setMinimum(int(val_min))
            spinbox.setMaximum(int(val_max))
            spinbox.setValue(int(default_val))

        spinbox.setFixedWidth(90)
        spinbox.valueChanged.connect(self._on_value_changed)
        row.addWidget(spinbox, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._value_inputs[index] = spinbox
        self._labels[index] = label
        self._initial_values[index] = default_val

        container = QWidget()
        container.setLayout(row)
        container.setStyleSheet(
            f"border-bottom: 1px solid {_row_border()}; background: transparent;"
        )
        self._body_layout.addWidget(container)

    def _on_value_changed(self) -> None:
        """Show Apply when any value input differs from initial."""
        self._check_any_changed()

    def _on_toggle_changed(self) -> None:
        """Show the Apply button when any toggle differs from its initial state."""
        self._check_any_changed()

    def _check_any_changed(self) -> None:
        """Show Apply when any toggle or value input differs from initial."""
        changed = any(
            cb.isChecked() != self._initial_states[idx]
            for idx, cb in self._toggles.items()
        )
        if not changed:
            changed = any(
                sb.value() != self._initial_values[idx]
                for idx, sb in self._value_inputs.items()
            )
        self._apply_btn.setVisible(changed)

    def _on_apply(self) -> None:
        # Variant-mode apply — emit dedicated signal with variant metadata.
        if getattr(self, "_variant_mode", False):
            out: list[dict] = []
            for idx, v in enumerate(self._variants_meta):
                widget = self._variant_widgets.get(idx)
                if widget is None:
                    enabled = bool(v.get("enabled"))
                else:
                    enabled = widget.isChecked()
                row = {
                    "label": v.get("label", ""),
                    "filename": v.get("filename", ""),
                    "enabled": enabled,
                    "group": v.get("group", -1),
                }
                # Pass through grid-variant metadata (Character Creator
                # style) so mods_page can tell a grid apply from a JSON
                # multi-variant apply.
                if "_level" in v:
                    row["_level"] = v["_level"]
                if "_header" in v:
                    row["_header"] = v["_header"]
                out.append(row)
            self.variants_apply_clicked.emit(self._mod_id, out)
            return

        result = []
        for idx, cb in sorted(self._toggles.items()):
            result.append({"label": self._labels[idx], "enabled": cb.isChecked()})
        for idx, sb in sorted(self._value_inputs.items()):
            result.append({"label": self._labels[idx], "enabled": True, "value": sb.value()})
        self.apply_clicked.emit(self._mod_id, result)

    # ------------------------------------------------------------------
    # Variant-mode entry point
    # ------------------------------------------------------------------

    def show_variant_mod(
        self,
        mod_id: int,
        name: str,
        author: str,
        version: str,
        status: str,
        variants: list[dict],
        conflicts: list[str] | None = None,
    ) -> None:
        """Open the panel for a multi-variant JSON mod.

        ``variants`` is the list stored in ``mods.variants``:
        ``[{"label": str, "filename": str, "enabled": bool, "group": int}, ...]``.
        Variants that share a positive ``group`` are rendered as a radio
        group (only one may be enabled at a time); ``group == -1`` gets
        an independent checkbox.
        """
        self._mod_id = mod_id
        self._initial_states.clear()
        self._toggles.clear()
        self._labels.clear()
        self._value_inputs.clear()
        self._initial_values.clear()
        self._apply_btn.setVisible(False)
        self._variant_mode = True
        self._variants_meta = [dict(v) for v in variants]
        self._variant_widgets: dict[int, QCheckBox | QRadioButton] = {}
        self._variant_initial: dict[int, bool] = {
            i: bool(v.get("enabled")) for i, v in enumerate(self._variants_meta)
        }
        # Reset collapsible-section strong refs on every panel open.
        # Without this, switching from a mutex-pack mod to a plain
        # variant mod would leave dangling references to deleteLater'd
        # widgets. Any later iteration (e.g. a theme reapply pass)
        # would hit RuntimeError on a deleted C++ object. C-M2.
        self._collapsible_sections: list[_CollapsibleSection] = []
        # Per-variant label selections (populated by the "Configure..."
        # button and read by the page-level Apply handler). Keyed by
        # the variant's filename.
        self._variant_label_prev: dict[str, list[str]] = {}
        self._variant_label_dirty: set[str] = set()
        # Seed with any previously-persisted selections from mod_config
        # so the dialog pre-checks the user's last picks.
        try:
            import json as _j
            # Access the DB through the page parent if we can find it.
            db = None
            for cand in (getattr(self, "_db", None),
                         getattr(self.parent(), "_db", None) if self.parent() else None):
                if cand is not None:
                    db = cand
                    break
            if db is not None:
                row = db.connection.execute(
                    "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
                    (mod_id,)).fetchone()
                if row and row[0]:
                    sel = _j.loads(row[0])
                    # Accept the per-variant dict shape or a flat list
                    # (legacy single-JSON mods). Only the per-variant
                    # shape matters for variant mods.
                    if isinstance(sel, dict):
                        self._variant_label_prev = {
                            str(k): list(v) for k, v in sel.items()
                            if isinstance(v, list)
                        }
        except Exception as _e:
            logger.debug("Could not seed variant label prev: %s", _e)

        self._title_label.setText(name)
        self._author_label.setText(f"by {author}" if author else "")

        self._clear_badges()
        self._badge_row.insertWidget(0, _make_badge(status))
        if version:
            self._badge_row.insertWidget(
                1, _make_badge(f"v{version}", "#444C5C"))
        n_enabled = sum(1 for v in self._variants_meta if v.get("enabled"))
        # Detect mutex-variant-pack mode (collapsibles will render) so
        # the badge doesn't read "1/144 variants" — which feels alarming,
        # like 143 mods are broken. Show the active loadout name instead,
        # matching the user's mental model ("I've picked ONE loadout").
        _labels = [str(v.get("label", "")) for v in self._variants_meta]
        _mutex_pack = (
            len(self._variants_meta) >= 4
            and all(" / " in lbl for lbl in _labels)
            and _group_variants_by_category_prefix(self._variants_meta)
                is not None
        )
        if _mutex_pack:
            _active = next(
                (v for v in self._variants_meta if v.get("enabled")), None)
            if _active:
                _short = _strip_category_prefix(_active.get("label", ""))
                _badge_text = f"Active: {_short}"
            else:
                _badge_text = f"{len(self._variants_meta)} loadouts"
        else:
            _badge_text = f"{n_enabled}/{len(self._variants_meta)} variants"
        self._badge_row.insertWidget(
            2, _make_badge(_badge_text, "#444C5C"),
        )

        self._clear_body()
        # Translation key may not exist in older locale files — fall back to
        # the English literal if tr() returns the key unchanged.
        # Skip the generic VARIANTS header when every group has its own
        # _header (Character-Creator-style gender/race per-axis headers).
        _every_group_has_header = bool(variants) and all(
            v.get("_header") for v in variants if v.get("group", -1) >= 0)
        if not _every_group_has_header:
            variants_header = tr("config_panel.section_variants")
            if variants_header == "config_panel.section_variants":
                variants_header = "VARIANTS"
            self._add_section_header(variants_header)

        # Render radio groups (positive group ids, size ≥ 2) first, then
        # independent checkboxes (group = -1). Each row uses the same
        # label-column + indicator layout as ``_add_config_row`` so long
        # labels wrap and text reads correctly on both light + dark themes.
        groups: dict[int, list[int]] = {}
        independents: list[int] = []
        for i, v in enumerate(self._variants_meta):
            g = v.get("group", -1)
            if g >= 0:
                groups.setdefault(g, []).append(i)
            else:
                independents.append(i)

        # Archive-wide mutex packs (GildsGear-style: 40+ variants in one
        # radio group with 'Category / Variant' labels) render as
        # collapsible category sections so the user isn't scrolling
        # through 40 radios. Triggers when we have exactly ONE radio
        # group, no independents, and every label parses into 2+
        # categories.
        cat_groups = None
        if len(groups) == 1 and not independents:
            only_members = next(iter(groups.values()))
            member_variants = [self._variants_meta[i] for i in only_members]
            parsed = _group_variants_by_category_prefix(member_variants)
            if parsed is not None:
                # Map local member indices back to full variants_meta
                # indices so downstream handlers still work.
                cat_groups = {
                    cat: [only_members[li] for li in idxs]
                    for cat, idxs in parsed.items()
                }

        if cat_groups:
            # Which category contains the currently-enabled variant?
            # That one starts expanded; the rest collapsed.
            active_cat = next(iter(cat_groups))
            for cat, idxs in cat_groups.items():
                if any(self._variants_meta[i].get("enabled") for i in idxs):
                    active_cat = cat
                    break
            button_group = QButtonGroup(self)
            button_group.setExclusive(True)
            # PySide6 signal connections hold a WEAK reference to
            # bound-method slots. If the section Python object goes out
            # of scope after this loop, GC collects it and the
            # header-button click becomes a silent no-op on Windows
            # (confirmed via Qt forum thread 154590). Keep a strong
            # reference on the panel so every section stays alive.
            self._collapsible_sections: list[_CollapsibleSection] = []
            for cat, idxs in cat_groups.items():
                section = _CollapsibleSection(
                    cat, len(idxs), start_expanded=(cat == active_cat))
                self._collapsible_sections.append(section)
                self._body_layout.addWidget(section.header)
                self._body_layout.addWidget(section.body)
                for idx in idxs:
                    v = dict(self._variants_meta[idx])
                    # Show just the right-hand side ('AbyssGear_1')
                    # since the section header already says
                    # 'Abyss Gears'.
                    v["label"] = _strip_category_prefix(v.get("label", ""))
                    rb = QRadioButton()
                    rb.setChecked(bool(self._variants_meta[idx].get("enabled")))
                    rb.toggled.connect(self._on_variant_changed)
                    button_group.addButton(rb, idx)
                    self._variant_widgets[idx] = rb
                    section.add_row(self._build_variant_row(rb, v))
            # Skip the flat-group render below.
            if conflicts:
                self._add_section_header(tr("config_panel.section_conflicts"))
                for desc in conflicts:
                    lbl = CaptionLabel(desc)
                    lbl.setWordWrap(True)
                    lbl.setStyleSheet("color: #D04848; padding: 4px 0;")
                    self._body_layout.addWidget(lbl)
            self._body_layout.addStretch()
            self._apply_theme()

            self.setVisible(True)
            self._anim.stop()
            try:
                self._anim.finished.disconnect(self._emit_closed)
            except RuntimeError:
                pass
            self._anim.setStartValue(self.maximumWidth())
            self._anim.setEndValue(self._PANEL_WIDTH)
            self._opacity_effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(self._opacity_effect)
            self._opacity_effect.setOpacity(0.0)
            self._fade_anim = QPropertyAnimation(
                self._opacity_effect, b"opacity")
            self._fade_anim.setDuration(self._ANIM_DURATION)
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._fade_anim.finished.connect(
                lambda: self.setGraphicsEffect(None))
            self._anim.start()
            self._fade_anim.start()
            return

        for g_id, members in sorted(groups.items()):
            # Per-group header (axis name like "Gender" / "Race") when
            # the variants were emitted with `_header` metadata.
            if members:
                first = self._variants_meta[members[0]]
                hdr = first.get("_header")
                if hdr:
                    self._add_section_header(hdr.upper())
            button_group = QButtonGroup(self)
            button_group.setExclusive(True)
            for idx in members:
                v = self._variants_meta[idx]
                rb = QRadioButton()
                rb.setChecked(bool(v.get("enabled")))
                rb.toggled.connect(self._on_variant_changed)
                button_group.addButton(rb, idx)
                self._variant_widgets[idx] = rb
                self._body_layout.addWidget(
                    self._build_variant_row(rb, v))

        for idx in independents:
            v = self._variants_meta[idx]
            cb = QCheckBox()
            cb.setChecked(bool(v.get("enabled")))
            cb.toggled.connect(self._on_variant_changed)
            self._variant_widgets[idx] = cb
            self._body_layout.addWidget(self._build_variant_row(cb, v))

        if conflicts:
            self._add_section_header(tr("config_panel.section_conflicts"))
            for desc in conflicts:
                lbl = CaptionLabel(desc)
                lbl.setWordWrap(True)
                lbl.setStyleSheet("color: #D04848; padding: 4px 0;")
                self._body_layout.addWidget(lbl)

        self._body_layout.addStretch()
        self._apply_theme()

        self.setVisible(True)
        self._anim.stop()
        try:
            self._anim.finished.disconnect(self._emit_closed)
        except RuntimeError:
            pass
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(self._PANEL_WIDTH)
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

    def _build_variant_row(self, indicator, variant: dict) -> QWidget:
        """Build a label-left / indicator-right row for a variant.

        Matches the visual language of ``_add_config_row``. Label colors
        come from the parent ``ConfigPanel`` stylesheet (via ``_apply_theme``)
        rather than inline overrides — mirroring how the per-change toggle
        rows work, so dark-theme and light-theme both render correctly
        without us second-guessing ``isDarkTheme()`` at row-build time.
        """
        from PySide6.QtGui import QFont

        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        row.setSpacing(8)

        # Label column (wraps)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        # Title — plain QLabel inherits ConfigPanel's QLabel color.
        title_lbl = QLabel(variant.get("label", ""))
        title_lbl.setWordWrap(True)
        title_lbl.setMinimumWidth(1)
        tf = title_lbl.font()
        tf.setPixelSize(14)
        tf.setWeight(QFont.Weight.DemiBold)
        title_lbl.setFont(tf)
        text_col.addWidget(title_lbl)

        # Meta — CaptionLabel has qfluentwidgets' built-in subtle caption
        # color that follows the active theme automatically.
        meta_bits: list[str] = []
        if variant.get("version"):
            meta_bits.append(f"v{variant['version']}")
        if variant.get("author"):
            meta_bits.append(f"by {variant['author']}")
        if meta_bits:
            meta_lbl = CaptionLabel(" · ".join(meta_bits))
            meta_lbl.setWordWrap(True)
            mf = meta_lbl.font()
            mf.setPixelSize(11)
            meta_lbl.setFont(mf)
            text_col.addWidget(meta_lbl)

        row.addLayout(text_col, 1)

        # Indicator-only widget (no built-in text — the label column handles it).
        # Suppress Qt's default focus frame / background so the widget shows
        # JUST the round radio dot / square checkbox, no rectangular outline.
        accent = "#2878D0"
        # Medium gray reads as a subtle-but-visible outline on both themes.
        unchecked_border = "#7B8595"
        indicator.setStyleSheet(
            "QCheckBox, QRadioButton { "
            "  border: none; background: transparent; spacing: 0; "
            "  padding: 0; margin: 0; outline: none; "
            "}"
            "QCheckBox:focus, QRadioButton:focus { outline: none; border: none; }"
            "QCheckBox::indicator, QRadioButton::indicator { "
            "  width: 18px; height: 18px; "
            "}"
            "QCheckBox::indicator { border-radius: 4px; }"
            f"QCheckBox::indicator:unchecked {{ "
            f"  background: transparent; border: 2px solid {unchecked_border}; "
            f"}}"
            f"QCheckBox::indicator:checked {{ "
            f"  background: {accent}; border: 2px solid {accent}; "
            f"}}"
            "QRadioButton::indicator { border-radius: 10px; }"
            f"QRadioButton::indicator:unchecked {{ "
            f"  background: transparent; border: 2px solid {unchecked_border}; "
            f"}}"
            f"QRadioButton::indicator:checked {{ "
            f"  background: {accent}; border: 2px solid {accent}; "
            f"}}"
        )
        row.addWidget(indicator, 0,
                      Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # When the variant ships internal labeled changes (e.g. Unlimited
        # Dragon Flying's mutex Ride Duration presets), append a
        # second-row Configure button BELOW the title. Inline placement
        # competes with the variant title for horizontal space and
        # truncates text in the narrow side panel.
        cfg_btn_widget = None
        if variant.get("_has_labels") and variant.get("_json_path"):
            from qfluentwidgets import PushButton
            cfg_btn = PushButton("Configure options...")
            cfg_btn.setFixedHeight(28)
            jp_path = variant["_json_path"]
            v_fn = variant.get("filename", "")
            cfg_btn.clicked.connect(
                lambda _checked=False, p=jp_path, fn=v_fn:
                    self._open_variant_label_picker(p, fn))
            cfg_btn_widget = cfg_btn

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        # Top: title + indicator row
        top_row_widget = QWidget()
        top_row_widget.setLayout(row)
        outer.addWidget(top_row_widget)
        # Bottom: full-width Configure button if relevant
        if cfg_btn_widget is not None:
            outer.addWidget(cfg_btn_widget)

        container = QWidget()
        container.setLayout(outer)
        container.setStyleSheet(
            f"border-bottom: 1px solid {_row_border()}; background: transparent;")
        return container

    def _open_variant_label_picker(self, json_path: str, variant_filename: str) -> None:
        """Pop the TogglePickerDialog for a single variant's JSON.

        The dialog already has mutex-offset detection (multiple changes
        at the same byte offset become a radio group). User's picks get
        stashed on the panel so the page-level Apply handler can persist
        them to mod_config.selected_labels and regenerate merged.json
        through synthesize_merged_json's label_selections param.
        """
        import json as _json
        from pathlib import Path as _Path
        from cdumm.gui.preset_picker import TogglePickerDialog
        try:
            data = _json.loads(_Path(json_path).read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read variant JSON %s: %s",
                           json_path, e)
            return
        previous = self._variant_label_prev.get(variant_filename) or []
        # `previous` is now a list of [patch_idx, change_idx] pairs
        # (post Codex-P1 fix). TogglePickerDialog expects plain label
        # strings for pre-check display, so convert. Legacy DB rows
        # may still be plain strings — pass them through unchanged.
        prev_labels: list[str] = []
        if previous and isinstance(previous[0], str):
            prev_labels = list(previous)
        else:
            for pair in previous:
                try:
                    pi, ci = int(pair[0]), int(pair[1])
                    c = data["patches"][pi]["changes"][ci]
                    if "label" in c:
                        prev_labels.append(c["label"])
                except (IndexError, KeyError, TypeError):
                    continue
        # Parent the modal dialog to the TOP-LEVEL window, not to the
        # narrow side panel. Otherwise the dialog inherits the panel's
        # cramped width and can't render readable mutex / checkbox
        # rows (the user reported the dialog looking truncated).
        top_parent = self.window() or self
        dlg = TogglePickerDialog(data, parent=top_parent,
                                  previous_labels=prev_labels)
        if dlg.exec() and dlg.selected_data is not None:
            # Extract stable (patch_idx, change_idx) keys for each
            # picked change. Label-text matching (old approach) broke
            # on variants that reused a label — picking one silently
            # picked every sibling sharing the text. Codex P1 fix.
            # We look up the INDEX of each picked change inside the
            # ORIGINAL variant JSON so downstream
            # synthesize_merged_json can reproduce the exact pick.
            picked_keys: list[list[int]] = []
            # Build a lookup: (game_file, offset, patched) -> (p_idx, c_idx)
            # from the ORIGINAL data, then stamp those indices onto
            # each picked change. The picker returns a deep-copied
            # subset of the data, so identity (id()) doesn't apply —
            # but (game_file, offset, patched) is unique enough to
            # locate each picked change in the source.
            index_lookup: dict[tuple, list[int]] = {}
            for p_idx, p in enumerate(data.get("patches", [])):
                gf = p.get("game_file", "")
                for c_idx, c in enumerate(p.get("changes", [])):
                    if "label" not in c:
                        continue
                    key = (gf, c.get("offset"), c.get("patched"),
                           c.get("label"))
                    index_lookup.setdefault(key, [p_idx, c_idx])
            for p in dlg.selected_data.get("patches", []):
                gf = p.get("game_file", "")
                for c in p.get("changes", []):
                    if "label" not in c:
                        continue
                    key = (gf, c.get("offset"), c.get("patched"),
                           c.get("label"))
                    pair = index_lookup.get(key)
                    if pair is not None:
                        picked_keys.append(list(pair))
            self._variant_label_prev[variant_filename] = picked_keys
            self._variant_label_dirty.add(variant_filename)
            self._apply_btn.setVisible(True)

    def _on_variant_changed(self, *_a) -> None:
        # Apply stays visible if EITHER the variant pick differs from
        # initial OR any variant's labels were edited (tracked by
        # _variant_label_dirty). Previously only the variant-change
        # branch was checked, so reverting a variant after editing
        # labels hid Apply and dropped the label edits.
        self._apply_btn.setVisible(_is_apply_visible(
            self._variant_widgets,
            self._variant_initial,
            getattr(self, "_variant_label_dirty", set()),
        ))
