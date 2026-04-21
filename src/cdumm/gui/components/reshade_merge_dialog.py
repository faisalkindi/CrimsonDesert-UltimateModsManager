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
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    MessageBoxBase,
    SmoothScrollArea,
    StrongBodyLabel,
)

from cdumm.engine.reshade_preset_ops import read_preset_for_merge
from cdumm.i18n import tr


@dataclass(frozen=True)
class MergeDialogResult:
    main_path: Path
    other_path: Path
    sections_to_take: list[str]
    output_filename: str   # just the filename (no path); dialog appends .ini


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
        title = StrongBodyLabel(tr("reshade.merge_dialog_title"), self)
        tf = title.font()
        tf.setPixelSize(20)
        title.setFont(tf)
        self.viewLayout.addWidget(title)
        self.viewLayout.addSpacing(8)

        # Error label lives UNDER the title so validation failures are
        # impossible to miss (previously at the bottom, users never saw them).
        self._error_label = CaptionLabel("", self)
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(
            "QLabel { background-color: rgba(196, 49, 75, 0.15);"
            " color: #C4314B; padding: 6px 10px; border-radius: 4px;"
            " font-weight: 600; }")
        self._error_label.setVisible(False)
        self.viewLayout.addWidget(self._error_label)

        # Main preset
        self.viewLayout.addWidget(BodyLabel(tr("reshade.merge_main_label"), self))
        self._main_combo = ComboBox(self)
        for p in self._presets:
            self._main_combo.addItem(p.stem, userData=p)
        self._main_combo.currentIndexChanged.connect(self._on_main_changed)
        self.viewLayout.addWidget(self._main_combo)
        self.viewLayout.addSpacing(8)

        # Other preset -- dropdown excludes whatever's currently selected
        # as the main. This structurally prevents same-preset merges.
        self.viewLayout.addWidget(BodyLabel(tr("reshade.merge_other_label"), self))
        self._other_combo = ComboBox(self)
        self._other_combo.currentIndexChanged.connect(self._on_other_changed)
        self.viewLayout.addWidget(self._other_combo)
        self.viewLayout.addSpacing(8)

        # Sections picker -- SmoothScrollArea with transparent background.
        self.viewLayout.addWidget(BodyLabel(tr("reshade.merge_sections_label"), self))
        scroll = SmoothScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(200)
        scroll.enableTransparentBackground()
        self._section_container = QWidget()
        self._section_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._section_container.setStyleSheet("background: transparent;")
        self._section_container_layout = QVBoxLayout(self._section_container)
        self._section_container_layout.setContentsMargins(8, 8, 8, 8)
        self._section_container_layout.setSpacing(6)
        scroll.setWidget(self._section_container)
        self.viewLayout.addWidget(scroll)

        # Advanced toggle: include non-fx sections (GENERAL, DEPTH, etc.).
        self._include_non_fx_cb = CheckBox(
            tr("reshade.merge_include_advanced"), self)
        self._include_non_fx_cb.setToolTip(
            tr("reshade.merge_include_advanced_tooltip"))
        self._include_non_fx_cb.stateChanged.connect(
            self._on_include_advanced_toggled)
        self.viewLayout.addWidget(self._include_non_fx_cb)
        self.viewLayout.addSpacing(8)

        # Output filename -- auto-populated with a sensible default.
        self.viewLayout.addWidget(BodyLabel(tr("reshade.merge_output_label"), self))
        self._name_edit = LineEdit(self)
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

        # Wider dialog for the scroll area to breathe.
        self.widget.setMinimumWidth(520)

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
        """Reload the section checkboxes when main or other dropdown changes."""
        assert self._section_container is not None

        # Clear existing checkboxes.
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

        # Reading can be slow-ish on huge presets but these are KB-sized files.
        self._main_sections = read_preset_for_merge(main_path)
        self._other_sections = read_preset_for_merge(other_path)

        # Display sections from Other. Effect (.fx) sections always show;
        # non-fx sections (e.g. [GENERAL], [DEPTH]) only when the advanced
        # toggle is on — those are power-user territory and most users just
        # want to combine effects between presets.
        any_row = False
        for section_name in self._other_sections:
            is_fx = section_name.lower().endswith(".fx")
            if not is_fx and not self._include_non_fx:
                continue
            pretty = section_name if not is_fx else section_name.removesuffix(".fx")
            overwrites = section_name in self._main_sections
            hint = tr("reshade.merge_will_overwrite") if overwrites \
                else tr("reshade.merge_will_add")
            cb = CheckBox(f"{pretty}  {hint}", self._section_container)
            self._section_container_layout.addWidget(cb)
            self._section_checks.append((section_name, cb))
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
