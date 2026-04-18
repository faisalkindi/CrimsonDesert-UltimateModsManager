"""Preset and toggle picker dialogs for JSON mods."""

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup, QVBoxLayout, QListWidget, QListWidgetItem,
    QHBoxLayout, QCheckBox, QRadioButton, QWidget,
    QFrame,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SingleDirectionScrollArea,
    SubtitleLabel,
)

from cdumm.i18n import tr

logger = logging.getLogger(__name__)


def find_json_presets(path: Path) -> list[tuple[Path, dict]]:
    """Find all valid JSON patch files in a path.

    Returns list of (file_path, parsed_json) for each valid preset.

    Optimisation: skip parsing entirely when there's only one .json
    candidate. The caller only shows the preset picker for ``> 1``
    presets, so for single-AIO-JSON folders (the common case for
    mods like 0xNobody's stamina-and-spirit pack with 4000+ offsets)
    we'd otherwise burn 10s+ on the GUI thread parsing a file the
    picker won't display anyway. The import worker re-parses on its
    own thread via :func:`detect_json_patch` later.
    """
    if path.is_file() and path.suffix.lower() == ".json":
        candidates = [path]
    elif path.is_dir():
        candidates = sorted(path.glob("*.json"))
        if not candidates:
            candidates = sorted(path.glob("*/*.json"))
    else:
        candidates = []

    if len(candidates) <= 1:
        # Picker isn't shown for ≤1 preset; don't pay the parse cost
        # on the GUI thread. Return empty so the caller proceeds
        # straight to import_handler, which parses on a worker.
        return []

    presets = []
    for f in candidates:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if (isinstance(data, dict)
                    and "patches" in data
                    and isinstance(data["patches"], list)
                    and len(data["patches"]) > 0
                    and "game_file" in data["patches"][0]
                    and "changes" in data["patches"][0]):
                presets.append((f, data))
        except Exception:
            continue

    return presets


def find_folder_variants(path: Path) -> list[Path]:
    """Find folder-based mod variants (subdirectories that each contain a mod).

    Detects patterns like:
        ModName/
            VariantA/   (contains .paz, .pamt, .json, or numbered dirs)
            VariantB/

    Returns list of variant folder paths, or empty if not a variant mod.
    Ignores numbered PAZ directories (0001/, 0036/) — those are game data, not variants.
    """
    if not path.is_dir():
        return []

    subdirs = sorted([
        d for d in path.iterdir()
        if d.is_dir() and not d.name.startswith('.') and not d.name.startswith('_')
    ])

    if len(subdirs) < 2:
        return []

    # Check if ALL subdirs are numbered PAZ dirs (like 0002/, 0012/) — not variants
    import re
    if all(re.match(r'^\d{4}$', d.name) for d in subdirs):
        return []

    # Check if subdirs look like mod variants (contain game files)
    variants = []
    for d in subdirs:
        has_content = False
        for f in d.rglob("*"):
            if f.is_file() and f.suffix.lower() in ('.paz', '.pamt', '.json', '.bsdiff'):
                has_content = True
                break
            if f.is_dir() and re.match(r'^\d{4}$', f.name):
                has_content = True
                break
        if has_content:
            variants.append(d)

    return variants if len(variants) >= 2 else []


class FolderVariantDialog(MessageBoxBase):
    """Dialog for choosing which folder variant to import."""

    def __init__(self, variants: list[Path], parent=None):
        super().__init__(parent)
        self._variants = variants
        self.selected_path: Path | None = None

        title = SubtitleLabel(tr("preset.choose"))
        tf = title.font()
        tf.setPixelSize(20)
        tf.setWeight(QFont.Weight.Bold)
        title.setFont(tf)
        self.viewLayout.addWidget(title)
        self.viewLayout.addWidget(
            CaptionLabel(tr("preset.choose_desc")))
        self.viewLayout.addSpacing(12)

        from cdumm.engine.import_handler import prettify_mod_name

        self._group = QButtonGroup(self)
        for i, v in enumerate(variants):
            radio = QRadioButton(prettify_mod_name(v.name))
            rf = radio.font()
            rf.setPixelSize(14)
            radio.setFont(rf)
            if i == 0:
                radio.setChecked(True)
            self._group.addButton(radio, i)
            self.viewLayout.addWidget(radio)

        self.yesButton.setText(tr("main.install"))
        self.cancelButton.setText(tr("main.cancel"))
        self.widget.setMinimumWidth(400)

    def _on_yesButton_clicked(self):
        idx = self._group.checkedId()
        if 0 <= idx < len(self._variants):
            self.selected_path = self._variants[idx]
        self.accept()


