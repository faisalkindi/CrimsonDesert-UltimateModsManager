"""Preset and toggle picker dialogs for JSON mods."""

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QHBoxLayout, QCheckBox, QScrollArea, QWidget,
    QFrame,
)

logger = logging.getLogger(__name__)


def find_json_presets(path: Path) -> list[tuple[Path, dict]]:
    """Find all valid JSON patch files in a path.

    Returns list of (file_path, parsed_json) for each valid preset.
    """
    candidates = []

    if path.is_file() and path.suffix.lower() == ".json":
        candidates = [path]
    elif path.is_dir():
        candidates = sorted(path.glob("*.json"))
        if not candidates:
            candidates = sorted(path.glob("*/*.json"))

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


class PresetPickerDialog(QDialog):
    """Dialog for choosing which JSON preset to import."""

    def __init__(self, presets: list[tuple[Path, dict]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Mod Preset")
        self.setMinimumWidth(420)
        self.resize(460, 340)
        self._presets = presets
        self.selected_path: Path | None = None
        self.selected_data: dict | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QLabel("This mod has multiple presets.\nChoose which one to install:")
        header.setStyleSheet("font-size: 13px; color: #ECEFF4;")
        layout.addWidget(header)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background: #1A1D23; border: 1px solid #2E3440; "
            "border-radius: 6px; padding: 4px; }"
            "QListWidget::item { padding: 6px 8px; }"
            "QListWidget::item:selected { background: #2E3440; color: #D4A43C; }"
        )

        for file_path, data in presets:
            name = data.get("name", file_path.stem)
            desc = data.get("description", "")
            patch_count = sum(len(p.get("changes", [])) for p in data.get("patches", []))

            label = name
            if desc:
                label += f"  —  {desc[:60]}"
            label += f"  ({patch_count} changes)"

            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, file_path)
            self._list.addItem(item)

        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(90)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        ok_btn = QPushButton("Install")
        ok_btn.setFixedWidth(90)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(ok_btn)

        layout.addLayout(btn_row)

    def _on_accept(self) -> None:
        item = self._list.currentItem()
        if item:
            path = item.data(Qt.ItemDataRole.UserRole)
            for fp, data in self._presets:
                if fp == path:
                    self.selected_path = fp
                    self.selected_data = data
                    break
        self.accept()

    def _on_double_click(self, item: QListWidgetItem) -> None:
        self._on_accept()


def has_labeled_changes(data: dict) -> bool:
    """Check if a JSON patch mod has configurable options.

    Returns True for:
    - Grouped presets with [BracketPrefix] pattern (mutually exclusive)
    - 10+ labeled changes (clearly a toggle/customization mod)

    Regular mods with 1-9 labeled changes are just annotated, not configurable.
    """
    if _detect_preset_groups(data) is not None:
        return True

    # Count total labeled changes across all patches
    label_count = 0
    for patch in data.get("patches", []):
        for c in patch.get("changes", []):
            if isinstance(c, dict) and "label" in c:
                label_count += 1
    return label_count >= 10


def _detect_preset_groups(data: dict) -> dict[str, list[int]] | None:
    """Detect if patches represent mutually exclusive preset groups.

    Returns {group_name: [patch_indices]} if grouped presets found, None if
    independent toggles.

    Pattern: multiple patches targeting the same game_file, with labels
    starting with [GroupName].
    """
    import re
    patches = data.get("patches", [])
    if len(patches) < 2:
        return None

    # Check if multiple patches target the same file
    files = [p.get("game_file") for p in patches]
    if len(set(files)) > 1:
        return None  # different files = independent, not presets

    # Check for [GroupName] prefix pattern in labels
    groups: dict[str, list[int]] = {}
    for i, patch in enumerate(patches):
        changes = patch.get("changes", [])
        if not changes or "label" not in changes[0]:
            return None
        label = changes[0].get("label", "")
        match = re.match(r'\[([^\]]+)\]', label)
        if match:
            group_name = match.group(1)
            groups.setdefault(group_name, []).append(i)
        else:
            return None  # no group prefix = not grouped presets

    if len(groups) >= 2:
        return groups
    return None


