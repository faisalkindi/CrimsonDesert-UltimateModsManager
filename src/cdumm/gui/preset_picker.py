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
        import re as _re_nnnn
        _NNNN = _re_nnnn.compile(r"^\d{4}$")

        def _legit(p: Path) -> bool:
            try:
                rel = p.relative_to(path)
            except ValueError:
                return False
            return not any(
                _NNNN.match(part) or part.lower() == "meta"
                for part in rel.parts[:-1]
            )

        # Try depth 1, then depth 2, then a full rglob. First non-empty
        # result wins so the fast path stays fast for normal layouts.
        # Same NNNN/meta filter detect_json_patches_all uses so the
        # picker doesn't false-positive on extracted vanilla content.
        candidates = [p for p in sorted(path.glob("*.json")) if _legit(p)]
        if not candidates:
            candidates = [p for p in sorted(path.glob("*/*.json")) if _legit(p)]
        if not candidates:
            candidates = [p for p in sorted(path.rglob("*.json")) if _legit(p)]
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

    import re
    subdirs = sorted([
        d for d in path.iterdir()
        if d.is_dir() and not d.name.startswith('.') and not d.name.startswith('_')
        # Exclude NNNN-numbered game-data directories and the 'meta'
        # companion. These are destination folders the import worker
        # writes to (Barber Unlocked, Enhanced Maps etc. produce a
        # sources/<id>/0009/ + 0036/ + meta/ tree). Without this
        # filter find_folder_variants treats 0009 and 0036 as
        # 'variants' because they each contain a 0.paz, and the mod
        # gets a phantom cog post-import.
        and not re.match(r'^\d{4}$', d.name)
        and d.name.lower() != 'meta'
    ])

    if len(subdirs) < 2:
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