class PresetPickerDialog(MessageBoxBase):
    """Dialog for choosing which JSON presets to import (multi-select)."""

    def __init__(self, presets: list[tuple[Path, dict]], parent=None):
        super().__init__(parent)
        self._presets = presets
        # Legacy single-select compat
        self.selected_path: Path | None = None
        self.selected_data: dict | None = None
        # Multi-select results
        self.selected_presets: list[tuple[Path, dict]] = []

        title = SubtitleLabel(tr("preset.choose"))
        tf = title.font()
        tf.setPixelSize(20)
        tf.setWeight(QFont.Weight.Bold)
        title.setFont(tf)
        self.viewLayout.addWidget(title)
        self.viewLayout.addSpacing(4)

        header = CaptionLabel(tr("preset.choose_desc"))
        hf = header.font()
        hf.setPixelSize(13)
        header.setFont(hf)
        header.setWordWrap(True)
        self.viewLayout.addWidget(header)
        self.viewLayout.addSpacing(8)

        # Checkbox list inside a scroll area (multi-select)
        from qfluentwidgets import isDarkTheme
        scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(SingleDirectionScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(200)
        scroll.setMaximumHeight(350)

        container = QWidget()
        if isDarkTheme():
            container.setStyleSheet("QWidget { background: #1C2028; } "
                "QCheckBox { color: #E2E8F0; padding: 10px; spacing: 8px; }"
                "QCheckBox::indicator { width: 16px; height: 16px; }")
        else:
            container.setStyleSheet("QWidget { background: #FAFBFC; } "
                "QCheckBox { color: #1A202C; padding: 10px; spacing: 8px; }"
                "QCheckBox::indicator { width: 16px; height: 16px; }")
        cb_layout = QVBoxLayout(container)
        cb_layout.setContentsMargins(8, 8, 8, 8)
        cb_layout.setSpacing(4)

        self._checkboxes: list[QCheckBox] = []
        # Mutable copy of each preset's parsed data — when the user
        # opens "Customize" and unchecks individual entries, we replace
        # the preset's dict in-place so the import flow uses the
        # filtered version. Preserving the file path for diagnostics.
        self._presets: list[tuple[Path, dict]] = list(presets)
        for i, (file_path, data) in enumerate(self._presets):
            name = data.get("name", file_path.stem)
            desc = data.get("description", "")
            patch_count = sum(len(p.get("changes", [])) for p in data.get("patches", []))

            label = name
            if desc:
                label += f"\n{desc[:80]}"
            label += f"\n{patch_count} changes"

            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            cb = QCheckBox(label)
            cbf = cb.font()
            cbf.setPixelSize(13)
            cb.setFont(cbf)
            cb.setChecked(i == 0)  # first one checked by default
            self._checkboxes.append(cb)
            row_layout.addWidget(cb, 1)

            # "Customize" button — shows TogglePickerDialog for this
            # specific preset so power users can fine-tune which
            # individual changes apply (butanokaabii's request).
            # Hidden when the preset has no labelled changes.
            if has_labeled_changes(data):
                customize_btn = PushButton(tr("preset.customize"))
                customize_btn.setFixedHeight(28)
                cf = customize_btn.font()
                cf.setPixelSize(12)
                customize_btn.setFont(cf)
                customize_btn.clicked.connect(
                    lambda _checked, idx=i: self._on_customize(idx))
                row_layout.addWidget(customize_btn, 0,
                                     Qt.AlignmentFlag.AlignTop)

            cb_layout.addWidget(row_widget)

        cb_layout.addStretch()
        scroll.setWidget(container)
        self.viewLayout.addWidget(scroll)

        # Override default buttons
        self.yesButton.setText(tr("main.install"))
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_accept)
        self.cancelButton.setText(tr("main.cancel"))

        self.widget.setMinimumWidth(500)

    def _on_customize(self, idx: int) -> None:
        """Open the TogglePickerDialog for one preset so the user can
        fine-tune which labelled changes apply. Result replaces the
        preset's data in-place so :meth:`_on_accept` picks up the
        filtered version.
        """
        if idx >= len(self._presets):
            return
        fp, data = self._presets[idx]
        dlg = TogglePickerDialog(data, parent=self)
        accepted = dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
        if accepted and dlg.selected_data:
            self._presets[idx] = (fp, dlg.selected_data)
            self._checkboxes[idx].setChecked(True)
            new_count = sum(
                len(p.get("changes", []))
                for p in dlg.selected_data.get("patches", []))
            name = dlg.selected_data.get("name", fp.stem)
            desc = dlg.selected_data.get("description", "")
            label = name
            if desc:
                label += f"\n{desc[:80]}"
            label += f"\n{new_count} changes (customised)"
            self._checkboxes[idx].setText(label)

    def _on_accept(self) -> None:
        self.selected_presets = []
        for i, cb in enumerate(self._checkboxes):
            if cb.isChecked() and i < len(self._presets):
                fp, data = self._presets[i]
                self.selected_presets.append((fp, data))
        # Legacy compat: set first selected as primary
        if self.selected_presets:
            self.selected_path = self.selected_presets[0][0]
            self.selected_data = self.selected_presets[0][1]
        self.accept()


