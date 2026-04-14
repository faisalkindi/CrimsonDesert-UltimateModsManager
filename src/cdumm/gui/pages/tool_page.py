"""Tool pages for CDUMM v3 -- inline diagnostic tools.

Each diagnostic tool is a full sub-interface page (no popups/dialogs).
Results, progress, and actions all happen within the page itself.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEventLoop, QThread, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    IndeterminateProgressBar,
    ProgressBar,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    isDarkTheme,
)

from cdumm.i18n import tr

logger = logging.getLogger(__name__)

# Module-level lock: only one tool can run at a time
_active_tool = None


# ======================================================================
# Result card -- used by all tool pages to display result items
# ======================================================================

class _StatCard(CardWidget):
    """A wide dashboard stat card with a colored left accent border.

    Uses CardWidget (not QFrame) so qfluentwidgets handles theme-aware
    backgrounds natively. The accent left border is painted via paintEvent.
    """

    def __init__(self, value: str, label: str,
                 accent_color: str = "#2878D0", label_key: str = "",
                 parent=None):
        super().__init__(parent)
        self._accent = accent_color
        self._label_key = label_key
        self.setFixedHeight(140)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 28, 24)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self._value = StrongBodyLabel(value, self)
        vf = self._value.font()
        vf.setPixelSize(48)
        vf.setWeight(QFont.Weight.Bold)
        self._value.setFont(vf)
        layout.addWidget(self._value)

        self._label = CaptionLabel(label, self)
        lf = self._label.font()
        lf.setPixelSize(13)
        self._label.setFont(lf)
        self._apply_label_color()
        layout.addWidget(self._label)

    def paintEvent(self, event):
        # Draw subtle drop shadow BEFORE the card background
        from PySide6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow_alpha = 77 if isDarkTheme() else 20  # 0.3 vs 0.08
        shadow_color = QColor(0, 0, 0, shadow_alpha)
        shadow_rect = self.rect().adjusted(2, 2, -1, 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow_color)
        painter.drawRoundedRect(shadow_rect, 8, 8)
        painter.end()

        super().paintEvent(event)

        # Draw the accent left border on top of CardWidget's background
        painter2 = QPainter(self)
        painter2.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter2.setPen(Qt.PenStyle.NoPen)
        painter2.setBrush(QColor(self._accent))
        painter2.drawRoundedRect(0, 0, 5, self.height(), 2, 2)
        painter2.end()

    def _apply_label_color(self):
        from qfluentwidgets import setCustomStyleSheet
        setCustomStyleSheet(self._label,
            "CaptionLabel{color:#718096;}", "CaptionLabel{color:#A0AEC0;}")

    def _apply_theme(self):
        # Re-apply font sizes (QFont survives theme changes, but call just in case)
        if hasattr(self, '_value'):
            vf = self._value.font()
            vf.setPixelSize(48)
            vf.setWeight(QFont.Weight.Bold)
            self._value.setFont(vf)
        if hasattr(self, '_label'):
            lf = self._label.font()
            lf.setPixelSize(13)
            self._label.setFont(lf)
            self._apply_label_color()

    def set_value(self, value: str):
        self._value.setText(value)

    def retranslate(self):
        """Update the label text after a language change."""
        if self._label_key:
            self._label.setText(tr(self._label_key))

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_theme()
            self._apply_label_color()


class _ResultCard(CardWidget):
    """A result card with a colored left accent border and terminal-style details.

    Uses CardWidget for native theme handling. Accent border via paintEvent.
    """

    def __init__(self, title: str, detail: str = "",
                 color: str = "", parent=None):
        super().__init__(parent)
        self._color = color

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 16, 24, 16)
        layout.setSpacing(8)

        self._title_label = StrongBodyLabel(title, self)
        tf = self._title_label.font()
        tf.setPixelSize(15)
        self._title_label.setFont(tf)
        if color:
            from qfluentwidgets import setCustomStyleSheet
            setCustomStyleSheet(self._title_label,
                f"StrongBodyLabel{{color:{color};}}", f"StrongBodyLabel{{color:{color};}}")
        layout.addWidget(self._title_label)

        if detail:
            detail_label = CaptionLabel(detail, self)
            detail_label.setWordWrap(True)
            mono = "Consolas" if os.name == "nt" else "monospace"
            df = detail_label.font()
            df.setPixelSize(13)
            df.setFamily(mono)
            detail_label.setFont(df)
            layout.addWidget(detail_label)

    def paintEvent(self, event):
        # Draw subtle drop shadow BEFORE the card background
        from PySide6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow_alpha = 77 if isDarkTheme() else 20
        shadow_color = QColor(0, 0, 0, shadow_alpha)
        shadow_rect = self.rect().adjusted(2, 2, -1, 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow_color)
        painter.drawRoundedRect(shadow_rect, 8, 8)
        painter.end()

        super().paintEvent(event)

        if self._color:
            painter2 = QPainter(self)
            painter2.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter2.setPen(Qt.PenStyle.NoPen)
            painter2.setBrush(QColor(self._color))
            painter2.drawRoundedRect(0, 0, 5, self.height(), 2, 2)
            painter2.end()


class _ShadowCard(CardWidget):
    """CardWidget with a subtle drop shadow painted underneath."""

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow_alpha = 77 if isDarkTheme() else 20
        shadow_color = QColor(0, 0, 0, shadow_alpha)
        shadow_rect = self.rect().adjusted(2, 2, -1, 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow_color)
        painter.drawRoundedRect(shadow_rect, 8, 8)
        painter.end()
        super().paintEvent(event)


# ======================================================================
# ToolPageBase -- reusable base for all tool pages
# ======================================================================

class ToolPageBase(SmoothScrollArea):
    """Base class for a diagnostic tool page.

    Provides: title, description, Run button, progress ring, scrollable
    results area, and status label.  Subclasses override ``_run_tool()``
    and call ``_add_result_card()`` / ``_set_status()`` when done.
    """

    def __init__(self, object_name: str, title: str, description: str,
                 run_label: str = "Run", parent=None,
                 title_key: str = "", desc_key: str = "", run_key: str = ""):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWidgetResizable(True)
        # Store translation keys for retranslate_ui
        self._title_key = title_key
        self._desc_key = desc_key
        self._run_key = run_key

        # Engine refs (set via set_managers)
        self._db = None
        self._game_dir: Path | None = None
        self._snapshot = None
        self._mod_manager = None
        self._conflict_detector = None
        self._vanilla_dir: Path | None = None
        self._deltas_dir: Path | None = None
        self._activity_log = None

        # Worker tracking
        self._worker_thread: QThread | None = None

        # ── Build UI ────────────────────────────────────────────────
        self._container = QWidget()
        root = QVBoxLayout(self._container)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────
        from qfluentwidgets import TitleLabel
        self._title_label = TitleLabel(title, self._container)
        tf = self._title_label.font()
        tf.setPixelSize(28)
        tf.setWeight(QFont.Weight.Bold)
        self._title_label.setFont(tf)
        root.addWidget(self._title_label)
        root.addSpacing(8)

        self._desc_label = BodyLabel(description, self._container)
        self._desc_label.setWordWrap(True)
        df = self._desc_label.font()
        df.setPixelSize(15)
        self._desc_label.setFont(df)
        root.addWidget(self._desc_label)
        root.addSpacing(20)

        # ── Divider ────────────────────────────────────────────────
        self._header_divider = self._make_divider()
        root.addWidget(self._header_divider)
        root.addSpacing(20)

        # ── Stats row ──────────────────────────────────────────────
        self._stats_row = QHBoxLayout()
        self._stats_row.setContentsMargins(0, 0, 0, 0)
        self._stats_row.setSpacing(16)
        root.addLayout(self._stats_row)
        root.addSpacing(48)

        # ── Action area (no card wrapper — button lives directly in page) ─
        self._action_card = self._container  # keep reference for subclass compat

        self._action_row = action_row = QVBoxLayout()
        action_row.setSpacing(12)
        action_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._run_btn = PrimaryPushButton(run_label, self._container)
        self._run_btn.setFixedWidth(360)
        self._run_btn.setFixedHeight(52)
        self._apply_run_btn_style()
        self._run_btn.clicked.connect(self._on_run_clicked)
        action_row.addWidget(self._run_btn, 0, Qt.AlignmentFlag.AlignCenter)

        self._status_label = BodyLabel("", self._container)
        sf = self._status_label.font()
        sf.setPixelSize(20)
        sf.setWeight(QFont.Weight.DemiBold)
        self._status_label.setFont(sf)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        action_row.addWidget(self._status_label)

        root.addLayout(action_row)
        root.addSpacing(0)

        # Progress bar (full width)
        self._progress_bar = ProgressBar(self._container)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(10)
        self._progress_bar.setStyleSheet("ProgressBar { border-radius: 5px; }")
        self._progress_bar.hide()
        root.addWidget(self._progress_bar)

        # Indeterminate progress bar
        self._indeterminate_bar = IndeterminateProgressBar(self._container)
        self._indeterminate_bar.setFixedHeight(10)
        self._indeterminate_bar.hide()
        root.addWidget(self._indeterminate_bar)
        root.addSpacing(4)

        # Progress detail text
        self._progress_detail = BodyLabel("", self._container)
        self._progress_detail.setWordWrap(True)
        self._progress_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pf = self._progress_detail.font()
        pf.setPixelSize(15)
        self._progress_detail.setFont(pf)
        self._progress_detail.hide()
        root.addWidget(self._progress_detail)
        root.addSpacing(0)

        # ── Divider above results ──────────────────────────────────
        self._results_divider = self._make_divider()
        root.addWidget(self._results_divider)
        root.addSpacing(16)

        # ── Results area ───────────────────────────────────────────
        self._results_layout = QVBoxLayout()
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(10)
        root.addLayout(self._results_layout)

        # ── Extra actions ──────────────────────────────────────────
        self._actions_layout = QHBoxLayout()
        self._actions_layout.setContentsMargins(0, 8, 0, 0)
        self._actions_layout.setSpacing(8)
        self._actions_layout.addStretch()
        root.addLayout(self._actions_layout)

        root.addStretch()

        self.setWidget(self._container)
        self.enableTransparentBackground()
        self.setScrollAnimation(Qt.Orientation.Vertical, 400,
                                QEasingCurve.Type.OutQuint)

    # ── Engine wiring ───────────────────────────────────────────────

    def set_managers(self, **kwargs) -> None:
        self._db = kwargs.get("db")
        self._game_dir = kwargs.get("game_dir")
        self._snapshot = kwargs.get("snapshot")
        self._mod_manager = kwargs.get("mod_manager")
        self._conflict_detector = kwargs.get("conflict_detector")
        self._vanilla_dir = kwargs.get("vanilla_dir")
        self._deltas_dir = kwargs.get("deltas_dir")
        self._activity_log = kwargs.get("activity_log")

    def refresh(self) -> None:
        """No-op -- tool pages are static until the user clicks Run."""
        pass

    def retranslate_ui(self) -> None:
        """Update all visible text after a language change."""
        if self._title_key:
            self._title_label.setText(tr(self._title_key))
        if self._desc_key:
            self._desc_label.setText(tr(self._desc_key))
        if self._run_key:
            self._run_btn.setText(tr(self._run_key))
        # Retranslate all stat cards in the stats row
        for i in range(self._stats_row.count()):
            item = self._stats_row.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, _StatCard):
                widget.retranslate()

    # ── Helpers for subclasses ──────────────────────────────────────

    @staticmethod
    def _make_divider() -> QFrame:
        """Create a thin horizontal divider line, theme-aware."""
        line = QFrame()
        line.setFixedHeight(1)
        color = "#2D3340" if isDarkTheme() else "#E5E7EB"
        line.setStyleSheet(f"background: {color}; border: none;")
        return line

    def _apply_desc_style(self) -> None:
        desc_color = "#8B95A5" if isDarkTheme() else "#6B7585"
        from qfluentwidgets import setCustomStyleSheet
        setCustomStyleSheet(self._desc_label,
            f"BodyLabel{{color:#6B7585;}}", f"BodyLabel{{color:#8B95A5;}}")

    def _apply_run_btn_style(self) -> None:
        from qfluentwidgets import setCustomStyleSheet
        light = (
            "PrimaryPushButton {"
            "  background-color: #2878D0; color: white;"
            "  border-radius: 12px; border: none; padding-bottom: 6px;"
            "}"
            "PrimaryPushButton:hover { background-color: #3388E0; }"
            "PrimaryPushButton:pressed { background-color: #2060B0; }"
            "PrimaryPushButton:disabled { background-color: #ccc; color: #999; }"
        )
        dark = (
            "PrimaryPushButton {"
            "  background-color: #3A8FE0; color: white;"
            "  border-radius: 12px; border: none; padding-bottom: 6px;"
            "}"
            "PrimaryPushButton:hover { background-color: #4DA0F0; }"
            "PrimaryPushButton:pressed { background-color: #2878D0; }"
            "PrimaryPushButton:disabled { background-color: #333; color: #666; }"
        )
        setCustomStyleSheet(self._run_btn, light, dark)
        bf = self._run_btn.font()
        bf.setPixelSize(15)
        bf.setWeight(QFont.Weight.Bold)
        self._run_btn.setFont(bf)

    @staticmethod
    def _update_divider(divider: QFrame) -> None:
        color = "#2D3340" if isDarkTheme() else "#E5E7EB"
        divider.setStyleSheet(f"background: {color}; border: none;")

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_desc_style()
            self._apply_run_btn_style()
            self._update_divider(self._header_divider)
            self._update_divider(self._results_divider)

    def _clear_results(self) -> None:
        """Remove all result cards and action buttons."""
        while self._results_layout.count():
            item = self._results_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        # Clear action buttons (keep the stretch at index 0)
        while self._actions_layout.count() > 1:
            item = self._actions_layout.takeAt(1)
            w = item.widget()
            if w:
                w.deleteLater()

    def _add_stat_card(self, value: str, label: str,
                       accent: str = "#2878D0",
                       label_key: str = "") -> _StatCard:
        """Add a stat card to the stats row (fills equally)."""
        card = _StatCard(value, label, accent_color=accent,
                         label_key=label_key, parent=self._container)
        self._stats_row.addWidget(card, 1)  # stretch=1 so cards fill width equally
        return card

    def _add_result_card(self, title: str, detail: str = "",
                         color: str = "") -> _ResultCard:
        card = _ResultCard(title, detail, color, self._container)
        self._results_layout.addWidget(card)
        return card

    def _add_action_button(self, text: str, callback, primary=False) -> PushButton:
        btn_cls = PrimaryPushButton if primary else PushButton
        btn = btn_cls(text, self._container)
        btn.clicked.connect(callback)
        self._actions_layout.addWidget(btn)
        return btn

    def _set_status(self, text: str, color: str = "") -> None:
        self._status_label.setText(text)
        sf = self._status_label.font()
        sf.setPixelSize(18)
        sf.setWeight(QFont.Weight.DemiBold)
        self._status_label.setFont(sf)
        if color:
            from qfluentwidgets import setCustomStyleSheet
            setCustomStyleSheet(self._status_label,
                f"BodyLabel{{color:{color};}}", f"BodyLabel{{color:{color};}}")

    def _set_progress(self, pct: int, message: str) -> None:
        """Update progress bar, percentage, and detail text."""
        if self._indeterminate_bar.isVisible():
            self._indeterminate_bar.hide()
            self._progress_bar.show()
        self._progress_bar.setValue(pct)
        self._status_label.setText(f"{pct}%")
        sf = self._status_label.font()
        sf.setPixelSize(32)
        sf.setWeight(QFont.Weight.Bold)
        self._status_label.setFont(sf)
        self._progress_detail.setText(message)

    def _set_running(self, running: bool) -> None:
        global _active_tool
        if running:
            _active_tool = self
            self._run_btn.setEnabled(False)
            self._progress_bar.setValue(0)
            self._progress_bar.hide()
            self._indeterminate_bar.show()  # start with indeterminate
            self._progress_detail.setText(tr("tools.starting"))
            self._progress_detail.setStyleSheet("font-size: 15px;")
            self._progress_detail.show()
            self._set_status(tr("tools.running"))
        else:
            _active_tool = None
            self._run_btn.setEnabled(True)
            self._progress_bar.hide()
            self._indeterminate_bar.hide()
            self._progress_detail.hide()
            # Reset status font back to normal after completion
            sf = self._status_label.font()
            sf.setPixelSize(18)
            sf.setWeight(QFont.Weight.DemiBold)
            self._status_label.setFont(sf)

    @staticmethod
    def is_any_tool_running() -> bool:
        return _active_tool is not None

    def _log_activity(self, category: str, message: str,
                      detail: str = None) -> None:
        if self._activity_log:
            try:
                self._activity_log.log(category, message, detail)
            except Exception:
                pass

    # ── Subclass hook ───────────────────────────────────────────────

    def _can_run(self) -> bool:
        """Check if a tool can start. Returns False if another tool is running."""
        if _active_tool is not None and _active_tool is not self:
            self._set_status(tr("tools.another_running"), "#E65100")
            return False
        return True

    def _on_run_clicked(self) -> None:
        """Override in subclass to implement tool logic."""
        raise NotImplementedError


# ======================================================================
# VerifyStatePage
# ======================================================================

class VerifyStatePage(ToolPageBase):
    """Verify which game files are modded, vanilla, or unexpected."""

    def __init__(self, parent=None):
        super().__init__(
            object_name="VerifyStatePage",
            title=tr("tools.verify.title"),
            description=tr("tools.verify.desc"),
            run_label=tr("tools.verify.run"),
            parent=parent,
            title_key="tools.verify.title",
            desc_key="tools.verify.desc",
            run_key="tools.verify.run",
        )
        # Dashboard stat cards
        self._stat_total = self._add_stat_card(
            "--", tr("tools.verify.total_files"), "#2878D0",
            label_key="tools.verify.total_files")
        self._stat_last = self._add_stat_card(
            "--", tr("tools.verify.last_verified"), "#8B5CF6",
            label_key="tools.verify.last_verified")
        self._stat_state = self._add_stat_card(
            tr("tools.verify.unknown"), tr("tools.verify.game_state"), "#A3BE8C",
            label_key="tools.verify.game_state")

    def set_managers(self, **kwargs) -> None:
        super().set_managers(**kwargs)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        if self._db:
            try:
                row = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots").fetchone()
                self._stat_total.set_value(str(row[0]) if row else "0")
            except Exception:
                self._stat_total.set_value("0")
        if self._activity_log:
            try:
                entries = self._activity_log.search("verified")
                if entries:
                    ts = entries[0]["timestamp"]
                    # Show only date portion
                    self._stat_last.set_value(ts.split(" ")[0] if " " in ts else ts)
                else:
                    self._stat_last.set_value(tr("tools.stat.never"))
            except Exception:
                self._stat_last.set_value(tr("tools.stat.never"))

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        # Re-translate the "Unknown" default value on the game state card
        # (only if it hasn't been set to a result yet -- check for known keys)
        current = self._stat_state._value.text()
        state_values = {
            tr("tools.verify.unknown"), tr("tools.verify.clean"),
            tr("tools.verify.modded"), "Unknown", "Clean", "Modded",
            "Unbekannt", "Sauber", "Gemoddet",
        }
        if current in state_values or current == "--":
            self._refresh_stats()
            # If still default, force unknown
            if self._stat_state._value.text() in ("--", "Unknown", "Unbekannt"):
                self._stat_state.set_value(tr("tools.verify.unknown"))
        # Re-translate "Never" values
        never_values = {"Never", "Nie", tr("tools.stat.never")}
        if self._stat_last._value.text() in never_values:
            self._stat_last.set_value(tr("tools.stat.never"))

    def _on_run_clicked(self) -> None:
        if not self._can_run():
            return
        if not self._snapshot or not self._snapshot.has_snapshot():
            self._clear_results()
            self._set_status(tr("tools.verify.no_snapshot"),
                             "#BF616A")
            return

        self._clear_results()
        self._set_running(True)

        # Run inline (not threaded) so progress updates paint immediately
        from cdumm.storage.database import Database
        import os

        try:
            db = Database(self._db.db_path)
            db.initialize()
            cursor = db.connection.execute(
                "SELECT file_path, file_hash, file_size FROM snapshots")
            snap_entries = cursor.fetchall()

            if not snap_entries:
                self._set_running(False)
                self._set_status(tr("tools.verify.no_snapshot"), "#BF616A")
                db.close()
                return

            results = {"vanilla": [], "modded": [], "missing": [], "extra_dirs": [], "total": len(snap_entries)}

            for i, (file_path, snap_hash, snap_size) in enumerate(snap_entries):
                pct = int((i / len(snap_entries)) * 100)
                self._set_progress(pct, tr("tools.progress.checking", name=file_path))
                QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

                game_file = self._game_dir / file_path.replace("/", os.sep)
                if not game_file.exists():
                    results["missing"].append(file_path)
                    continue

                actual_size = game_file.stat().st_size
                if actual_size != snap_size:
                    results["modded"].append(f"{file_path} — size {actual_size} != vanilla {snap_size}")
                else:
                    from cdumm.engine.snapshot_manager import hash_file
                    actual_hash, _ = hash_file(game_file)
                    if actual_hash != snap_hash:
                        results["modded"].append(f"{file_path} — content differs (same size)")
                    else:
                        results["vanilla"].append(file_path)

            # Check for extra directories (>= 0036)
            for item in sorted(self._game_dir.iterdir()):
                if item.is_dir() and item.name.isdigit() and int(item.name) >= 36:
                    results["extra_dirs"].append(item.name)

            self._set_progress(100, tr("tools.progress.done"))
            db.close()
            self._on_verify_done(results)

        except Exception as e:
            self._on_verify_error(str(e))

    def _on_verify_done(self, results: dict) -> None:
        self._set_running(False)
        self._worker_thread = None

        modded = results.get("modded", [])
        vanilla = results.get("vanilla", [])
        missing = results.get("missing", [])
        extra = results.get("extra_dirs", [])
        total = results.get("total", 0)

        # Update stat cards
        from datetime import datetime
        self._stat_last.set_value(datetime.now().strftime("%Y-%m-%d"))

        if not modded and not extra and not missing:
            self._stat_state.set_value(tr("tools.verify.clean"))
            self._stat_state._value.setStyleSheet("font-size: 36px; color: #A3BE8C; background: transparent; border: none;")
            self._set_status(tr("tools.verify.all_clean"), "#A3BE8C")
            self._add_result_card(
                tr("tools.verify.all_files_match", count=len(vanilla)),
                tr("tools.verify.no_mods_detected"),
                color="#A3BE8C",
            )
            self._log_activity("verify",
                f"Game state verified: ALL CLEAN ({len(vanilla)} files vanilla)")
        else:
            self._stat_state.set_value(tr("tools.verify.modded"))
            self._stat_state._value.setStyleSheet("font-size: 36px; color: #BF616A; background: transparent; border: none;")
            self._set_status(tr("tools.verify.modded_status"), "#BF616A")
            self._add_result_card(
                tr("tools.verify.summary", modded=len(modded), missing=len(missing), extra=len(extra)),
                tr("tools.verify.vanilla_count", vanilla=len(vanilla), total=total),
                color="#BF616A",
            )
            self._log_activity("verify",
                f"Game state verified: {len(modded)} modded, "
                f"{len(extra)} extra dirs, {len(vanilla)} vanilla")

        # Modded files
        if modded:
            if modded and isinstance(modded[0], dict):
                detail = "\n".join(f"• {m['path']} — {m['reason']}" for m in modded)
            else:
                detail = "\n".join(f"• {m}" for m in modded)
            self._add_result_card(
                tr("tools.verify.modded_files", count=len(modded)),
                detail,
                color="#BF616A",
            )

        # Extra directories
        if extra:
            if extra and isinstance(extra[0], dict):
                lines = [f"• {d['name']}/ — {', '.join(d['files'])}" for d in extra]
            else:
                lines = [f"• {d}/" for d in extra]
            self._add_result_card(
                tr("tools.verify.extra_dirs", count=len(extra)),
                "\n".join(lines),
                color="#D08770",
            )

        # Missing files
        if missing:
            self._add_result_card(
                tr("tools.verify.missing_files", count=len(missing)),
                "\n".join(missing[:30]),
                color="#EBCB8B",
            )

        # Vanilla files (collapsed summary)
        if vanilla:
            self._add_result_card(
                tr("tools.verify.vanilla_files", count=len(vanilla)),
                tr("tools.verify.all_match_snapshot"),
                color="#A3BE8C",
            )

    def _on_verify_error(self, error: str) -> None:
        self._set_running(False)
        self._worker_thread = None
        self._set_status(tr("tools.error", detail=error), "#BF616A")
        self._add_result_card(tr("tools.verify.failed"), error, color="#BF616A")


# ======================================================================
# CheckModsPage
# ======================================================================

class CheckModsPage(ToolPageBase):
    """Run deep validation on all enabled mods."""

    def __init__(self, parent=None):
        super().__init__(
            object_name="CheckModsPage",
            title=tr("tools.check.title"),
            description=tr("tools.check.desc"),
            run_label=tr("tools.check.run"),
            parent=parent,
            title_key="tools.check.title",
            desc_key="tools.check.desc",
            run_key="tools.check.run",
        )
        # Dashboard stat cards
        self._stat_enabled = self._add_stat_card(
            "--", tr("tools.check.enabled_mods"), "#2878D0",
            label_key="tools.check.enabled_mods")
        self._stat_deltas = self._add_stat_card(
            "--", tr("tools.check.total_deltas"), "#D08770",
            label_key="tools.check.total_deltas")
        self._stat_last = self._add_stat_card(
            "--", tr("tools.check.last_check"), "#8B5CF6",
            label_key="tools.check.last_check")

    def set_managers(self, **kwargs) -> None:
        super().set_managers(**kwargs)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        if self._db:
            try:
                row = self._db.connection.execute(
                    "SELECT COUNT(*) FROM mods WHERE enabled = 1 AND mod_type = 'paz'"
                ).fetchone()
                self._stat_enabled.set_value(str(row[0]) if row else "0")
            except Exception:
                self._stat_enabled.set_value("0")
            try:
                row = self._db.connection.execute(
                    "SELECT COUNT(*) FROM mod_deltas").fetchone()
                self._stat_deltas.set_value(str(row[0]) if row else "0")
            except Exception:
                self._stat_deltas.set_value("0")
        if self._activity_log:
            try:
                entries = self._activity_log.search("Mod check")
                if entries:
                    ts = entries[0]["timestamp"]
                    self._stat_last.set_value(ts.split(" ")[0] if " " in ts else ts)
                else:
                    self._stat_last.set_value(tr("tools.stat.never"))
            except Exception:
                self._stat_last.set_value(tr("tools.stat.never"))

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        never_values = {"Never", "Nie", tr("tools.stat.never")}
        if self._stat_last._value.text() in never_values:
            self._stat_last.set_value(tr("tools.stat.never"))

    def _on_run_clicked(self) -> None:
        if not self._can_run():
            return
        if not self._db or not self._game_dir:
            self._set_status(tr("tools.check.not_configured"),
                             "#BF616A")
            return

        self._clear_results()
        self._set_running(True)

        from cdumm.storage.database import Database
        import os

        try:
            db = Database(self._db.db_path)
            db.initialize()
            issues = []

            # 1. Check vanilla file sizes
            self._set_progress(10, tr("tools.check.checking_sizes"))
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            try:
                size_rows = db.connection.execute(
                    "SELECT m.name, vs.file_path, vs.vanilla_size "
                    "FROM mod_vanilla_sizes vs JOIN mods m ON vs.mod_id = m.id "
                    "WHERE m.enabled = 1"
                ).fetchall()
                for i, (mod_name, fp, expected_size) in enumerate(size_rows):
                    if i % 10 == 0:
                        pct = 10 + int((i / max(len(size_rows), 1)) * 70)
                        self._set_progress(pct, tr("tools.progress.checking", name=fp))
                        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
                    vanilla_path = self._game_dir / "CDMods" / "vanilla" / fp.replace("/", os.sep)
                    game_path = self._game_dir / fp.replace("/", os.sep)
                    src = vanilla_path if vanilla_path.exists() else game_path
                    if src.exists():
                        actual_size = src.stat().st_size
                        if actual_size != expected_size:
                            issues.append((mod_name,
                                f"{fp} size changed ({expected_size} -> {actual_size}) — "
                                f"game updated, mod needs re-importing"))
            except Exception:
                pass

            # 2. Check delta files exist
            self._set_progress(85, tr("tools.check.checking_deltas"))
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            delta_rows = db.connection.execute(
                "SELECT m.name, md.delta_path, md.file_path "
                "FROM mod_deltas md JOIN mods m ON md.mod_id = m.id "
                "WHERE m.enabled = 1"
            ).fetchall()
            checked_paths = set()
            for mod_name, dp, fp in delta_rows:
                if dp in checked_paths:
                    continue
                checked_paths.add(dp)
                if not Path(dp).exists():
                    issues.append((mod_name, f"Missing delta file for {fp}"))

            self._set_progress(100, tr("tools.progress.done"))
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            db.close()
            self._on_check_done(issues)

        except Exception as e:
            self._on_check_error(str(e))

    def _on_check_done(self, issues: list) -> None:
        self._set_running(False)
        self._worker_thread = None

        if not issues:
            self._set_status(tr("tools.check.all_good"), "#A3BE8C")
            self._add_result_card(
                tr("tools.check.no_issues"),
                tr("tools.check.all_passed"),
                color="#A3BE8C",
            )
            self._log_activity("verify", "Mod check passed -- no issues found")
            return

        # Categorize issues
        broken_mods = set()
        for source, detail in issues:
            if source not in ("PAPGT", "Conflict", "?"):
                broken_mods.add(source)

        self._set_status(tr("tools.check.found_issues", count=len(issues)), "#BF616A")

        for source, detail in issues[:30]:
            color = "#BF616A" if source in broken_mods else "#EBCB8B"
            self._add_result_card(f"[{source}]", detail, color=color)

        if len(issues) > 30:
            self._add_result_card(
                tr("tools.check.and_more", count=len(issues) - 30),
                tr("tools.check.too_many"),
            )

        self._log_activity("warning",
            f"Mod check: {len(issues)} issue(s)",
            "; ".join(f"[{s}] {d}" for s, d in issues[:5]))

        # Offer to disable broken mods
        if broken_mods:
            self._broken_mods = broken_mods
            self._add_action_button(
                tr("tools.check.disable_broken", count=len(broken_mods)),
                self._on_disable_broken,
                primary=True,
            )

    def _on_check_error(self, error: str) -> None:
        self._set_running(False)
        self._worker_thread = None
        self._set_status(tr("tools.error", detail=error), "#BF616A")

    def _on_disable_broken(self) -> None:
        if not self._mod_manager or not hasattr(self, "_broken_mods"):
            return
        disabled = 0
        for mod in self._mod_manager.list_mods():
            if mod["name"] in self._broken_mods and mod["enabled"]:
                self._mod_manager.set_enabled(mod["id"], False)
                disabled += 1
                self._log_activity("warning",
                    f"Auto-disabled: {mod['name']}",
                    "Failed mod compatibility check")
        self._set_status(
            tr("tools.check.disabled_mods", count=disabled),
            "#A3BE8C")
        # Refresh mods page so disabled state is visible
        window = self.window()
        if hasattr(window, '_refresh_all'):
            window._refresh_all()


# ======================================================================
# FindCulpritPage
# ======================================================================

class FindCulpritPage(ToolPageBase):
    """Binary search through enabled mods to find the problematic one."""

    def __init__(self, parent=None):
        super().__init__(
            object_name="FindCulpritPage",
            title=tr("tools.culprit.title"),
            description=tr("tools.culprit.desc"),
            run_label=tr("tools.culprit.run"),
            parent=parent,
            title_key="tools.culprit.title",
            desc_key="tools.culprit.desc",
            run_key="tools.culprit.run",
        )
        # Dashboard stat cards
        self._stat_enabled = self._add_stat_card(
            "--", tr("tools.culprit.enabled_mods"), "#2878D0",
            label_key="tools.culprit.enabled_mods")
        self._stat_rounds = self._add_stat_card(
            "--", tr("tools.culprit.estimated_rounds"), "#BF616A",
            label_key="tools.culprit.estimated_rounds")

        self._auto_running = False
        self._bisect_worker = None
        self._generation = 0  # incremented on each start, used to ignore stale workers

        # Stop and Pause/Resume buttons (hidden until running)
        from qfluentwidgets import setCustomStyleSheet as _scs
        self._stop_btn = PushButton(tr("tools.culprit.stop"), self._container)
        self._stop_btn.setFixedWidth(110)
        self._stop_btn.setFixedHeight(36)
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.hide()
        _scs(self._stop_btn,
            "PushButton { background: #FFF3E0; color: #E65100; border: 1px solid #FFCC80; border-radius: 18px; padding-bottom: 6px; }"
            "PushButton:hover { background: #FFE0B2; }",
            "PushButton { background: #3E2A10; color: #FFB74D; border: 1px solid #6E4A1A; border-radius: 18px; padding-bottom: 6px; }"
            "PushButton:hover { background: #4E3418; }")

        self._pause_btn = PushButton(tr("tools.culprit.pause"), self._container)
        self._pause_btn.setFixedWidth(110)
        self._pause_btn.setFixedHeight(36)
        self._pause_btn.clicked.connect(self._on_pause_resume)
        self._pause_btn.hide()
        _scs(self._pause_btn,
            "PushButton { background: #F0F4FF; color: #2878D0; border: 1px solid #B8D4F0; border-radius: 18px; padding-bottom: 6px; }"
            "PushButton:hover { background: #E0ECFF; }",
            "PushButton { background: #1A2840; color: #5CB8F0; border: 1px solid #2A4060; border-radius: 18px; padding-bottom: 6px; }"
            "PushButton:hover { background: #223450; }")

        # Add buttons to the action row
        self._action_row.addWidget(self._stop_btn)
        self._action_row.addWidget(self._pause_btn)

    def set_managers(self, **kwargs) -> None:
        super().set_managers(**kwargs)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        if self._mod_manager:
            try:
                enabled = [m for m in self._mod_manager.list_mods() if m["enabled"]]
                n = len(enabled)
                self._stat_enabled.set_value(str(n))
                if n >= 2:
                    rounds = max(1, 2 * math.ceil(math.log2(max(n, 2))))
                    self._stat_rounds.set_value(str(rounds))
                else:
                    self._stat_rounds.set_value("N/A")
            except Exception:
                pass

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        # Re-translate stop/pause buttons (only if visible / not mid-run)
        if not self._auto_running:
            self._stop_btn.setText(tr("tools.culprit.stop"))
            self._pause_btn.setText(tr("tools.culprit.pause"))

    def _on_stop(self) -> None:
        if self._bisect_worker:
            self._bisect_worker.cancel()
        self._stop_btn.hide()
        self._pause_btn.hide()
        self._run_btn.setEnabled(False)
        self._run_btn.setText(tr("tools.culprit.stopping"))
        self._set_status(tr("tools.culprit.stopping_wait"), "#EBCB8B")
        # The thread will finish on its own (cancel flag checked in loops)
        # _on_thread_finished will clean up

    def _on_thread_finished(self) -> None:
        """Called when the QThread actually exits."""
        if hasattr(self, '_poll_timer') and self._poll_timer.isActive():
            # Drain any remaining messages
            self._poll_bisect_queue()
            self._poll_timer.stop()
        if self._auto_running:
            # Stopped by user (not by natural completion)
            self._auto_running = False
            self._set_running(False)
            self._run_btn.setText(tr("tools.culprit.run"))
            self._run_btn.setEnabled(True)
            self._stop_btn.hide()
            self._pause_btn.hide()
            self._bisect_worker = None
            self._bisect_thread = None
            self._set_status(tr("tools.culprit.stopped"), "#EBCB8B")
            self._add_result_card(tr("tools.culprit.bisection_stopped"), tr("tools.culprit.process_cancelled"), color="#EBCB8B")

    def _on_pause_resume(self) -> None:
        if not self._bisect_worker:
            return
        if self._bisect_worker._paused:
            self._bisect_worker.resume()
            self._pause_btn.setText(tr("tools.culprit.pause"))
            self._set_status(tr("tools.culprit.resumed"))
        else:
            self._bisect_worker.pause()
            self._pause_btn.setText(tr("tools.culprit.resume"))
            self._set_status(tr("tools.culprit.paused"), "#EBCB8B")

    def _on_run_clicked(self) -> None:
        if not self._can_run():
            return
        if self._auto_running:
            return

        if not self._mod_manager:
            self._set_status(tr("tools.culprit.no_manager"), "#BF616A")
            return

        enabled = [m for m in self._mod_manager.list_mods() if m["enabled"]]
        if len(enabled) < 2:
            self._clear_results()
            self._set_status(
                tr("tools.culprit.need_two_mods"), "#EBCB8B")
            return

        # Check if game is already running
        try:
            from cdumm.engine.game_monitor import find_game_process
            if find_game_process():
                self._set_status(
                    tr("tools.culprit.close_game"), "#BF616A")
                return
        except Exception:
            pass

        self._clear_results()
        self._set_running(True)
        self._auto_running = True
        self._run_btn.setText(tr("tools.running"))
        self._stop_btn.show()
        self._pause_btn.show()
        self._pause_btn.setText(tr("tools.culprit.pause"))

        # Show mod list being tested
        mod_names = [m["name"] for m in enabled]
        self._add_result_card(
            tr("tools.culprit.testing_mods", count=len(enabled)),
            "\n".join(mod_names),
        )

        # Build session and ASI mod list
        from cdumm.engine.binary_search import DeltaDebugSession
        from cdumm.gui.binary_search_dialog import _AutoBisectWorker

        asi_mods = {}
        try:
            from cdumm.asi.asi_manager import AsiManager
            bin64 = self._game_dir / "bin64"
            if bin64.exists():
                asi_mgr = AsiManager(bin64)
                plugins = asi_mgr.scan()
                for i, p in enumerate(plugins):
                    if p.enabled:
                        fake_id = -(i + 1)
                        asi_mods[fake_id] = {
                            "id": fake_id,
                            "name": f"[ASI] {p.name}",
                            "enabled": True,
                            "mod_type": "asi",
                            "_plugin": p,
                        }
        except Exception:
            pass

        session = DeltaDebugSession(
            self._mod_manager, extra_mods=list(asi_mods.values()))

        n = len(session.enabled_mods)
        estimated = max(1, 2 * math.ceil(math.log2(max(n, 2))))
        self._set_status(
            tr("tools.culprit.starting_bisection", mods=n, rounds=estimated))

        # Log card for live updates
        self._log_card = self._add_result_card(tr("tools.culprit.progress_log"), "")
        self._log_lines = []

        # Thread-safe queue for worker → UI communication
        import queue
        self._msg_queue = queue.Queue()

        worker = _AutoBisectWorker(
            session, self._mod_manager, self._game_dir,
            self._vanilla_dir, self._db,
            asi_mods=asi_mods)
        # Give worker direct access to queue (bypasses Qt signal delivery)
        worker.msg_queue = self._msg_queue
        self._bisect_worker = worker

        # Use plain Python thread — simplest, guaranteed to work
        import threading
        from PySide6.QtCore import QTimer

        def _thread_target():
            worker.run()
            self._msg_queue.put(("_thread_done", None))

        self._bisect_thread = threading.Thread(target=_thread_target, daemon=True)

        # Timer polls the queue every 200ms to update UI
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_bisect_queue)
        self._poll_timer.start(200)

        self._bisect_thread.start()

    def _poll_bisect_queue(self) -> None:
        """Drain the message queue and update UI."""
        import queue
        while True:
            try:
                msg = self._msg_queue.get_nowait()
            except queue.Empty:
                break

            if msg[0] == "log":
                self._log_lines.append(msg[1])
                if len(self._log_lines) > 50:
                    self._log_lines = self._log_lines[-50:]
                # Update the log card
                for child in self._log_card.findChildren(CaptionLabel):
                    child.setText("\n".join(self._log_lines))
                    break
                self._progress_detail.setText(msg[1])
            elif msg[0] == "progress":
                current, total = msg[1], msg[2]
                pct = int((current / max(total, 1)) * 100)
                self._progress_bar.setValue(pct)
                self._status_label.setText(tr("tools.culprit.round_progress", current=current, total=total))
            elif msg[0] == "finished":
                self._poll_timer.stop()
                self._on_bisect_finished(msg[1])
            elif msg[0] == "error":
                self._poll_timer.stop()
                self._on_bisect_error(msg[1])
            elif msg[0] == "_thread_done":
                # Thread exited (may happen after stop)
                if self._auto_running:
                    self._on_thread_finished()

    @Slot(dict)
    def _on_bisect_finished(self, result: dict) -> None:
        self._auto_running = False
        self._set_running(False)
        self._run_btn.setText(tr("tools.culprit.run"))
        self._stop_btn.hide()
        self._pause_btn.hide()
        self._bisect_worker = None
        self._bisect_thread = None

        minimal = result.get("minimal_set", [])
        rounds = result.get("rounds", 0)

        if not minimal:
            self._set_status(tr("tools.culprit.no_problems"), "#A3BE8C")
            self._add_result_card(
                tr("tools.culprit.all_clear"),
                tr("tools.culprit.all_compatible"),
                color="#A3BE8C",
            )
        elif len(minimal) == 1:
            name = minimal[0]["name"]
            self._set_status(tr("tools.culprit.found_it", name=name), "#BF616A")
            self._add_result_card(
                tr("tools.culprit.culprit_name", name=name),
                tr("tools.culprit.found_single", rounds=rounds),
                color="#BF616A",
            )
        else:
            names = ", ".join(m["name"] for m in minimal)
            self._set_status(
                tr("tools.culprit.found_multiple", count=len(minimal)), "#BF616A")
            self._add_result_card(
                tr("tools.culprit.problem_mods", count=len(minimal)),
                tr("tools.culprit.found_set", names=names, rounds=rounds),
                color="#BF616A",
            )

        # Action buttons
        self._add_action_button(tr("tools.culprit.copy_report"), self._on_copy_report)

    @Slot(str)
    def _on_bisect_error(self, msg: str) -> None:
        self._auto_running = False
        self._set_running(False)
        self._run_btn.setText(tr("tools.culprit.run"))
        self._stop_btn.hide()
        self._pause_btn.hide()
        self._bisect_worker = None
        self._set_status(tr("tools.error", detail=msg), "#BF616A")
        self._add_result_card(tr("tools.culprit.bisection_failed"), msg, color="#BF616A")

    def _on_copy_report(self) -> None:
        if self._mod_manager:
            from PySide6.QtWidgets import QApplication
            report = self._mod_manager.get_crash_report()
            QApplication.clipboard().setText(report)
            self._set_status(tr("tools.culprit.report_copied"))


# ======================================================================
# InspectModPage
# ======================================================================

class InspectModPage(ToolPageBase):
    """Validate a mod archive before importing -- read-only analysis."""

    def __init__(self, parent=None):
        super().__init__(
            object_name="InspectModPage",
            title=tr("tools.inspect.title"),
            description=tr("tools.inspect.desc"),
            run_label=tr("tools.inspect.run"),
            parent=parent,
            title_key="tools.inspect.title",
            desc_key="tools.inspect.desc",
            run_key="tools.inspect.run",
        )
        # Drag-drop hint card
        hint = CardWidget(self._container)
        hint_layout = QVBoxLayout(hint)
        hint_layout.setContentsMargins(24, 32, 24, 32)
        hint_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_title = StrongBodyLabel(".zip", hint)
        hint_title.setStyleSheet("font-size: 28px;")
        hint_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_layout.addWidget(hint_title)
        hint_text = CaptionLabel(
            tr("tools.inspect.drop_hint"), hint)
        hint_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_layout.addWidget(hint_text)
        hint.setMinimumHeight(120)
        # Insert into stats row (stretch=1 to fill equally like stat cards)
        self._stats_row.addWidget(hint, 1)

    def _on_run_clicked(self) -> None:
        if not self._can_run():
            return
        if not self._snapshot or not self._db or not self._game_dir:
            self._set_status(
                tr("tools.inspect.not_available"), "#BF616A")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, tr("tools.inspect.select_file"),
            "", tr("tools.inspect.file_filter"))
        if not path:
            return

        self._clear_results()
        self._set_running(True)
        self._set_status(tr("tools.inspect.analyzing", name=Path(path).name))

        # test_mod is fast enough to run on the main thread
        # (it's a read-only analysis, no heavy I/O)
        try:
            from cdumm.engine.test_mod_checker import (
                test_mod, generate_compatibility_report,
            )
            result = test_mod(
                Path(path), self._game_dir, self._db, self._snapshot)
            self._on_inspect_done(result, Path(path).name)
        except Exception as e:
            self._set_running(False)
            self._set_status(tr("tools.error", detail=str(e)), "#BF616A")
            self._add_result_card(tr("tools.inspect.failed"), str(e), color="#BF616A")

    def _on_inspect_done(self, result, filename: str) -> None:
        self._set_running(False)
        self._inspect_result = result

        if result.error:
            self._set_status(tr("tools.inspect.error_analyzing", name=filename), "#BF616A")
            self._add_result_card(tr("tools.inspect.error"), result.error, color="#BF616A")
            return

        self._set_status(tr("tools.inspect.analysis_complete", name=filename), "#A3BE8C")

        # Summary card
        self._add_result_card(
            tr("tools.inspect.mod_name", name=result.mod_name),
            tr("tools.inspect.files_modified", count=len(result.changed_files)) + "\n"
            + tr("tools.inspect.compatible_count", count=len(result.compatible_mods)) + "\n"
            + tr("tools.inspect.conflict_count", count=len(result.conflicts)),
            color="#A3BE8C" if not result.conflicts else "#EBCB8B",
        )

        # Changed files
        if result.changed_files:
            files_text = "\n".join(
                f.get("path", str(f)) if isinstance(f, dict) else str(f)
                for f in result.changed_files[:20]
            )
            if len(result.changed_files) > 20:
                files_text += "\n" + tr("tools.inspect.and_more", count=len(result.changed_files) - 20)
            self._add_result_card(
                tr("tools.inspect.modified_files", count=len(result.changed_files)),
                files_text,
            )

        # Conflicts
        if result.conflicts:
            for c in result.conflicts[:10]:
                self._add_result_card(
                    tr("tools.inspect.conflict"),
                    str(c.explanation) if hasattr(c, "explanation") else str(c),
                    color="#BF616A",
                )

        # Compatible mods
        if result.compatible_mods:
            self._add_result_card(
                tr("tools.inspect.compatible_with", count=len(result.compatible_mods)),
                ", ".join(result.compatible_mods[:20]),
                color="#A3BE8C",
            )

        # Export button
        self._add_action_button(tr("tools.inspect.export_report"), self._on_export_report)

    def _on_export_report(self) -> None:
        if not hasattr(self, "_inspect_result"):
            return
        from cdumm.engine.test_mod_checker import generate_compatibility_report
        report_text = generate_compatibility_report(self._inspect_result)

        path, _ = QFileDialog.getSaveFileName(
            self, tr("tools.inspect.save_report"),
            f"{self._inspect_result.mod_name}_compatibility.md",
            "Markdown (*.md)")
        if path:
            Path(path).write_text(report_text, encoding="utf-8")
            self._set_status(tr("tools.inspect.report_saved", name=Path(path).name))


# ======================================================================
# FixEverythingPage
# ======================================================================

class FixEverythingPage(ToolPageBase):
    """One-click repair: revert all game files, clear old backups, remove
    orphan directories, and optionally rescan."""

    # Signal to parent window to trigger a full rescan
    rescan_requested = Signal(bool)  # skip_verify_prompt

    def __init__(self, parent=None):
        super().__init__(
            object_name="FixEverythingPage",
            title=tr("tools.fix.title"),
            description=tr("tools.fix.desc"),
            run_label=tr("tools.fix.run"),
            parent=parent,
            title_key="tools.fix.title",
            desc_key="tools.fix.desc",
            run_key="tools.fix.run",
        )
        # Dashboard stat cards
        self._stat_backups = self._add_stat_card(
            "--", tr("tools.fix.vanilla_backups"), "#D08770",
            label_key="tools.fix.vanilla_backups")
        self._stat_state = self._add_stat_card(
            tr("tools.verify.unknown"), tr("tools.verify.game_state"), "#A3BE8C",
            label_key="tools.verify.game_state")

        self._steam_verified = False

        # Hide the default Run button — we'll use two custom buttons instead
        self._run_btn.hide()

        # Two option cards side by side
        from qfluentwidgets import setCustomStyleSheet
        options_row = QHBoxLayout()
        options_row.setSpacing(16)

        # Option 1: Quick fix (no Steam verify)
        quick_card = CardWidget(self._container)
        quick_layout = QVBoxLayout(quick_card)
        quick_layout.setContentsMargins(24, 20, 24, 20)
        quick_layout.setSpacing(10)

        quick_title = StrongBodyLabel("Quick Fix", quick_card)
        qtf = quick_title.font()
        qtf.setPixelSize(16)
        qtf.setWeight(QFont.Weight.Bold)
        quick_title.setFont(qtf)
        quick_layout.addWidget(quick_title)

        quick_desc = CaptionLabel(
            "Revert game files to vanilla, clean orphan directories.\n"
            "Use when mods are broken but game files haven't changed.",
            quick_card)
        qdf = quick_desc.font()
        qdf.setPixelSize(13)
        quick_desc.setFont(qdf)
        quick_desc.setWordWrap(True)
        quick_layout.addWidget(quick_desc)

        quick_layout.addStretch()

        self._quick_btn = PrimaryPushButton("Run Quick Fix", quick_card)
        self._quick_btn.setFixedHeight(44)
        qbf = self._quick_btn.font()
        qbf.setPixelSize(14)
        qbf.setWeight(QFont.Weight.Bold)
        self._quick_btn.setFont(qbf)
        setCustomStyleSheet(self._quick_btn,
            "PrimaryPushButton { background: #2878D0; color: white; border-radius: 12px; border: none; padding-bottom: 6px; }"
            "PrimaryPushButton:hover { background: #3388E0; }"
            "PrimaryPushButton:pressed { background: #2060B0; }",
            "PrimaryPushButton { background: #3A8FE0; color: white; border-radius: 12px; border: none; padding-bottom: 6px; }"
            "PrimaryPushButton:hover { background: #4DA0F0; }"
            "PrimaryPushButton:pressed { background: #2878D0; }")
        self._quick_btn.clicked.connect(self._on_quick_fix)
        quick_layout.addWidget(self._quick_btn)

        options_row.addWidget(quick_card, 1)

        # Option 2: Full fix (Steam verified)
        full_card = CardWidget(self._container)
        full_layout = QVBoxLayout(full_card)
        full_layout.setContentsMargins(24, 20, 24, 20)
        full_layout.setSpacing(10)

        # Title row with checkbox on the right
        full_header = QHBoxLayout()
        full_title = StrongBodyLabel("Full Reset + Rescan", full_card)
        ftf = full_title.font()
        ftf.setPixelSize(16)
        ftf.setWeight(QFont.Weight.Bold)
        full_title.setFont(ftf)
        full_header.addWidget(full_title)
        full_header.addStretch()

        from qfluentwidgets import CheckBox as FluentCheckBox
        self._full_check = FluentCheckBox(tr("tools.fix.steam_verified"), full_card)
        fcf = self._full_check.font()
        fcf.setPixelSize(12)
        self._full_check.setFont(fcf)
        self._full_check.toggled.connect(
            lambda checked: self._full_btn.setEnabled(checked))
        full_header.addWidget(self._full_check)

        full_layout.addLayout(full_header)

        full_desc = CaptionLabel(
            "Revert files, clear all backups, remove orphans,\n"
            "then take a fresh snapshot. Use after verifying\n"
            "game files through Steam.",
            full_card)
        fdf = full_desc.font()
        fdf.setPixelSize(13)
        full_desc.setFont(fdf)
        full_desc.setWordWrap(True)
        full_layout.addWidget(full_desc)

        full_layout.addStretch()

        self._full_btn = PrimaryPushButton("Run Full Reset", full_card)
        self._full_btn.setFixedHeight(44)
        fbf = self._full_btn.font()
        fbf.setPixelSize(14)
        fbf.setWeight(QFont.Weight.Bold)
        self._full_btn.setFont(fbf)
        setCustomStyleSheet(self._full_btn,
            "PrimaryPushButton { background: #2878D0; color: white; border-radius: 12px; border: none; padding-bottom: 6px; }"
            "PrimaryPushButton:hover { background: #3388E0; }"
            "PrimaryPushButton:pressed { background: #2060B0; }",
            "PrimaryPushButton { background: #3A8FE0; color: white; border-radius: 12px; border: none; padding-bottom: 6px; }"
            "PrimaryPushButton:hover { background: #4DA0F0; }"
            "PrimaryPushButton:pressed { background: #2878D0; }")
        self._full_btn.clicked.connect(self._on_full_fix)
        self._full_btn.setEnabled(False)
        full_layout.addWidget(self._full_btn)

        options_row.addWidget(full_card, 1)

        # Insert the options row before the action row
        parent_layout = self._container.layout()
        for i in range(parent_layout.count()):
            item = parent_layout.itemAt(i)
            if item and item.layout() is self._action_row:
                parent_layout.insertLayout(i, options_row)
                parent_layout.insertSpacing(i + 1, 8)
                break

    def _on_quick_fix(self) -> None:
        self._steam_verified = False
        self._on_run_clicked()

    def _on_full_fix(self) -> None:
        self._steam_verified = True
        self._on_run_clicked()

    def set_managers(self, **kwargs) -> None:
        super().set_managers(**kwargs)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        # Count vanilla backup files
        if self._vanilla_dir and self._vanilla_dir.exists():
            try:
                count = sum(1 for _ in self._vanilla_dir.rglob("*") if _.is_file())
                self._stat_backups.set_value(str(count))
            except Exception:
                self._stat_backups.set_value("0")
        else:
            self._stat_backups.set_value("0")
        # Game state from last verify
        if self._activity_log:
            try:
                entries = self._activity_log.search("Game state verified")
                if entries:
                    msg = entries[0]["message"]
                    if "ALL CLEAN" in msg:
                        self._stat_state.set_value(tr("tools.verify.clean"))
                        self._stat_state._value.setStyleSheet(
                            "font-size: 36px; color: #A3BE8C; background: transparent; border: none;")
                    else:
                        self._stat_state.set_value(tr("tools.verify.modded"))
                        self._stat_state._value.setStyleSheet(
                            "font-size: 36px; color: #BF616A; background: transparent; border: none;")
                else:
                    self._stat_state.set_value(tr("tools.verify.unknown"))
            except Exception:
                pass

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        # Re-translate game state card value
        current = self._stat_state._value.text()
        state_values = {
            tr("tools.verify.unknown"), tr("tools.verify.clean"),
            tr("tools.verify.modded"), "Unknown", "Clean", "Modded",
            "Unbekannt", "Sauber", "Gemoddet",
        }
        if current in state_values or current == "--":
            self._refresh_stats()
            if self._stat_state._value.text() in ("--", "Unknown", "Unbekannt"):
                self._stat_state.set_value(tr("tools.verify.unknown"))

    def _on_run_clicked(self) -> None:
        if not self._can_run():
            return
        import shutil

        if not self._db or not self._game_dir:
            self._set_status(tr("tools.fix.not_configured"),
                             "#BF616A")
            return

        self._clear_results()
        self._set_running(True)
        self._set_progress(5, tr("tools.fix.step1"))
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

        # Step 1: Revert
        try:
            from cdumm.engine.apply_engine import RevertWorker
            from cdumm.storage.database import Database as _DB
            revert_db = _DB(self._db.db_path)
            revert_db.initialize()
            rw = RevertWorker.__new__(RevertWorker)
            rw._game_dir = self._game_dir
            rw._vanilla_dir = self._vanilla_dir
            rw._db = revert_db
            rw._revert()
            revert_db.close()
            self._add_result_card(
                tr("tools.fix.revert_complete"), tr("tools.fix.revert_complete_desc"),
                color="#A3BE8C")
        except Exception as e:
            logger.warning("Fix: revert failed: %s", e)
            self._add_result_card(
                tr("tools.fix.revert_warning"), f"{tr('tools.fix.revert_issue')}: {e}",
                color="#EBCB8B")

        # Step 2: Clean orphan directories
        self._set_progress(40, tr("tools.fix.step2"))
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        cleaned = 0
        try:
            for d in sorted(self._game_dir.iterdir()):
                if (d.is_dir() and d.name.isdigit() and len(d.name) == 4
                        and int(d.name) >= 36):
                    snap_check = self._db.connection.execute(
                        "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                        (d.name + "/%",)).fetchone()[0]
                    if snap_check == 0:
                        shutil.rmtree(d, ignore_errors=True)
                        cleaned += 1
            if cleaned:
                self._add_result_card(
                    tr("tools.fix.cleaned_orphans", count=cleaned),
                    color="#A3BE8C")
        except Exception as e:
            self._add_result_card(
                tr("tools.fix.cleanup_warning"), str(e), color="#EBCB8B")

        # Step 3: Steam-verified extras
        if self._steam_verified:
            self._set_progress(70, tr("tools.fix.step3"))
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            try:
                if self._vanilla_dir and self._vanilla_dir.exists():
                    shutil.rmtree(self._vanilla_dir, ignore_errors=True)
                    self._vanilla_dir.mkdir(parents=True, exist_ok=True)
                self._add_result_card(
                    tr("tools.fix.backups_cleared"),
                    tr("tools.fix.backups_cleared_desc"),
                    color="#A3BE8C")
            except Exception as e:
                self._add_result_card(
                    tr("tools.fix.backup_cleanup_warning"), str(e), color="#EBCB8B")

            self._log_activity("fix",
                "Fix Everything: reverted, cleared backups, rescanning")
            self._set_progress(100, tr("tools.fix.complete_rescan"))
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            self._set_running(False)
            self._set_status(tr("tools.fix.complete_rescan"), "#A3BE8C")
            self.rescan_requested.emit(True)
        else:
            self._log_activity("fix",
                "Fix Everything: reverted and cleaned up (no rescan)")
            self._set_progress(100, tr("tools.fix.complete"))
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            self._set_running(False)
            self._set_status(tr("tools.fix.complete"), "#A3BE8C")
            # Refresh mods page so reverted state is visible
            window = self.window()
            if hasattr(window, '_refresh_all'):
                window._refresh_all()


# ======================================================================
# RescanPage
# ======================================================================

class RescanPage(ToolPageBase):
    """Create a fresh vanilla snapshot from current game files."""

    # Signal to parent window to trigger a snapshot refresh
    rescan_requested = Signal(bool)  # skip_verify_prompt

    def __init__(self, parent=None):
        super().__init__(
            object_name="RescanPage",
            title=tr("tools.rescan.title"),
            description=tr("tools.rescan.desc"),
            run_label=tr("tools.rescan.run"),
            parent=parent,
            title_key="tools.rescan.title",
            desc_key="tools.rescan.desc",
            run_key="tools.rescan.run",
        )
        # Steps info card — inserted after description, before divider
        steps_card = CardWidget(self._container)
        steps_layout = QVBoxLayout(steps_card)
        steps_layout.setContentsMargins(24, 20, 24, 20)
        steps_layout.setSpacing(12)

        steps_title = StrongBodyLabel(tr("tools.rescan.steps_title"), steps_card)
        stf = steps_title.font()
        stf.setPixelSize(15)
        stf.setWeight(QFont.Weight.Bold)
        steps_title.setFont(stf)
        steps_layout.addWidget(steps_title)

        for i, key in enumerate([
            "tools.rescan.step1", "tools.rescan.step2",
            "tools.rescan.step3", "tools.rescan.step4",
        ], 1):
            step = CaptionLabel(f"  {i}.  {tr(key)}", steps_card)
            spf = step.font()
            spf.setPixelSize(14)
            step.setFont(spf)
            steps_layout.addWidget(step)

        # Insert after description (index 2 = desc, 3 = spacing, 4 = divider)
        parent_layout = self._container.layout()
        parent_layout.insertWidget(3, steps_card)
        parent_layout.insertSpacing(4, 12)

        # Dashboard stat cards
        self._stat_files = self._add_stat_card(
            "--", tr("tools.rescan.snapshot_files"), "#2878D0",
            label_key="tools.rescan.snapshot_files")
        self._stat_last = self._add_stat_card(
            "--", tr("tools.rescan.last_scan"), "#8B5CF6",
            label_key="tools.rescan.last_scan")

        # Checkbox — Rescan button disabled until user confirms they verified
        from qfluentwidgets import CheckBox as FluentCheckBox
        self._verify_check = FluentCheckBox(
            tr("tools.fix.steam_verified"), self._container)
        vf = self._verify_check.font()
        vf.setPixelSize(15)
        vf.setWeight(QFont.Weight.DemiBold)
        self._verify_check.setFont(vf)
        self._verify_check.toggled.connect(
            lambda checked: self._run_btn.setEnabled(checked))

        # Insert centered above the Run button
        parent_layout = self._container.layout()
        for i in range(parent_layout.count()):
            item = parent_layout.itemAt(i)
            if item and item.layout() is self._action_row:
                parent_layout.insertWidget(i, self._verify_check, 0, Qt.AlignmentFlag.AlignCenter)
                parent_layout.insertSpacing(i + 1, 16)
                break

        self._run_btn.setEnabled(False)

    def set_managers(self, **kwargs) -> None:
        super().set_managers(**kwargs)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        if self._db:
            try:
                row = self._db.connection.execute(
                    "SELECT COUNT(*) FROM snapshots").fetchone()
                self._stat_files.set_value(str(row[0]) if row else "0")
            except Exception:
                self._stat_files.set_value("0")
            try:
                row = self._db.connection.execute(
                    "SELECT MAX(created_at) FROM snapshots").fetchone()
                if row and row[0]:
                    ts = row[0]
                    self._stat_last.set_value(ts.split(" ")[0] if " " in ts else ts)
                else:
                    self._stat_last.set_value(tr("tools.stat.never"))
            except Exception:
                self._stat_last.set_value(tr("tools.stat.never"))

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        never_values = {"Never", "Nie", tr("tools.stat.never")}
        if self._stat_last._value.text() in never_values:
            self._stat_last.set_value(tr("tools.stat.never"))

    def _on_run_clicked(self) -> None:
        if not self._can_run():
            return
        if not self._game_dir:
            self._set_status(tr("tools.rescan.not_configured"), "#BF616A")
            return

        self._clear_results()
        self._set_status(tr("tools.rescan.initiating"))
        self._add_result_card(
            tr("tools.rescan.requested"),
            tr("tools.rescan.requested_desc"),
        )

        # Signal parent to do the actual rescan
        self.rescan_requested.emit(True)
