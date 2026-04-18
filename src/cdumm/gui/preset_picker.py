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
        from qfluentwidgets import isDarkTheme

        # Explicit theme-aware stylesheet — without this the radio text
        # inherits an unstyled color and renders white-on-white in
        # light mode (bug reported against 3.1.31a on Vaxis LoD).
        _radio_style = (
            "QRadioButton { color: %s; padding: 8px; spacing: 8px; "
            "font-size: 14px; } "
            "QRadioButton::indicator { width: 16px; height: 16px; }"
        ) % ("#E2E8F0" if isDarkTheme() else "#1A202C")

        self._group = QButtonGroup(self)
        for i, v in enumerate(variants):
            radio = QRadioButton(prettify_mod_name(v.name))
            radio.setStyleSheet(_radio_style)
            rf = radio.font()
            rf.setPixelSize(14)
            radio.setFont(rf)
            if i == 0:
                radio.setChecked(True)
            self._group.addButton(radio, i)
            self.viewLayout.addWidget(radio)

        # Wire Install to record the chosen path before accepting. The
        # previous _on_yesButton_clicked method was never connected, so
        # selected_path stayed None even after clicking Install, and
        # callers silently treated that as Cancel.
        self.yesButton.setText(tr("main.install"))
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_yesButton_clicked)
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

        # Detect mutually exclusive groups the same way the cog side
        # panel does. Presets whose patches overlap the same bytes of
        # the same game_file can't coexist, so we render them as a
        # radio group (pick one). Presets that don't overlap anyone
        # get a plain checkbox (independent on/off).
        from cdumm.engine.variant_handler import detect_conflict_groups
        try:
            self._auto_group_ids = detect_conflict_groups(
                [d for _, d in presets])
        except Exception as _e:
            logger.debug("detect_conflict_groups failed: %s", _e)
            self._auto_group_ids = [-1] * len(presets)

        # Version-variant grouping: byte-range overlap misses presets
        # that target the same game_file but at different offsets
        # because the game binary shifted between builds (e.g. "Mod
        # 1.02.00.json" and "Mod 1.03.00.json" both patch the same
        # .paseq but at offsets 35824 vs 36019). Those are still
        # mutually exclusive. Detect them by stripping a trailing
        # version number from the filename stem — presets whose stems
        # collapse to the same base name get grouped together so the
        # dialog renders them as radio buttons.
        import re as _re
        _version_re = _re.compile(r"\s*[\-_]?\s*\d+(?:\.\d+){1,3}\s*$")
        _base_to_indices: dict[str, list[int]] = {}
        for i, (fp, _d) in enumerate(presets):
            base = _version_re.sub("", fp.stem).strip().lower()
            if base:
                _base_to_indices.setdefault(base, []).append(i)
        _next_gid = max(self._auto_group_ids, default=-1) + 1
        for base, idxs in _base_to_indices.items():
            if len(idxs) < 2:
                continue
            existing = {self._auto_group_ids[i] for i in idxs
                        if self._auto_group_ids[i] >= 0}
            if existing:
                target = min(existing)
            else:
                target = _next_gid
                _next_gid += 1
            for i in idxs:
                self._auto_group_ids[i] = target

        # Override toggle — when ON, every preset becomes a checkbox
        # regardless of detected conflicts. Lets users override the
        # auto-detect for mods whose author meant "pick any combo" but
        # the patches technically overlap.
        self._override_enabled = False

        has_any_conflict = any(g >= 0 for g in self._auto_group_ids)
        if has_any_conflict:
            self._override_btn = PushButton(
                tr("preset.override_allow_multi") or "Allow multi-select")
            if (tr("preset.override_allow_multi")
                    == "preset.override_allow_multi"):
                self._override_btn.setText("Allow multi-select")
            self._override_btn.setToolTip(
                "Auto-detect says some of these presets conflict "
                "(they edit the same bytes). Click to force multi-"
                "select anyway — only use this if the mod's author "
                "says independent picks are allowed.")
            self._override_btn.setCheckable(True)
            self._override_btn.clicked.connect(self._on_override_toggled)
            self.viewLayout.addWidget(self._override_btn)
            self.viewLayout.addSpacing(4)

        from qfluentwidgets import isDarkTheme
        self._is_dark = isDarkTheme()
        scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(SingleDirectionScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(200)
        scroll.setMaximumHeight(350)

        self._list_container = QWidget()
        if self._is_dark:
            self._list_container.setStyleSheet("QWidget { background: #1C2028; } "
                "QCheckBox, QRadioButton { color: #E2E8F0; padding: 10px; spacing: 8px; }"
                "QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }")
        else:
            self._list_container.setStyleSheet("QWidget { background: #FAFBFC; } "
                "QCheckBox, QRadioButton { color: #1A202C; padding: 10px; spacing: 8px; }"
                "QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }")
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(8, 8, 8, 8)
        self._list_layout.setSpacing(4)

        self._widgets: list[QCheckBox | QRadioButton] = []
        self._button_groups: dict[int, QButtonGroup] = {}
        self._rebuild_preset_list()

        scroll.setWidget(self._list_container)
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
            if idx < len(self._widgets):
                self._widgets[idx].setChecked(True)
                new_count = sum(
                    len(p.get("changes", []))
                    for p in dlg.selected_data.get("patches", []))
                name = dlg.selected_data.get("name", fp.stem)
                desc = dlg.selected_data.get("description", "")
                label = name
                if desc:
                    label += f"\n{desc[:80]}"
                label += f"\n{new_count} changes (customised)"
                self._widgets[idx].setText(label)

    def _on_accept(self) -> None:
        self.selected_presets = []
        for i, w in enumerate(self._widgets):
            if w.isChecked() and i < len(self._presets):
                fp, data = self._presets[i]
                self.selected_presets.append((fp, data))
        # Legacy compat: set first selected as primary
        if self.selected_presets:
            self.selected_path = self.selected_presets[0][0]
            self.selected_data = self.selected_presets[0][1]
        self.accept()

    def _rebuild_preset_list(self) -> None:
        """(Re)build the checkbox/radio list based on current override state.

        When override is OFF: respect auto-detected conflict groups
        (radio for grouped, checkbox for independent).
        When override is ON: every preset becomes a checkbox, no
        exclusivity enforced anywhere.
        """
        # Preserve current selection across rebuild so user doesn't lose
        # their picks when toggling override.
        prior_checked: set[int] = set()
        for i, w in enumerate(self._widgets):
            if w.isChecked():
                prior_checked.add(i)

        # Clear existing widgets and button groups
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for bg in self._button_groups.values():
            bg.setParent(None)
            bg.deleteLater()
        self._button_groups.clear()
        self._widgets.clear()

        # Force every preset to group=-1 (checkbox) when override is on
        effective_groups = (
            [-1] * len(self._presets)
            if self._override_enabled
            else list(self._auto_group_ids)
        )

        first_of_group_seen: set[int] = set()
        has_any_default = False

        for i, (file_path, data) in enumerate(self._presets):
            name = data.get("name", file_path.stem)
            desc = data.get("description", "")
            patch_count = sum(
                len(p.get("changes", [])) for p in data.get("patches", []))

            label = name
            if desc:
                label += f"\n{desc[:80]}"
            label += f"\n{patch_count} changes"

            gid = effective_groups[i] if i < len(effective_groups) else -1
            if gid >= 0:
                rb = QRadioButton(label)
                rf = rb.font()
                rf.setPixelSize(13)
                rb.setFont(rf)
                bg = self._button_groups.get(gid)
                if bg is None:
                    bg = QButtonGroup(self)
                    bg.setExclusive(True)
                    self._button_groups[gid] = bg
                bg.addButton(rb)
                if i in prior_checked:
                    rb.setChecked(True)
                    has_any_default = True
                elif gid not in first_of_group_seen and not prior_checked:
                    rb.setChecked(True)
                    first_of_group_seen.add(gid)
                    has_any_default = True
                self._widgets.append(rb)
                self._list_layout.addWidget(
                    self._wrap_with_customize(rb, i, data))
            else:
                cb = QCheckBox(label)
                cbf = cb.font()
                cbf.setPixelSize(13)
                cb.setFont(cbf)
                if i in prior_checked:
                    cb.setChecked(True)
                    has_any_default = True
                elif (not has_any_default and not self._button_groups
                        and not prior_checked):
                    cb.setChecked(True)
                    has_any_default = True
                self._widgets.append(cb)
                self._list_layout.addWidget(
                    self._wrap_with_customize(cb, i, data))

        self._list_layout.addStretch()

    def _wrap_with_customize(self, widget, idx: int, data: dict):
        """Wrap a preset row with a per-preset Customize button when the
        preset has labelled toggles. Lets power users fine-tune which
        individual changes apply without touching other presets
        (butanokaabii's request, restored from v3.0.5 master)."""
        if not has_labeled_changes(data):
            return widget
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        row_layout.addWidget(widget, 1)
        btn = PushButton(tr("preset.customize"))
        btn.setFixedHeight(28)
        bf = btn.font()
        bf.setPixelSize(12)
        btn.setFont(bf)
        btn.clicked.connect(
            lambda _checked=False, i=idx: self._on_customize(i))
        row_layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _on_override_toggled(self) -> None:
        """Flip override state and rebuild the preset list."""
        self._override_enabled = self._override_btn.isChecked()
        if self._override_enabled:
            self._override_btn.setText("Revert to auto-detect")
        else:
            self._override_btn.setText("Allow multi-select")
        self._rebuild_preset_list()


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
