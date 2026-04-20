"""ReShade install + preset management page.

Three display modes driven by `detect_reshade_install`:
  - "not_installed" -> install wizard card with a "Download from reshade.me" button
  - "error"         -> distinct error card (don't tell the user to reinstall when
                       CDUMM couldn't even read bin64/)
  - "installed"     -> preset picker (Task 3 fleshes out the picker; Task 2 ships
                       with an "Installed" summary card as a placeholder)

Refresh triggers:
  - Explicit Refresh button (always visible in the header)
  - `focusInEvent`: when the tab regains focus, re-detect (debounced 500ms so
    rapid tab switches don't spam filesystem calls)
"""
from __future__ import annotations

import logging
import webbrowser
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    TitleLabel,
)

from cdumm.engine.reshade_detect import ReshadeInstall, detect_reshade_install
from cdumm.i18n import tr

logger = logging.getLogger(__name__)

_REFRESH_DEBOUNCE_MS = 500


class ReshadePage(SmoothScrollArea):
    """Sidebar page for ReShade install + preset management.

    Public surface:
      - `set_managers(db=..., game_dir=...)`  standard CDUMM page wiring
      - `refresh()`                             re-run detection and rebuild UI
      - `current_state`                         the most recent detect state
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("reshade_page")
        self.setWidgetResizable(True)

        self._db = None
        self._game_dir: Path | None = None
        self._last_detect: ReshadeInstall | None = None

        # Debounce timer for focus-triggered refresh.
        self._focus_refresh_timer = QTimer(self)
        self._focus_refresh_timer.setInterval(_REFRESH_DEBOUNCE_MS)
        self._focus_refresh_timer.setSingleShot(True)
        self._focus_refresh_timer.timeout.connect(self.refresh)

        self._build_shell()
        self.enableTransparentBackground()
        self.setScrollAnimation(
            Qt.Orientation.Vertical, 400, QEasingCurve.Type.OutQuint)

    # ── Setup -------------------------------------------------------

    def _build_shell(self) -> None:
        """Build the outer layout once; _build_content() swaps the inner body."""
        self._container = QWidget()
        root = QVBoxLayout(self._container)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(0)

        # Header: title + Refresh button
        header = QHBoxLayout()
        title = TitleLabel(tr("reshade.title"), self._container)
        tf = title.font()
        tf.setPixelSize(28)
        tf.setWeight(QFont.Weight.Bold)
        title.setFont(tf)
        header.addWidget(title)
        header.addStretch()
        self._refresh_btn = PushButton(FluentIcon.SYNC, tr("reshade.refresh"),
                                        self._container)
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        root.addLayout(header)
        root.addSpacing(24)

        # Body placeholder — refresh() replaces this.
        self._body_layout = QVBoxLayout()
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(16)
        root.addLayout(self._body_layout)

        root.addStretch()
        self.setWidget(self._container)

    def set_managers(self, **kwargs) -> None:
        self._db = kwargs.get("db", self._db)
        self._game_dir = kwargs.get("game_dir", self._game_dir)
        # Initial scan once we have a game_dir.
        if self._game_dir is not None:
            self.refresh()

    # ── Detection + rebuild ----------------------------------------

    @property
    def current_state(self) -> str | None:
        return self._last_detect.state if self._last_detect else None

    def refresh(self) -> None:
        """Run detection and rebuild the body for the current state.

        No-op if game_dir isn't set yet.
        """
        if self._game_dir is None:
            return
        self._last_detect = detect_reshade_install(self._game_dir)
        self._rebuild_body(self._last_detect)

    def _rebuild_body(self, install: ReshadeInstall) -> None:
        """Replace the body layout contents with a view matching `install.state`."""
        _clear_layout(self._body_layout)

        if install.state == "installed":
            self._build_installed_view(install)
        elif install.state == "error":
            self._build_error_view(install)
        else:
            self._build_not_installed_view()

    def _build_not_installed_view(self) -> None:
        card = CardWidget(self._container)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(32, 24, 32, 24)
        lay.setSpacing(12)

        title = StrongBodyLabel(tr("reshade.not_installed_title"), card)
        tf = title.font()
        tf.setPixelSize(18)
        title.setFont(tf)
        lay.addWidget(title)
        lay.addSpacing(4)

        for key in ("reshade.not_installed_step1",
                    "reshade.not_installed_step2",
                    "reshade.not_installed_step3"):
            row = BodyLabel(tr(key), card)
            row.setWordWrap(True)
            lay.addWidget(row)

        lay.addSpacing(8)
        btn = PrimaryPushButton(FluentIcon.LINK,
                                tr("reshade.download_btn"), card)
        btn.clicked.connect(lambda: webbrowser.open("https://reshade.me/"))
        btn_row = QHBoxLayout()
        btn_row.addWidget(btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._body_layout.addWidget(card)

    def _build_error_view(self, install: ReshadeInstall) -> None:
        card = CardWidget(self._container)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(32, 24, 32, 24)
        lay.setSpacing(12)

        title = StrongBodyLabel(tr("reshade.error_title"), card)
        tf = title.font()
        tf.setPixelSize(18)
        title.setFont(tf)
        lay.addWidget(title)

        bin64 = (self._game_dir / "bin64") if self._game_dir else Path("bin64")
        body = BodyLabel(tr("reshade.error_body",
                            path=str(bin64),
                            error=install.error or "unknown"), card)
        body.setWordWrap(True)
        lay.addWidget(body)

        retry_row = QHBoxLayout()
        retry_btn = PrimaryPushButton(FluentIcon.SYNC,
                                      tr("reshade.error_retry"), card)
        retry_btn.clicked.connect(self.refresh)
        retry_row.addWidget(retry_btn)
        retry_row.addStretch()
        lay.addLayout(retry_row)

        self._body_layout.addWidget(card)

    def _build_installed_view(self, install: ReshadeInstall) -> None:
        """Task 2 ships an 'Installed' summary. Task 3 replaces this with
        the full preset picker."""
        card = CardWidget(self._container)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(32, 24, 32, 24)
        lay.setSpacing(8)

        title = StrongBodyLabel(tr("reshade.installed_title"), card)
        tf = title.font()
        tf.setPixelSize(18)
        title.setFont(tf)
        lay.addWidget(title)

        if install.dll_path:
            dll_info = CaptionLabel(
                tr("reshade.installed_location", dll=install.dll_path.name), card)
            lay.addWidget(dll_info)

        if install.presets:
            count = BodyLabel(
                tr("reshade.installed_presets_count",
                   count=len(install.presets)), card)
            lay.addWidget(count)
        else:
            # Empty-state: installed but no presets in the search dir.
            no_presets = BodyLabel(tr("reshade.no_presets_body",
                                      path=str(install.base_path or "bin64")),
                                   card)
            no_presets.setWordWrap(True)
            lay.addWidget(no_presets)

        self._body_layout.addWidget(card)

    # ── Focus-triggered refresh ------------------------------------

    def focusInEvent(self, event):  # noqa: N802 — Qt API signature
        """Debounced re-detect on tab focus."""
        super().focusInEvent(event)
        self._focus_refresh_timer.start()  # restarts if already running


# --- helpers --------------------------------------------------------------

def _clear_layout(layout) -> None:
    """Remove and delete every widget/sub-layout from `layout`."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
            continue
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)
