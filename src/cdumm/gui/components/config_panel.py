"""Right-side sliding configuration panel for mod settings."""

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
                out.append({
                    "label": v.get("label", ""),
                    "filename": v.get("filename", ""),
                    "enabled": enabled,
                    "group": v.get("group", -1),
                })
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

        self._title_label.setText(name)
        self._author_label.setText(f"by {author}" if author else "")

        self._clear_badges()
        self._badge_row.insertWidget(0, _make_badge(status))
        if version:
            self._badge_row.insertWidget(
                1, _make_badge(f"v{version}", "#444C5C"))
        n_enabled = sum(1 for v in self._variants_meta if v.get("enabled"))
        self._badge_row.insertWidget(
            2,
            _make_badge(f"{n_enabled}/{len(self._variants_meta)} variants",
                        "#444C5C"),
        )

        self._clear_body()
        # Translation key may not exist in older locale files — fall back to
        # the English literal if tr() returns the key unchanged.
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

        for g_id, members in sorted(groups.items()):
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

        container = QWidget()
        container.setLayout(row)
        container.setStyleSheet(
            f"border-bottom: 1px solid {_row_border()}; background: transparent;")
        return container

    def _on_variant_changed(self, *_a) -> None:
        changed = False
        for idx, widget in self._variant_widgets.items():
            if widget.isChecked() != self._variant_initial.get(idx, False):
                changed = True
                break
        self._apply_btn.setVisible(changed)