def has_labeled_changes(data: dict) -> bool:
    """Check if a JSON patch mod has configurable options.

    Returns True for:
    1. Grouped presets with [BracketPrefix] pattern (radio buttons)
    2. Mods with 2+ labeled changes that represent independent options

    Does NOT trigger for mods where all changes share the same bracket
    prefix (like [Trust] Talk Gain x2, [Trust] Talk Gain x2). Those are
    parts of one feature, not separate toggles.
    """
    import re
    if _detect_preset_groups(data) is not None:
        return True
    # Collect all labels across all patches
    labels = []
    for patch in data.get("patches", []):
        for change in patch.get("changes", []):
            if "label" in change:
                labels.append(change["label"])
    if len(labels) < 2:
        return False
    # Check bracket prefixes
    prefixes = set()
    has_any_bracket = False
    for label in labels:
        match = re.match(r'\[([^\]]+)\]', label)
        if match:
            prefixes.add(match.group(1))
            has_any_bracket = True
    # All same bracket prefix = one feature, not toggleable
    if has_any_bracket and len(prefixes) <= 1:
        return False
    # Multiple distinct bracket groups, but only if all patches target
    # the SAME game file. Different game files = different components
    # that need to be installed together (like LET ME SLEEP's sleep_left
    # + sleep_right), not independent options.
    if has_any_bracket and len(prefixes) >= 2:
        game_files = set()
        for patch in data.get("patches", []):
            gf = patch.get("game_file")
            if gf:
                game_files.add(gf)
        if len(game_files) <= 1:
            return True
        return False  # multiple game files = not configurable
    # Plain labels (no brackets): only show toggle for mods with many
    # changes (10+), suggesting a mod with lots of independent options.
    # Small numbers of plain labels are just descriptions, not toggles.
    if len(labels) >= 10:
        return True
    return False