def folder_variant_game_files(folders: list[Path]) -> dict[Path, set[str]]:
    """For each folder, collect the set of game_file targets its JSONs
    declare. Used by the picker to decide whether the folders behave
    like mutually-exclusive variants (overlapping targets) or
    independent categories (disjoint targets).

    Folders without JSONs contribute an empty set; they do not force
    mutex mode because nothing they touch collides with anything.
    """
    result: dict[Path, set[str]] = {}
    for folder in folders:
        targets: set[str] = set()
        if folder.is_dir():
            for jp in folder.rglob("*.json"):
                try:
                    data = json.loads(jp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                patches = data.get("patches")
                if not isinstance(patches, list):
                    continue
                for p in patches:
                    if isinstance(p, dict):
                        gf = p.get("game_file")
                        if isinstance(gf, str) and gf:
                            targets.add(gf)
        result[folder] = targets
    return result


def folders_are_independent(gf_map: dict[Path, set[str]]) -> bool:
    """True if no two non-empty folder target-sets share any game_file.

    Empty folders (unparseable JSONs, docs/readme siblings) USED to be
    treated as "never conflict" which let a single malformed folder
    flip the whole archive into checkbox mode — and the mutex folders
    got silently drop-at-byte-level at apply time. E4 fix: require at
    least 2 NON-EMPTY folders to consider the archive 'independent'
    at all. If only one folder has byte targets, the 'independent'
    check is moot — there's nothing to conflict WITH.
    """
    non_empty = [t for t in gf_map.values() if t]
    if len(non_empty) < 2:
        # With fewer than 2 target-bearing folders, there's no
        # meaningful 'independent' decision to make — default to the
        # safer mutex (radio) mode.
        return False
    seen: set[str] = set()
    for targets in non_empty:
        if seen & targets:
            return False
        seen.update(targets)
    return True


class FolderVariantDialog(MessageBoxBase):
    """Dialog for choosing which folder variant(s) to import.

    Two modes:
      * Mutually exclusive (radio group, single pick) — when folders'
        game_file targets overlap. Vaxis LoD, Character Creator, any
        real "variant" mod.
      * Independent (checkbox group, multi pick) — when folders target
        disjoint game files. Mega-packs like GildsGear where each
        folder is an independent category.

    ``selected_paths`` always exposes the list of picked paths. The
    legacy ``selected_path`` attribute points at the first pick (back-
    compat for callers that haven't migrated yet).
    """

    def __init__(self, variants: list[Path], parent=None):
        super().__init__(parent)
        self._variants = variants
        self.selected_path: Path | None = None
        self.selected_paths: list[Path] = []

        # Decide single-pick (radio) vs multi-pick (checkbox) based on
        # whether the folders' JSON targets overlap. GildsGear-style
        # category packs go to checkbox mode; Vaxis-style variants stay
        # on radio. If detection fails for any reason, default to the
        # safe radio mode so existing workflows don't regress.
        try:
            gf_map = folder_variant_game_files(variants)
            self._multi_select = folders_are_independent(gf_map)
        except Exception as e:  # pragma: no cover
            logger.debug("folder-independence detection failed: %s", e)
            self._multi_select = False

        # Mode-specific copy: radio mode told the user to "Check ALL
        # you want installed" which was a lie — they could only pick
        # one. Split the strings so the text matches the actual UI.
        title = SubtitleLabel(
            tr("preset.choose_many") if self._multi_select
            else tr("preset.choose_one"))
        tf = title.font()
        tf.setPixelSize(20)
        tf.setWeight(QFont.Weight.Bold)
        title.setFont(tf)
        self.viewLayout.addWidget(title)
        desc_label = CaptionLabel(
            tr("preset.choose_desc_many") if self._multi_select
            else tr("preset.choose_one_desc"))
        desc_label.setWordWrap(True)
        self.viewLayout.addWidget(desc_label)
        self.viewLayout.addSpacing(12)

        from cdumm.engine.import_handler import prettify_mod_name
        from qfluentwidgets import isDarkTheme

        # Explicit theme-aware stylesheet — without this the radio text
        # inherits an unstyled color and renders white-on-white in
        # light mode (bug reported against 3.1.31a on Vaxis LoD).
        # Also setAutoFillBackground + an explicit widget background
        # to prevent the 'black dialog' rendering bug users have
        # occasionally hit on Windows 11 + PySide6 6.11 (Qt forum
        # thread: 'Most widgets obscured, covered up by black' —
        # the fix is always the same: set a background).
        _is_dark = isDarkTheme()
        _bg = "#1C2028" if _is_dark else "#FAFBFC"
        _fg = "#E2E8F0" if _is_dark else "#1A202C"
        _button_style = (
            "QRadioButton, QCheckBox { color: %s; padding: 8px; "
            "spacing: 8px; font-size: 14px; } "
            "QRadioButton::indicator, QCheckBox::indicator { "
            "width: 16px; height: 16px; }"
        ) % _fg
        try:
            self.widget.setAutoFillBackground(True)
            self.widget.setStyleSheet(
                f"QWidget {{ background: {_bg}; color: {_fg}; }}")
        except AttributeError:
            pass

        self._widgets: list[QCheckBox | QRadioButton] = []
        if self._multi_select:
            # Category-pack mode: each folder is an independent pick.
            for i, v in enumerate(variants):
                cb = QCheckBox(prettify_mod_name(v.name))
                cb.setStyleSheet(_button_style)
                cf = cb.font()
                cf.setPixelSize(14)
                cb.setFont(cf)
                cb.setChecked(True)   # default: install everything
                self._widgets.append(cb)
                self.viewLayout.addWidget(cb)
        else:
            # Variant mode: existing radio-group behaviour.
            self._group = QButtonGroup(self)
            for i, v in enumerate(variants):
                radio = QRadioButton(prettify_mod_name(v.name))
                radio.setStyleSheet(_button_style)
                rf = radio.font()
                rf.setPixelSize(14)
                radio.setFont(rf)
                if i == 0:
                    radio.setChecked(True)
                self._group.addButton(radio, i)
                self._widgets.append(radio)
                self.viewLayout.addWidget(radio)

        # Wire Install to record the chosen path before accepting. The
        # previous _on_yesButton_clicked method was never connected, so
        # selected_path stayed None even after clicking Install, and
        # callers silently treated that as Cancel.
        self.yesButton.setText(tr("main.install"))
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_yesButton_clicked)
        self.cancelButton.setText(tr("main.cancel"))
        self.widget.setMinimumWidth(440)

    def _on_yesButton_clicked(self):
        picks: list[Path] = []
        if self._multi_select:
            for i, w in enumerate(self._widgets):
                if w.isChecked():
                    picks.append(self._variants[i])
            if not picks:
                # Nothing checked — treat as cancel so we don't import
                # an empty pack.
                return
        else:
            idx = self._group.checkedId()
            if 0 <= idx < len(self._variants):
                picks = [self._variants[idx]]
        self.selected_paths = picks
        self.selected_path = picks[0] if picks else None
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


