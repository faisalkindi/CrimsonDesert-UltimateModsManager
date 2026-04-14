"""ConflictCard — flat card showing a conflict between two mods."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from qfluentwidgets import CardWidget, CaptionLabel, StrongBodyLabel, isDarkTheme, setCustomStyleSheet


class ConflictCard(CardWidget):
    """A card showing a conflict between two mods.

    Parameters
    ----------
    mod_a : str
        Name of the first conflicting mod.
    mod_b : str
        Name of the second conflicting mod.
    description : str
        Human-readable description (e.g. "Both change inventory settings").
    level : str
        Conflict severity label shown in the pill badge.
    resolution : str
        Resolution text (e.g. "Inventory Expander wins").
    parent : QWidget | None
        Parent widget.
    """

    def __init__(
        self,
        mod_a: str,
        mod_b: str,
        description: str,
        level: str,
        resolution: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setFixedHeight(68)
        self._apply_flat_style()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(12)

        # -- Warning icon (grayscale text glyph) --
        icon_label = QLabel("\u26A0")
        icon_label.setFixedSize(24, 24)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("font-size: 24px; color: #9E9E9E;")
        layout.addWidget(icon_label)

        # -- Info section --
        info = QVBoxLayout()
        info.setSpacing(2)
        info.setContentsMargins(0, 0, 0, 0)
        info.addWidget(StrongBodyLabel(f"{mod_a} vs {mod_b}"))
        info.addWidget(CaptionLabel(description))
        layout.addLayout(info, 1)

        # -- Level badge (pill) --
        level_label = QLabel(level)
        level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._level_label = level_label
        self._apply_level_style()
        layout.addWidget(level_label)

        # -- Resolution text --
        res_label = CaptionLabel(resolution)
        layout.addWidget(res_label)

    # ------------------------------------------------------------------
    # Flat card styling — 1px border, no shadow
    # ------------------------------------------------------------------

    def _apply_flat_style(self) -> None:
        light_qss = f"ConflictCard{{border: 1px solid #E5E7EB; background: #FFFFFF; border-radius: 6px;}}"
        dark_qss = f"ConflictCard{{border: 1px solid #2D3340; background: #1C2028; border-radius: 6px;}}"
        setCustomStyleSheet(self, light_qss, dark_qss)

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_flat_style()
            self._apply_level_style()

    def _apply_level_style(self) -> None:
        dark = isDarkTheme()
        if dark:
            bg, fg, border = "#3E2A10", "#FFB74D", "#6E4A1A"
        else:
            bg, fg, border = "#FFF3E0", "#E65100", "#FFCC80"
        self._level_label.setStyleSheet(
            f"background: {bg}; color: {fg}; border: 1px solid {border}; "
            f"border-radius: 10px; padding: 2px 10px; "
            f"font-size: 11px; font-weight: 600;"
        )
        self._level_label.setFixedHeight(22)