def _detect_preset_groups(data: dict) -> dict[str, list[int]] | None:
    """Detect if patches represent mutually exclusive preset groups.

    Returns {group_name: [patch_indices]} if grouped presets found, None if
    independent toggles.

    Supports two patterns:
    1. Multiple patches targeting the same game_file, each with [GroupName] labels
    2. Single patch with changes labeled [GroupName] — groups changes by prefix

    For pattern 2, returns negative indices (-1, -2, ...) as sentinel values
    so _on_accept knows to filter changes within the patch, not filter patches.
    """
    import re
    patches = data.get("patches", [])
    if not patches:
        return None

    # Pattern 1: multiple patches with bracket prefixes
    if len(patches) >= 2:
        files = [p.get("game_file") for p in patches]
        if len(set(files)) == 1:
            groups: dict[str, list[int]] = {}
            all_have_prefix = True
            for i, patch in enumerate(patches):
                changes = patch.get("changes", [])
                if not changes or "label" not in changes[0]:
                    all_have_prefix = False
                    break
                label = changes[0].get("label", "")
                match = re.match(r'\[([^\]]+)\]', label)
                if match:
                    groups.setdefault(match.group(1), []).append(i)
                else:
                    all_have_prefix = False
                    break
            if all_have_prefix and len(groups) >= 2:
                return groups

    # Pattern 2 removed: single-patch bracket labels should use toggle mode
    # (checkboxes) not preset mode (radio buttons), since categories like
    # [Swimming], [Flying], [Combat] are independent, not mutually exclusive.

    return None