def _detect_mutex_offset_groups(data: dict) -> list[dict] | None:
    """Detect changes that target the same (game_file, offset) — mutex presets.

    When a mod ships multiple labeled changes at the same byte offset
    (the Unlimited Dragon Flying case: five "Ride Duration" options at
    offset 21860562), only one can apply at a time — they overwrite
    each other. The toggle picker should present these as radio
    buttons, not checkboxes, so the user picks exactly one.

    Returns a list of group descriptors:
        [{"key": "Ride Duration", "changes": [change_dict, ...],
          "patch_idx": i, "group_label": "Ride Duration"}, ...]
    or None if no mutex groups found.

    A "group" is 2+ changes sharing the same numeric offset within one
    patch. Changes with distinct offsets stay independent (checkboxes).
    """
    groups: list[dict] = []
    for pidx, patch in enumerate(data.get("patches", [])):
        changes = patch.get("changes", [])
        # Preserve both the change dict AND its stable (patch_idx, change_idx)
        # key in each bucket so downstream renderers can filter via indices
        # rather than id() (HIGH #13: id() may alias after GC).
        by_offset: dict[int, list[tuple[dict, tuple[int, int]]]] = {}
        for cidx, ch in enumerate(changes):
            raw = ch.get("offset")
            if raw is None or "label" not in ch:
                continue
            try:
                off = int(raw, 0) if isinstance(raw, str) else int(raw)
            except (ValueError, TypeError):
                continue
            by_offset.setdefault(off, []).append((ch, (pidx, cidx)))
        for off, entries in by_offset.items():
            if len(entries) < 2:
                continue
            ch_list = [e[0] for e in entries]
            key_list = [e[1] for e in entries]
            labels = [c.get("label", "") for c in ch_list]
            group_label = _common_label_prefix(labels) or f"Offset 0x{off:X}"
            groups.append({
                "key": f"{patch.get('game_file', '')}:{off}",
                "group_label": group_label,
                "changes": ch_list,
                "change_keys": key_list,
                "patch_idx": pidx,
            })
    return groups if groups else None


def _filter_patches_by_keys(data: dict,
                            selected_keys: set[tuple[int, int]]) -> dict:
    """Return a deep copy of `data` keeping only changes whose
    (patch_idx, change_idx) tuple is in `selected_keys`.

    Used by the toggle-mode Apply handler. Index-based keys survive
    deepcopy, JSON round-trips, and GC recycling — unlike id().

    Content-hash tiebreaker: keys may optionally carry a 3rd element
    — a short hash of the change's (offset, original, patched) tuple
    captured at pick time. If the hash is present AND doesn't match
    the change currently at that index, the key is ignored (a silent
    position mismatch is logged as a warning). Protects against
    callers re-sorting or re-emitting patches between pick and
    filter. E5 defensive.
    """
    import copy as _copy
    out = _copy.deepcopy(data)
    # Split keys into pure-index vs indexed-with-hash so the filter
    # can validate the hashed ones.
    index_only: set[tuple[int, int]] = set()
    index_with_hash: dict[tuple[int, int], str] = {}
    for k in selected_keys:
        if len(k) >= 3:
            index_with_hash[(int(k[0]), int(k[1]))] = str(k[2])
        else:
            index_only.add((int(k[0]), int(k[1])))
    kept_patches: list[dict] = []
    for p_idx, patch in enumerate(data.get("patches", [])):
        kept_changes: list[dict] = []
        for c_idx, change in enumerate(patch.get("changes", [])):
            key = (p_idx, c_idx)
            if key in index_only:
                kept_changes.append(_copy.deepcopy(change))
            elif key in index_with_hash:
                expected = index_with_hash[key]
                actual = _change_content_hash(change)
                if actual == expected:
                    kept_changes.append(_copy.deepcopy(change))
                else:
                    logger.warning(
                        "preset filter: change at (%d,%d) content-hash "
                        "mismatch (expected %s got %s) — skipping. "
                        "Upstream likely re-sorted the patches list.",
                        p_idx, c_idx, expected, actual)
        if kept_changes:
            new_patch = _copy.deepcopy(patch)
            new_patch["changes"] = kept_changes
            kept_patches.append(new_patch)
    out["patches"] = kept_patches
    return out


