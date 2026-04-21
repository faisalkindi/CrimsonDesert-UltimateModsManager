"""Dialog for merging two ReShade presets.

UX model (asymmetric merge):
  1. Pick the MAIN preset — this is the base. Everything in it is kept.
  2. Pick the preset to bring effects from.
  3. Tick which effects (shader sections) to bring across.
     For each: shows whether it will OVERWRITE an existing effect in the main
     preset, or ADD a new one.
  4. Name the output file.

The dialog returns a `MergeDialogResult` with the user's choices; the caller
runs the actual merge (engine/reshade_preset_ops.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    MessageBoxBase,
    PushButton,
    StrongBodyLabel,
    TransparentToolButton,
)

from cdumm.engine.reshade_preset_ops import read_preset_for_merge
from cdumm.i18n import tr


@dataclass(frozen=True)
class MergeDialogResult:
    main_path: Path
    other_path: Path
    sections_to_take: list[str]
    output_filename: str   # just the filename (no path); dialog appends .ini


class _CollapsibleSectionGroup(QWidget):
    """A collapsible group:
      - Row 1: [arrow] [title]              [Select all button]
      - Row 2: [small caption subtitle]
      - Row 3: a thin 1px divider below the subtitle (theme-agnostic)
      - Body: checkboxes

    No card-style background — previous versions painted a CardWidget
    background that, in some Qt + HiDPI combinations, extended past the
    widget's own rect and visually leaked over siblings. This plain-widget
    implementation uses spacing + a thin divider line for group separation.
    """

    def __init__(self, title: str, subtitle: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._checkboxes: list[CheckBox] = []
        self._expanded = True

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 6, 4, 6)
        root.setSpacing(4)

        # Header row — fixed layout so the button never gets pushed off.
        header_wrap = QWidget(self)
        header = QHBoxLayout(header_wrap)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)

        self._arrow_btn = TransparentToolButton(FluentIcon.CHEVRON_RIGHT, header_wrap)
        self._arrow_btn.setFixedSize(28, 28)
        self._arrow_btn.clicked.connect(self._toggle)
        header.addWidget(self._arrow_btn)

        self._title_label = StrongBodyLabel(title, header_wrap)
        tf = self._title_label.font()
        tf.setPixelSize(15)
        self._title_label.setFont(tf)
        self._title_label.mousePressEvent = lambda _e: self._toggle()
        self._title_label.setCursor(Qt.CursorShape.PointingHandCursor)
        header.addWidget(self._title_label)

        # Spacer pushes the button to the right edge.
        header.addStretch(1)

        # "Select all" — a visible PushButton with icon that swaps to
        # "Clear all" once everything is ticked.
        self._select_all_btn = PushButton(
            FluentIcon.ACCEPT, tr("reshade.merge_group_select_all"), header_wrap)
        self._select_all_btn.setFixedHeight(30)
        self._select_all_btn.clicked.connect(self._on_select_all_clicked)
        header.addWidget(self._select_all_btn)
        root.addWidget(header_wrap)

        # Subtitle row — small caption text under the header for context.
        if subtitle:
            self._subtitle_label = CaptionLabel(subtitle, self)
            self._subtitle_label.setWordWrap(True)
            self._subtitle_label.setContentsMargins(38, 0, 4, 4)
            root.addWidget(self._subtitle_label)
        else:
            self._subtitle_label = None

        # Thin 1px divider line between the header area and the checkbox body,
        # indented to match. A plain HLine QFrame takes its color from the
        # current Qt palette so it works in both light and dark themes.
        divider = QFrame(self)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        divider.setStyleSheet("QFrame { color: rgba(128, 128, 128, 0.25); }")
        divider.setFixedHeight(1)
        divider.setContentsMargins(38, 0, 8, 0)
        root.addWidget(divider)

        # Body: holds checkboxes. Hidden when collapsed.
        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(38, 8, 8, 4)
        self._body_layout.setSpacing(8)
        root.addWidget(self._body)

        # Keep divider's visibility tied to expansion state.
        self._divider = divider

        # Start expanded.
        self._apply_expanded()

    # ── Public surface ──────────────────────────────────────────

    def add_checkbox(self, cb: CheckBox) -> None:
        self._checkboxes.append(cb)
        self._body_layout.addWidget(cb)
        cb.stateChanged.connect(self._refresh_select_all_label)
        self._refresh_select_all_label()

    def checkboxes(self) -> list[CheckBox]:
        return list(self._checkboxes)

    def set_title(self, title: str) -> None:
        self._title = title
        self._title_label.setText(title)

    # ── Internals ───────────────────────────────────────────────

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._apply_expanded()

    def _apply_expanded(self) -> None:
        self._body.setVisible(self._expanded)
        # Hide the divider when collapsed too, so collapsed groups are compact.
        if hasattr(self, "_divider") and self._divider is not None:
            self._divider.setVisible(self._expanded)
        self._arrow_btn.setIcon(
            FluentIcon.CHEVRON_DOWN_MED if self._expanded
            else FluentIcon.CHEVRON_RIGHT_MED)

    def _on_select_all_clicked(self) -> None:
        # If all are already checked, toggle to clear. Otherwise check all.
        all_checked = all(cb.isChecked() for cb in self._checkboxes)
        new_state = not all_checked
        for cb in self._checkboxes:
            cb.setChecked(new_state)
        self._refresh_select_all_label()

    def _refresh_select_all_label(self) -> None:
        if not self._checkboxes:
            self._select_all_btn.setEnabled(False)
            return
        self._select_all_btn.setEnabled(True)
        all_checked = all(cb.isChecked() for cb in self._checkboxes)
        # Toggle both the label AND the icon so the button state is
        # obvious without reading the text.
        if all_checked:
            self._select_all_btn.setText(tr("reshade.merge_group_clear_all"))
            self._select_all_btn.setIcon(FluentIcon.REMOVE)
        else:
            self._select_all_btn.setText(tr("reshade.merge_group_select_all"))
            self._select_all_btn.setIcon(FluentIcon.ACCEPT)


class ReshadeMergeDialog(MessageBoxBase):
    """Modal dialog for picking two presets + which sections to merge."""

    def __init__(self, presets: list[Path], base_path: Path, parent=None):
        super().__init__(parent)
        self._presets = list(presets)
        self._base_path = base_path
        self._section_checks: list[tuple[str, CheckBox]] = []
        self._section_container: QWidget | None = None
        self._main_sections: dict[str, dict[str, str]] = {}
        self._other_sections: dict[str, dict[str, str]] = {}
        self._include_non_fx = False
        self._error_label: CaptionLabel | None = None

        self._build_ui()
        self._refresh_sections()

    # ── UI --------------------------------------------------------------

    def _build_ui(self) -> None:
        # Give the whole view more breathing room. MessageBoxBase's default
        # viewLayout spacing is 12; bumping to 14 + tighter-but-deliberate
        # section gaps gives a cleaner feel without wasting vertical space.
        self.viewLayout.setSpacing(10)

        title = StrongBodyLabel(tr("reshade.merge_dialog_title"), self)
        tf = title.font()
        tf.setPixelSize(22)
        title.setFont(tf)
        self.viewLayout.addWidget(title)
        self.viewLayout.addSpacing(12)

        # Error label lives UNDER the title so validation failures are
        # impossible to miss (previously at the bottom, users never saw them).
        self._error_label = CaptionLabel("", self)
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(
            "QLabel { background-color: rgba(196, 49, 75, 0.15);"
            " color: #C4314B; padding: 10px 14px; border-radius: 6px;"
            " font-weight: 600; }")
        self._error_label.setVisible(False)
        self.viewLayout.addWidget(self._error_label)

        # Two presets side-by-side would crowd on narrower widths; stacked
        # layout with clear label-above-field gives each combo its own row
        # and readable label. Each combo gets a fixed taller height so the
        # dropdown area feels solid.
        self.viewLayout.addWidget(BodyLabel(tr("reshade.merge_main_label"), self))
        self._main_combo = ComboBox(self)
        self._main_combo.setFixedHeight(36)
        for p in self._presets:
            self._main_combo.addItem(p.stem, userData=p)
        self._main_combo.currentIndexChanged.connect(self._on_main_changed)
        self.viewLayout.addWidget(self._main_combo)
        self.viewLayout.addSpacing(14)

        # Other preset — dropdown excludes whatever's currently selected
        # as the main. Structurally prevents same-preset merges.
        self.viewLayout.addWidget(BodyLabel(tr("reshade.merge_other_label"), self))
        self._other_combo = ComboBox(self)
        self._other_combo.setFixedHeight(36)
        self._other_combo.currentIndexChanged.connect(self._on_other_changed)
        self.viewLayout.addWidget(self._other_combo)
        self.viewLayout.addSpacing(18)

        # Sections picker — plain QScrollArea with explicit vertical scrollbar
        # policy (tested to correctly clip the viewport) and a transparent
        # background so the scroll area inherits the dialog's paper color
        # instead of Qt's unstyled dark widget background.
        #
        # CRITICAL: use setMinimumHeight + stretch=1 (NOT setFixedHeight). A
        # fixed height forces the scroll area to be that tall regardless of
        # available space in the dialog. On smaller windows the dialog card
        # gets squeezed but the scroll area refuses to shrink, so widgets
        # below it (Include advanced, Save-as label/input) overlap with the
        # scroll area's content. With min+stretch the scroll area gives up
        # space when crowded but expands to fill free space when available.
        self.viewLayout.addWidget(BodyLabel(tr("reshade.merge_sections_label"), self))
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(180)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        scroll.viewport().setAutoFillBackground(False)
        self._section_container = QWidget()
        self._section_container.setAutoFillBackground(False)
        self._section_container_layout = QVBoxLayout(self._section_container)
        self._section_container_layout.setContentsMargins(4, 6, 4, 6)
        self._section_container_layout.setSpacing(12)
        scroll.setWidget(self._section_container)
        # stretch=1 gives the scroll area all leftover vertical space. Every
        # other widget in viewLayout is pinned to its sizeHint, so the scroll
        # area alone absorbs the dialog's vertical dimension.
        self.viewLayout.addWidget(scroll, stretch=1)
        self.viewLayout.addSpacing(10)

        # Advanced toggle: include non-fx sections (GENERAL, DEPTH, etc.).
        self._include_non_fx_cb = CheckBox(
            tr("reshade.merge_include_advanced"), self)
        self._include_non_fx_cb.setToolTip(
            tr("reshade.merge_include_advanced_tooltip"))
        self._include_non_fx_cb.stateChanged.connect(
            self._on_include_advanced_toggled)
        self.viewLayout.addWidget(self._include_non_fx_cb)
        self.viewLayout.addSpacing(16)

        # Output filename — auto-populated with a sensible default.
        self.viewLayout.addWidget(BodyLabel(tr("reshade.merge_output_label"), self))
        self._name_edit = LineEdit(self)
        self._name_edit.setFixedHeight(36)
        self._name_edit.setPlaceholderText("MergedPreset.ini")
        self._name_edit.textEdited.connect(lambda _=None: self._hide_error())
        self.viewLayout.addWidget(self._name_edit)

        # OK / Cancel — override button text (MessageBoxBase provides them)
        self.yesButton.setText(tr("reshade.merge_ok"))
        self.cancelButton.setText(tr("reshade.merge_cancel"))

        # Populate "Add from" for the default main selection, and prefill
        # the output filename.
        self._repopulate_other_combo()
        self._autofill_output_name()

        # Bigger dialog footprint — wide enough for long preset names,
        # tall enough for the full stack without cutting into the scroll
        # area. Min-height stays conservative so smaller windows still fit.
        self.widget.setMinimumWidth(720)
        self.widget.setMinimumHeight(660)

    def _on_main_changed(self) -> None:
        """Main preset changed: rebuild 'Add from' list (excluding the new
        main), re-fill the output filename, refresh sections."""
        self._hide_error()
        self._repopulate_other_combo()
        self._autofill_output_name()
        self._refresh_sections()

    def _on_other_changed(self) -> None:
        self._hide_error()
        self._autofill_output_name()
        self._refresh_sections()

    def _repopulate_other_combo(self) -> None:
        """Fill the 'Add from' dropdown with every preset EXCEPT the main
        one. Structurally prevents merging a preset with itself."""
        main_path = self._main_combo.currentData()
        # Block signals while we rebuild so _refresh_sections only fires once.
        self._other_combo.blockSignals(True)
        self._other_combo.clear()
        for p in self._presets:
            if main_path is not None and p == main_path:
                continue
            self._other_combo.addItem(p.stem, userData=p)
        self._other_combo.blockSignals(False)

    def _autofill_output_name(self) -> None:
        """Pre-fill the output filename with a sensible default.

        Leaves the user's typing alone once they've actually edited the field
        (we can tell via isModified()). Default format:
        `<Main> + <Other>.ini`.
        """
        if self._name_edit.isModified():
            return
        main_path = self._main_combo.currentData()
        other_path = self._other_combo.currentData()
        if main_path is None or other_path is None:
            return
        suggested = f"{main_path.stem} + {other_path.stem}.ini"
        self._name_edit.setText(suggested)
        # Clear modified so further combo changes keep refreshing until the
        # user actually types something.
        self._name_edit.setModified(False)

    def _refresh_sections(self) -> None:
        """Rebuild the section picker, split into collapsible groups by
        overwrite/add/advanced category. PRESERVES the user's existing tick
        state across rebuilds -- toggling 'Include advanced' or switching
        presets used to wipe everything, losing the user's work.
        """
        assert self._section_container is not None

        # Capture what's currently ticked BEFORE we destroy the widgets so
        # we can re-apply the state after rebuilding.
        previously_ticked = {
            name for name, cb in self._section_checks if cb.isChecked()
        }

        # Clear existing content.
        while self._section_container_layout.count():
            item = self._section_container_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._section_checks.clear()

        main_path = self._main_combo.currentData()
        other_path = self._other_combo.currentData()
        if main_path is None or other_path is None:
            return

        self._main_sections = read_preset_for_merge(main_path)
        self._other_sections = read_preset_for_merge(other_path)

        # Bucket sections: existing (overwrite), new (add), advanced (non-fx).
        # Skip the synthetic __preamble__ section entirely -- the user doesn't
        # want to see it as a merge candidate, it's a parser artifact.
        from cdumm.engine.reshade_preset_ops import _PREAMBLE_SECTION
        new_sections: list[str] = []
        existing_sections: list[str] = []
        advanced_sections: list[str] = []
        for section_name in self._other_sections:
            if section_name == _PREAMBLE_SECTION:
                continue
            is_fx = section_name.lower().endswith(".fx")
            if not is_fx:
                if self._include_non_fx:
                    advanced_sections.append(section_name)
                continue
            if section_name in self._main_sections:
                existing_sections.append(section_name)
            else:
                new_sections.append(section_name)

        any_row = False

        # Group 1: NEW effects (not in main) -- most common user intent first.
        if new_sections:
            group = _CollapsibleSectionGroup(
                title=tr("reshade.merge_group_new", count=len(new_sections)),
                subtitle=tr("reshade.merge_group_new_sub"),
                parent=self._section_container)
            for section_name in new_sections:
                pretty = section_name.removesuffix(".fx")
                cb = CheckBox(pretty, group)
                if section_name in previously_ticked:
                    cb.setChecked(True)
                group.add_checkbox(cb)
                self._section_checks.append((section_name, cb))
            self._section_container_layout.addWidget(group)
            any_row = True

        # Group 2: EXISTING effects (would overwrite main's version).
        if existing_sections:
            group = _CollapsibleSectionGroup(
                title=tr("reshade.merge_group_existing",
                         count=len(existing_sections)),
                subtitle=tr("reshade.merge_group_existing_sub"),
                parent=self._section_container)
            for section_name in existing_sections:
                pretty = section_name.removesuffix(".fx")
                cb = CheckBox(pretty, group)
                if section_name in previously_ticked:
                    cb.setChecked(True)
                group.add_checkbox(cb)
                self._section_checks.append((section_name, cb))
            self._section_container_layout.addWidget(group)
            any_row = True

        # Group 3: Advanced (non-fx) sections. Only when the toggle is on.
        if advanced_sections:
            group = _CollapsibleSectionGroup(
                title=tr("reshade.merge_group_advanced",
                         count=len(advanced_sections)),
                subtitle=tr("reshade.merge_group_advanced_sub"),
                parent=self._section_container)
            for section_name in advanced_sections:
                cb = CheckBox(section_name, group)
                if section_name in previously_ticked:
                    cb.setChecked(True)
                group.add_checkbox(cb)
                self._section_checks.append((section_name, cb))
            self._section_container_layout.addWidget(group)
            any_row = True

        if not any_row:
            self._section_container_layout.addWidget(
                CaptionLabel(tr("reshade.merge_nothing_to_take"),
                             self._section_container))
        self._section_container_layout.addStretch()

    def _on_include_advanced_toggled(self, _state: int) -> None:
        self._include_non_fx = self._include_non_fx_cb.isChecked()
        self._refresh_sections()

    # ── Result -------------------------------------------------------------

    def get_result(self) -> MergeDialogResult | None:
        """Return the user's picks. Does basic validation; returns None on bad input."""
        main_path = self._main_combo.currentData()
        other_path = self._other_combo.currentData()
        name = self._name_edit.text().strip()

        if main_path is None or other_path is None:
            return None
        if main_path == other_path:
            return None
        if not name:
            return None
        if not name.lower().endswith(".ini"):
            name += ".ini"

        sections = [sec for sec, cb in self._section_checks if cb.isChecked()]
        return MergeDialogResult(
            main_path=Path(main_path),
            other_path=Path(other_path),
            sections_to_take=sections,
            output_filename=name,
        )

    def validate(self) -> bool:
        """MessageBoxBase validate hook: returning False keeps the dialog
        open AND shows a specific error label (at the TOP of the dialog,
        not the bottom) so the user sees immediately why OK didn't work.

        Same-preset check is unnecessary here because the UI excludes the
        main preset from the 'Add from' dropdown.
        """
        main_path = self._main_combo.currentData()
        other_path = self._other_combo.currentData()
        name = self._name_edit.text().strip()

        if main_path is None or other_path is None:
            # Can only happen if there are fewer than 2 presets — the page
            # already guards against opening the dialog in that case.
            self._show_error(tr("reshade.merge_same_preset_error"))
            return False
        if not name:
            self._show_error(tr("reshade.merge_empty_name_error"))
            return False

        normalized_name = name if name.lower().endswith(".ini") else name + ".ini"
        if (self._base_path / normalized_name).exists():
            self._show_error(
                tr("reshade.merge_existing_output_error", name=normalized_name))
            return False

        self._hide_error()
        return True

    def _show_error(self, message: str) -> None:
        if self._error_label is not None:
            self._error_label.setText(message)
            self._error_label.setVisible(True)

    def _hide_error(self) -> None:
        if self._error_label is not None:
            self._error_label.setVisible(False)