class TogglePickerDialog(MessageBoxBase):
    """Dialog for picking which labeled changes to apply from a JSON mod.

    Handles two patterns:
    - Independent toggles: checkboxes for each change
    - Grouped presets: radio buttons for mutually exclusive groups
    """

    def __init__(self, data: dict, parent=None, previous_labels: list[str] | None = None):
        super().__init__(parent)
        self._data = data
        self._previous = set(previous_labels) if previous_labels else None
        self.selected_data: dict | None = None

        self.titleLabel = SubtitleLabel(tr("preset.choose_apply"))
        self.viewLayout.addWidget(self.titleLabel)

        name = data.get("name", "Mod")
        desc = data.get("description", "")
        name_label = BodyLabel(name)
        font = name_label.font()
        font.setPixelSize(14)
        font.setBold(True)
        name_label.setFont(font)
        self.viewLayout.addWidget(name_label)

        if desc:
            desc_label = CaptionLabel(desc)
            desc_label.setWordWrap(True)
            self.viewLayout.addWidget(desc_label)

        if self._previous:
            prev_hint = CaptionLabel(f"Previously selected: {len(self._previous)} items")
            self.viewLayout.addWidget(prev_hint)

        # Detect which mode to use
        self._groups = _detect_preset_groups(data)

        if self._groups:
            self._build_preset_mode()
        else:
            self._build_toggle_mode()

        # Override default buttons
        self.yesButton.setText(tr("preset.apply_selected"))
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_accept)
        self.cancelButton.setText(tr("main.cancel"))

        self.widget.setMinimumWidth(500)

    def _build_preset_mode(self):
        """Mutually exclusive presets — radio buttons."""
        from PySide6.QtWidgets import QRadioButton

        hint = BodyLabel(tr("preset.choose_preset"))
        self.viewLayout.addWidget(hint)

        from qfluentwidgets import isDarkTheme
        scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(SingleDirectionScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(250)
        scroll_widget = QWidget()
        if isDarkTheme():
            scroll_widget.setStyleSheet("QWidget { background: #1C2028; } "
                "QRadioButton { color: #E2E8F0; padding: 10px; }")
        else:
            scroll_widget.setStyleSheet("QWidget { background: #FAFBFC; } "
                "QRadioButton { color: #1A202C; padding: 10px; }")
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(12, 12, 12, 12)
        scroll_layout.setSpacing(8)

        self._radio_buttons: list[tuple] = []  # (radio, group_name, indices)
        first = True
        for group_name, indices in self._groups.items():
            # Collect details about this preset
            patches = self._data["patches"]
            detail_parts = []
            group_labels = []
            for idx in indices:
                for c in patches[idx].get("changes", []):
                    label = c.get("label", "")
                    group_labels.append(label)
                    import re
                    clean = re.sub(r'^\[[^\]]+\]\s*', '', label)
                    if clean:
                        detail_parts.append(clean)

            radio = QRadioButton(f"{group_name}")
            radio_font = radio.font()
            radio_font.setPixelSize(13)
            radio_font.setBold(True)
            radio.setFont(radio_font)
            # Pre-select based on previous choice
            if self._previous and any(l in self._previous for l in group_labels):
                radio.setChecked(True)
                first = False
            elif first:
                radio.setChecked(True)
                first = False
            scroll_layout.addWidget(radio)

            if detail_parts:
                MAX_SHOWN = 3
                if len(detail_parts) <= MAX_SHOWN:
                    summary = ", ".join(detail_parts)
                else:
                    summary = ", ".join(detail_parts[:MAX_SHOWN]) + f"  (+{len(detail_parts) - MAX_SHOWN} more)"
                detail = CaptionLabel("  " + summary)
                detail.setWordWrap(True)
                scroll_layout.addWidget(detail)

            self._radio_buttons.append((radio, group_name, indices))

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        self.viewLayout.addWidget(scroll)

    def _build_toggle_mode(self):
        """Independent toggles — checkboxes."""
        hint = BodyLabel(tr("preset.check_items"))
        self.viewLayout.addWidget(hint)

        sel_row = QHBoxLayout()
        sel_all = PushButton(tr("preset.select_all"))
        sel_all.setFixedWidth(90)
        sel_all.clicked.connect(self._select_all)
        sel_row.addWidget(sel_all)
        desel_all = PushButton(tr("preset.deselect_all"))
        desel_all.setFixedWidth(100)
        desel_all.clicked.connect(self._deselect_all)
        sel_row.addWidget(desel_all)
        sel_row.addStretch()
        self.viewLayout.addLayout(sel_row)

        from qfluentwidgets import isDarkTheme
        scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(SingleDirectionScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(250)
        scroll_widget = QWidget()
        if isDarkTheme():
            scroll_widget.setStyleSheet("QWidget { background: #1C2028; } "
                "QCheckBox { color: #E2E8F0; padding: 8px; }")
        else:
            scroll_widget.setStyleSheet("QWidget { background: #FAFBFC; } "
                "QCheckBox { color: #1A202C; padding: 8px; }")
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(8, 8, 8, 8)
        scroll_layout.setSpacing(4)

        self._checkboxes: list[tuple[QCheckBox, dict]] = []
        for patch in self._data.get("patches", []):
            for change in patch.get("changes", []):
                label = change.get("label", f"offset {change.get('offset', '?')}")
                cb = QCheckBox(label)
                # Pre-select based on previous choice, or all if first time
                if self._previous is not None:
                    cb.setChecked(label in self._previous)
                else:
                    cb.setChecked(True)
                scroll_layout.addWidget(cb)
                self._checkboxes.append((cb, change))

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        self.viewLayout.addWidget(scroll)

        self._count_label = CaptionLabel(f"{len(self._checkboxes)} items selected")
        self.viewLayout.addWidget(self._count_label)
        for cb, _ in self._checkboxes:
            cb.toggled.connect(self._update_count)

    def _select_all(self):
        for cb, _ in self._checkboxes:
            cb.setChecked(True)

    def _deselect_all(self):
        for cb, _ in self._checkboxes:
            cb.setChecked(False)

    def _update_count(self):
        count = sum(1 for cb, _ in self._checkboxes if cb.isChecked())
        self._count_label.setText(f"{count} of {len(self._checkboxes)} items selected")

    def _on_accept(self):
        import copy
        filtered = copy.deepcopy(self._data)

        if self._groups:
            # Preset mode — keep only the selected group's patches
            selected_indices = set()
            for radio, group_name, indices in self._radio_buttons:
                if radio.isChecked():
                    selected_indices.update(indices)
            filtered["patches"] = [
                p for i, p in enumerate(filtered["patches"])
                if i in selected_indices
            ]
        else:
            # Toggle mode — keep only checked changes
            selected_changes = [change for cb, change in self._checkboxes if cb.isChecked()]
            if not selected_changes:
                return
            selected_keys = {(c.get("offset"), c.get("label")) for c in selected_changes}
            for patch in filtered["patches"]:
                patch["changes"] = [
                    c for c in patch.get("changes", [])
                    if (c.get("offset"), c.get("label")) in selected_keys
                ]

        self.selected_data = filtered
        self.accept()