class TogglePickerDialog(QDialog):
    """Dialog for picking which labeled changes to apply from a JSON mod.

    Handles two patterns:
    - Independent toggles: checkboxes for each change
    - Grouped presets: radio buttons for mutually exclusive groups
    """

    def __init__(self, data: dict, parent=None, previous_labels: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Choose What to Apply")
        self.setMinimumSize(450, 400)
        self.resize(500, 500)
        self._data = data
        self._previous = set(previous_labels) if previous_labels else None
        self.selected_data: dict | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        name = data.get("name", "Mod")
        desc = data.get("description", "")
        header = QLabel(f"<b>{name}</b>")
        header.setStyleSheet("font-size: 14px; color: #ECEFF4;")
        layout.addWidget(header)
        if desc:
            desc_label = QLabel(desc)
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet("color: #788090; font-size: 11px;")
            layout.addWidget(desc_label)

        if self._previous:
            prev_hint = QLabel(f"Previously selected: {len(self._previous)} items")
            prev_hint.setStyleSheet("color: #D4A43C; font-size: 11px;")
            layout.addWidget(prev_hint)

        # Detect which mode to use
        self._groups = _detect_preset_groups(data)

        if self._groups:
            self._build_preset_mode(layout)
        else:
            self._build_toggle_mode(layout)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(90)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("Apply Selected")
        ok_btn.setFixedWidth(110)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _build_preset_mode(self, layout):
        """Mutually exclusive presets — radio buttons."""
        from PySide6.QtWidgets import QRadioButton, QGroupBox

        hint = QLabel("Choose a preset:")
        hint.setStyleSheet("color: #D8DEE9; font-size: 12px;")
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background: #1A1D23; border: 1px solid #2E3440; border-radius: 6px; }")
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(12, 12, 12, 12)
        scroll_layout.setSpacing(8)

        self._radio_buttons: list[tuple] = []  # (radio, group_name, patch_indices)
        first = True
        for group_name, patch_indices in self._groups.items():
            # Collect details about this preset
            patches = self._data["patches"]
            detail_parts = []
            group_labels = []
            for idx in patch_indices:
                for c in patches[idx].get("changes", []):
                    label = c.get("label", "")
                    group_labels.append(label)
                    import re
                    clean = re.sub(r'^\[[^\]]+\]\s*', '', label)
                    if clean:
                        detail_parts.append(clean)

            radio = QRadioButton(f"{group_name}")
            radio.setStyleSheet("color: #D4A43C; font-size: 13px; font-weight: bold; padding: 4px;")
            # Pre-select based on previous choice
            if self._previous and any(l in self._previous for l in group_labels):
                radio.setChecked(True)
                first = False
            elif first:
                radio.setChecked(True)
                first = False
            scroll_layout.addWidget(radio)

            if detail_parts:
                detail = QLabel("  " + ", ".join(detail_parts))
                detail.setStyleSheet("color: #788090; font-size: 11px; padding-left: 20px;")
                detail.setWordWrap(True)
                scroll_layout.addWidget(detail)

            self._radio_buttons.append((radio, group_name, patch_indices))

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

    def _build_toggle_mode(self, layout):
        """Independent toggles — checkboxes."""
        hint = QLabel("Check the items you want to apply:")
        hint.setStyleSheet("color: #D8DEE9; font-size: 12px;")
        layout.addWidget(hint)

        sel_row = QHBoxLayout()
        sel_all = QPushButton("Select All")
        sel_all.setFixedWidth(90)
        sel_all.clicked.connect(self._select_all)
        sel_row.addWidget(sel_all)
        desel_all = QPushButton("Deselect All")
        desel_all.setFixedWidth(90)
        desel_all.clicked.connect(self._deselect_all)
        sel_row.addWidget(desel_all)
        sel_row.addStretch()
        layout.addLayout(sel_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background: #1A1D23; border: 1px solid #2E3440; border-radius: 6px; }")
        scroll_widget = QWidget()
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
                cb.setStyleSheet("color: #D8DEE9; padding: 2px;")
                scroll_layout.addWidget(cb)
                self._checkboxes.append((cb, change))

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        self._count_label = QLabel(f"{len(self._checkboxes)} items selected")
        self._count_label.setStyleSheet("color: #788090; font-size: 11px;")
        layout.addWidget(self._count_label)
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
            for radio, group_name, patch_indices in self._radio_buttons:
                if radio.isChecked():
                    selected_indices.update(patch_indices)
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