def _change_content_hash(change: dict) -> str:
    """Short stable hash of a change's identity fields.

    Uses offset + original + patched since label can repeat. Not a
    security primitive; just a tiebreaker for position-key validation.
    """
    import hashlib
    parts = (
        str(change.get("offset", "")),
        str(change.get("original", "")),
        str(change.get("patched", "")),
    )
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def _common_label_prefix(labels: list[str]) -> str:
    """Pull a human group title from 2+ labels that share a common prefix.

    'Ride Duration: 30 Minutes' + 'Ride Duration: 60 Minutes' → 'Ride Duration'.
    Falls back to empty string if labels don't share a clean separator.
    """
    if not labels:
        return ""
    for sep in (": ", " - ", " – "):
        prefixes = {lbl.split(sep, 1)[0] for lbl in labels if sep in lbl}
        if len(prefixes) == 1:
            candidate = prefixes.pop().strip()
            if candidate:
                return candidate
    # Character-level common prefix fallback
    first = labels[0]
    shortest = min(len(lbl) for lbl in labels)
    common_len = 0
    for i in range(shortest):
        if all(lbl[i] == first[i] for lbl in labels):
            common_len = i + 1
        else:
            break
    if common_len >= 4:
        return first[:common_len].rstrip(" :-–").strip()
    return ""


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
        """Independent toggles — checkboxes, with radio groups for mutex presets.

        When multiple labeled changes target the same byte offset
        (Unlimited Dragon Flying: 5 Ride Duration options at one offset)
        they're mutually exclusive — rendered as a radio group so the
        user picks exactly one, instead of checkboxes where enabling
        all of them silently lets the last one win.
        """
        from PySide6.QtWidgets import QRadioButton, QButtonGroup

        self._mutex_groups = _detect_mutex_offset_groups(self._data) or []
        # Collect (patch_idx, change_idx) keys for change dicts that
        # belong to a mutex group — stable under deepcopy, unlike id().
        mutex_keys: set[tuple[int, int]] = set()
        for g in self._mutex_groups:
            for key in g.get("change_keys", []):
                mutex_keys.add(tuple(key))

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
                "QCheckBox { color: #E2E8F0; padding: 8px; } "
                "QRadioButton { color: #E2E8F0; padding: 6px 20px; } "
                "QLabel.mutexHeader { color: #E2E8F0; font-weight: bold; "
                "padding: 6px 0 0 0; }")
        else:
            scroll_widget.setStyleSheet("QWidget { background: #FAFBFC; } "
                "QCheckBox { color: #1A202C; padding: 8px; } "
                "QRadioButton { color: #1A202C; padding: 6px 20px; } "
                "QLabel.mutexHeader { color: #1A202C; font-weight: bold; "
                "padding: 6px 0 0 0; }")
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(8, 8, 8, 8)
        scroll_layout.setSpacing(4)

        self._checkboxes: list[tuple[QCheckBox, tuple[int, int]]] = []
        # Store: list of (button_group, [(radio, (p_idx,c_idx)), ...], group_label)
        self._toggle_radio_groups: list[tuple] = []

        # Render mutex groups first so they're visually at the top
        for group in self._mutex_groups:
            header_text = group["group_label"] + "  (pick one)"
            header = BodyLabel(header_text)
            header.setProperty("class", "mutexHeader")
            hfont = header.font()
            hfont.setBold(True)
            header.setFont(hfont)
            scroll_layout.addWidget(header)

            button_group = QButtonGroup(scroll_widget)
            button_group.setExclusive(True)
            radios: list[tuple] = []
            prefix_strip = group["group_label"]
            preselected_any = False
            for ch, ck in zip(group["changes"], group.get("change_keys", [])):
                label = ch.get("label", "")
                # Show just the option suffix if label starts with the
                # group's common prefix (e.g. "Ride Duration: 30 Minutes"
                # → "30 Minutes").
                display = label
                for sep in (": ", " - ", " – "):
                    if label.startswith(prefix_strip + sep):
                        display = label[len(prefix_strip) + len(sep):]
                        break
                radio = QRadioButton(display)
                if self._previous and label in self._previous:
                    radio.setChecked(True)
                    preselected_any = True
                button_group.addButton(radio)
                scroll_layout.addWidget(radio)
                radios.append((radio, tuple(ck)))
            # Default: first radio if nothing was pre-selected
            if not preselected_any and radios:
                radios[0][0].setChecked(True)
            self._toggle_radio_groups.append(
                (button_group, radios, group["group_label"]))

        # Then render independent changes as checkboxes
        has_any_independent = False
        for p_idx, patch in enumerate(self._data.get("patches", [])):
            for c_idx, change in enumerate(patch.get("changes", [])):
                key = (p_idx, c_idx)
                if key in mutex_keys:
                    continue
                label = change.get("label",
                                   f"offset {change.get('offset', '?')}")
                cb = QCheckBox(label)
                if self._previous is not None:
                    cb.setChecked(label in self._previous)
                else:
                    cb.setChecked(True)
                scroll_layout.addWidget(cb)
                self._checkboxes.append((cb, key))
                has_any_independent = True

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        self.viewLayout.addWidget(scroll)

        # Hide select/deselect-all buttons if there are no independent
        # toggles (only mutex radios) — those buttons don't apply.
        if not has_any_independent:
            sel_all.setVisible(False)
            desel_all.setVisible(False)

        total = len(self._checkboxes) + len(self._toggle_radio_groups)
        self._count_label = CaptionLabel(f"{total} configurable item(s)")
        self.viewLayout.addWidget(self._count_label)
        for cb, _ in self._checkboxes:
            cb.toggled.connect(self._update_count)
        # Also refresh the count line when the user swaps a mutex radio
        # pick — previously the label only reflected checkbox state.
        for _bg, radios, _glabel in self._toggle_radio_groups:
            for radio, _change in radios:
                radio.toggled.connect(self._update_count)

    def _select_all(self):
        for cb, _ in self._checkboxes:
            cb.setChecked(True)

    def _deselect_all(self):
        for cb, _ in self._checkboxes:
            cb.setChecked(False)

    def _update_count(self):
        cb_count = sum(1 for cb, _ in self._checkboxes if cb.isChecked())
        total = len(self._checkboxes) + len(
            getattr(self, "_toggle_radio_groups", []))
        picked = cb_count + len(
            getattr(self, "_toggle_radio_groups", []))
        self._count_label.setText(f"{picked} of {total} items selected")

    def _on_accept(self):
        import copy as _copy
        filtered = _copy.deepcopy(self._data)

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
            # Toggle mode — keep checked changes PLUS the one chosen
            # radio from each mutex group (same-offset presets).
            # Stable (patch_index, change_index) keys instead of id() —
            # id() may alias after GC or fail if dicts ever get copied.
            selected_keys = {
                key for cb, key in self._checkboxes if cb.isChecked()
            }
            for _bg, radios, _glabel in getattr(
                    self, "_toggle_radio_groups", []):
                for radio, key in radios:
                    if radio.isChecked():
                        selected_keys.add(key)
                        break
            if not selected_keys:
                return
            filtered = _filter_patches_by_keys(self._data, selected_keys)

        self.selected_data = filtered
        self.accept()
