"""Dialog for toggling individual patches within a JSON mod."""
import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout,
    QScrollArea, QVBoxLayout, QWidget, QFrame,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    MessageBox,
    MessageBoxBase,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
)

from cdumm.engine.mod_manager import ModManager

logger = logging.getLogger(__name__)


class PatchToggleDialog(MessageBoxBase):
    """Shows each individual byte-change in a JSON mod with a toggle checkbox."""

    def __init__(self, mod: dict, mod_manager: ModManager, parent=None) -> None:
        super().__init__(parent)
        self._mod = mod
        self._mm = mod_manager
        self._checkboxes: list[tuple[int, QCheckBox]] = []
        self._changed = False

        self.titleLabel = SubtitleLabel(f"Toggle Patches: {mod['name']}")
        self.viewLayout.addWidget(self.titleLabel)

        # Header
        header = BodyLabel(
            f"{mod['name']} -- Toggle individual changes on/off. "
            f"Disabled changes are skipped when you Apply."
        )
        header.setWordWrap(True)
        self.viewLayout.addWidget(header)

        # Load JSON source
        json_source = self._load_json_source(mod["id"])
        if json_source is None:
            self.viewLayout.addWidget(BodyLabel(
                "This mod does not use mount-time patching.\n"
                "Per-patch toggle is only available for JSON mods imported with v2.5+.\n"
                "Reimport the mod to enable this feature."
            ))
            self.yesButton.setText("Close")
            self.cancelButton.hide()
            self.widget.setMinimumWidth(650)
            return

        # Current disabled indices
        disabled = set(mod_manager.get_disabled_patches(mod["id"]))

        # Scroll area for patches
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(300)
        container = QWidget()
        patch_layout = QVBoxLayout(container)
        patch_layout.setSpacing(2)

        flat_idx = 0
        patches = json_source.get("patches", [])

        for patch in patches:
            game_file = patch.get("game_file", "unknown")
            changes = patch.get("changes", [])
            if not changes:
                continue

            # Game file header
            file_label = StrongBodyLabel(game_file)
            patch_layout.addWidget(file_label)

            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            patch_layout.addWidget(sep)

            for change in changes:
                cb = QCheckBox()
                cb.setChecked(flat_idx not in disabled)

                label_text = change.get("label", "")
                offset = change.get("offset", "?")
                original = change.get("original", "")
                patched = change.get("patched", "")
                ctype = change.get("type", "replace")

                if label_text:
                    desc = f"[{ctype}] {label_text} @ offset {offset}"
                else:
                    desc = f"[{ctype}] offset {offset}: {original[:16]}{'...' if len(original) > 16 else ''} -> {patched[:16]}{'...' if len(patched) > 16 else ''}"

                cb.setText(desc)
                cb.setToolTip(
                    f"Offset: {offset}\n"
                    f"Original: {original}\n"
                    f"Patched: {patched}\n"
                    f"Type: {ctype}"
                )

                idx = flat_idx
                cb.toggled.connect(lambda checked, i=idx: self._on_toggle(i, checked))
                self._checkboxes.append((flat_idx, cb))
                patch_layout.addWidget(cb)
                flat_idx += 1

        patch_layout.addStretch()
        scroll.setWidget(container)
        self.viewLayout.addWidget(scroll)

        # Status
        total = flat_idx
        enabled = total - len(disabled)
        self._status = CaptionLabel(f"{enabled}/{total} patches enabled")
        self.viewLayout.addWidget(self._status)

        # Bulk buttons
        bulk_row = QHBoxLayout()
        enable_all = PushButton("Enable All")
        enable_all.clicked.connect(self._enable_all)
        bulk_row.addWidget(enable_all)

        disable_all = PushButton("Disable All")
        disable_all.clicked.connect(self._disable_all)
        bulk_row.addWidget(disable_all)
        bulk_row.addStretch()
        self.viewLayout.addLayout(bulk_row)

        # Override default buttons
        self.yesButton.setText("Save && Close")
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._save_and_close)
        self.cancelButton.setText("Cancel")

        self.widget.setMinimumWidth(650)

    def _load_json_source(self, mod_id: int) -> dict | None:
        """Load the JSON source for a mount-time mod."""
        json_path = self._mm.get_json_source(mod_id)
        if not json_path:
            return None
        path = Path(json_path)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _on_toggle(self, idx: int, checked: bool) -> None:
        self._changed = True
        self._update_status()

    def _update_status(self) -> None:
        total = len(self._checkboxes)
        enabled = sum(1 for _, cb in self._checkboxes if cb.isChecked())
        self._status.setText(f"{enabled}/{total} patches enabled")

    def _enable_all(self) -> None:
        for _, cb in self._checkboxes:
            cb.setChecked(True)
        self._changed = True

    def _disable_all(self) -> None:
        for _, cb in self._checkboxes:
            cb.setChecked(False)
        self._changed = True

    def _save_and_close(self) -> None:
        disabled = [idx for idx, cb in self._checkboxes if not cb.isChecked()]
        self._mm.set_disabled_patches(self._mod["id"], disabled)
        self._changed = False
        self.accept()

    def reject(self) -> None:
        """Override reject to warn about unsaved changes."""
        if self._changed:
            w = MessageBox(
                "Unsaved Changes",
                "You have unsaved patch toggle changes. Save before closing?",
                self,
            )
            w.yesButton.setText("Save")
            w.cancelButton.setText("Discard")
            if w.exec():
                self._save_and_close()
                return
        super().reject()
