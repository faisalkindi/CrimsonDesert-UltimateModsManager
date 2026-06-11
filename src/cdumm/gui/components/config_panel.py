"""Right-side sliding configuration panel for mod settings."""

from __future__ import annotations

import logging
import re as _re

logger = logging.getLogger(__name__)

_CATEGORY_PREFIX_RE = _re.compile(r"^(?P<cat>[^/]+?)\s*/\s*(?P<rest>.+)$")

_PRESET_TAG_RE = _re.compile(r"^\[(?P<tag>[^\]]+)\]\s+")
_PRESET_PERCENT_RE = _re.compile(r"^-?\d+(\.\d+)?%$")
_PRESET_KNOWN_VOCAB = frozenset(
    s.lower()
    for s in (
        "Default", "Infinite", "Off", "On", "Vanilla",
        "None", "Min", "Max", "Low", "Mid", "High",
    )
)


def _detect_same_offset_preset_family(
    patches: list[dict],
) -> dict[str, list[int]] | None:
    """Recognise a preset family encoded as "N patches at the same
    (game_file, offset) sharing a meaningful label prefix" plus any
    number of independent always-on patches at unique offsets.

    Returns ``{tag: [idx, ...], "__always_on__": [idx, ...]}`` when a
    family is found, else None. Each variant tag is the per-label
    distinguishing piece after the common prefix.

    Bug 2026-05-09 (Zowbaid, mod 356 Unlimited Dragon Flying): the
    five "Ride Duration: 30 Minutes / 60 Minutes / ..." patches all
    target offset 23357109 (mutually exclusive byte writes), but
    none use a ``[Tag]`` prefix so the legacy detector returned None
    and the user got 7 flat checkboxes.
    """
    # Bucket patches by (game_file, offset). A bucket with 2+
    # patches is a candidate mutex family — only one byte sequence
    # at one offset can win at apply time.
    by_key: dict[tuple, list[int]] = {}
    for i, p in enumerate(patches):
        gf = p.get("game_file")
        off = p.get("offset")
        if gf is None or off is None:
            return None  # not a byte-patch shape
        by_key.setdefault((gf, off), []).append(i)

    # Pick the LARGEST mutex bucket as the preset axis. CDUMM's
    # config panel UI only exposes one preset radio row; if a mod
    # had two independent preset axes we'd need a richer UI. Mod
    # 356 has exactly one (Ride Duration). Anything else falls
    # through to the second-largest, etc., later if needed.
    mutex_buckets = [
        (k, idxs) for k, idxs in by_key.items() if len(idxs) >= 2
    ]
    if not mutex_buckets:
        return None
    mutex_buckets.sort(key=lambda kv: -len(kv[1]))
    family_indices = mutex_buckets[0][1]

    # Find the longest common label prefix across the family.
    family_labels = [str(patches[i].get("label", "")) for i in family_indices]
    if not all(family_labels):
        return None
    prefix = family_labels[0]
    for s in family_labels[1:]:
        # Walk char-by-char until divergence.
        m = 0
        for a, b in zip(prefix, s):
            if a != b:
                break
            m += 1
        prefix = prefix[:m]
    # Trim trailing whitespace + common label punctuation.
    prefix = prefix.rstrip(" :-_/.,")
    # Require at least 3 meaningful chars of common prefix so we
    # don't synthesize a family from "Apple Bonus" + "Banana Bonus"
    # (no shared head). Also reject a prefix that's entirely
    # whitespace or punctuation.
    meaningful = sum(1 for c in prefix if c.isalnum())
    if meaningful < 3:
        return None

    # Distinguishing piece per variant: strip the common prefix
    # plus any leading separator punctuation/whitespace.
    def _trim_lead(s: str, head: str) -> str:
        s = s[len(head):]
        return s.lstrip(" :-_/.,")

    groups: dict[str, list[int]] = {}
    for idx in family_indices:
        label = str(patches[idx].get("label", ""))
        tag = _trim_lead(label, prefix) or label
        groups.setdefault(tag, []).append(idx)

    # Final sanity: distinct variant labels must still be 2+ after
    # the prefix strip.
    if len(groups) < 2:
        return None

    family_set = set(family_indices)
    always_on = [i for i in range(len(patches)) if i not in family_set]
    groups["__always_on__"] = always_on
    return groups


def detect_preset_groups(patches: list[dict]) -> dict[str, list[int]] | None:
    """Detect a preset selector encoded in V2 byte-patch labels.

    Two recognized shapes:

    1. ``[Tag]`` prefix on every label (mod 1103 percent presets,
       known-vocab presets like Default/Off/On, or N-equal-sized
       arbitrary tag families).
    2. Same ``(game_file, offset)`` shared by 2+ patches with a
       common label prefix (Zowbaid's mod 356 Ride Duration pack).
       Patches outside the mutex family are returned under the
       magic ``"__always_on__"`` key so the UI can keep them as
       independent toggles.

    Returns the group dict on success, or None when no preset
    family can be recognised.
    """
    groups: dict[str, list[int]] = {}
    bracket_ok = True
    for i, patch in enumerate(patches):
        label = str(patch.get("label", ""))
        m = _PRESET_TAG_RE.match(label)
        if not m:
            bracket_ok = False
            break
        tag = m.group("tag").strip()
        if not tag:
            bracket_ok = False
            break
        groups.setdefault(tag, []).append(i)

    if bracket_ok and len(groups) >= 2:
        tags = list(groups)
        if all(_PRESET_PERCENT_RE.match(t) for t in tags):
            return groups
        if all(t.lower() in _PRESET_KNOWN_VOCAB for t in tags):
            return groups
        counts = {len(idxs) for idxs in groups.values()}
        if len(tags) >= 3 and len(counts) == 1:
            return groups

    # Fall through to same-offset mutex detection (mod 356 case).
    return _detect_same_offset_preset_family(patches)


def _group_variants_by_category_prefix(
    variants: list[dict],
) -> dict[str, list[int]] | None:
    """Parse variant labels of the form '<Category> / <Rest>' and
    return {category: [variant_index, ...]} preserving discovery order.

    Returns None unless:
      * EVERY variant's label matches the pattern (mixed sets would
        leave unmatched variants orphaned in the UI), AND
      * the total set spans 2+ categories (1 category is just a flat
        list, no collapsing needed).
    """
    groups: dict[str, list[int]] = {}
    for i, v in enumerate(variants):
        label = str(v.get("label", ""))
        m = _CATEGORY_PREFIX_RE.match(label)
        if not m:
            return None
        cat = m.group("cat").strip()
        if not cat:
            return None
        groups.setdefault(cat, []).append(i)
    if len(groups) < 2:
        return None
    return groups


def compute_bulk_toggle_indices(
    all_indices: list[int],
    preset_groups: dict[str, list[int]] | None,
    always_on: list[int],
    *,
    target: bool,
) -> list[int]:
    """Indices the Select-All / Deselect-All bar should flip.

    Indices that belong to a preset family (mutex radio group) are
    excluded so a single Select-All click cannot try to enable every
    Ride Duration variant at once. Indices outside any family,
    including the always-on toggles, are eligible.

    ``target`` is whether we're moving toggles to checked (Select All)
    or unchecked (Deselect All). The same exclusion applies in both
    directions, so the parameter currently only affects how the
    caller interprets the returned indices.
    """
    excluded: set[int] = set()
    if preset_groups:
        for tag, indices in preset_groups.items():
            if tag.startswith("__"):
                continue  # __always_on__ etc.
            excluded.update(indices)
    return [i for i in all_indices if i not in excluded]


def _strip_category_prefix(label: str) -> str:
    """Return the right-hand side of 'Category / Rest' labels, else
    the label unchanged."""
    if " / " in label:
        return label.split(" / ", 1)[1]
    return label


class _CollapsibleSection:
    """Tiny collapsible block: header button + a body widget that
    toggles visibility on click. Not a widget itself — callers layout
    the header and body separately. Kept deliberately simple so the
    Apply-theme pass on the parent ConfigPanel recolours the header
    label alongside every other text widget.
    """

    def __init__(self, title: str, count: int, *, start_expanded: bool):
        from PySide6.QtWidgets import QPushButton, QWidget, QVBoxLayout
        self._title = title
        self._count = count
        self._expanded = start_expanded
        self.header = QPushButton()
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        # Explicit theme-aware text color — the default QPushButton color
        # didn't inherit the ConfigPanel stylesheet and rendered
        # white-on-white in light mode. isDarkTheme() is sampled at
        # build time; _apply_theme on the parent ConfigPanel re-runs
        # show_variant_mod on theme flips which rebuilds these
        # sections from scratch.
        from qfluentwidgets import isDarkTheme
        _fg = "#E2E8F0" if isDarkTheme() else "#1A202C"
        self.header.setStyleSheet(
            f"QPushButton {{ text-align: left; padding: 10px 8px; "
            f"border: none; background: transparent; color: {_fg}; "
            f"font-weight: bold; font-size: 13px; }} "
            f"QPushButton:hover {{ background: rgba(128,128,128,0.08); "
            f"border-radius: 4px; }}"
        )
        self.body = QWidget()
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(18, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self.header.clicked.connect(self._toggle)
        self._refresh_header()
        self.body.setVisible(self._expanded)

    def add_row(self, widget) -> None:
        self._body_layout.addWidget(widget)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.body.setVisible(self._expanded)
        self._refresh_header()

    def _refresh_header(self) -> None:
        arrow = "\u25BE" if self._expanded else "\u25B8"   # ▾ / ▸
        self.header.setText(f"{arrow}  {self._title}    ({self._count})")


def _is_apply_visible(
    variant_widgets: dict,
    variant_initial: dict,
    label_dirty: set,
) -> bool:
    """Return True if the Apply button should be shown.

    Apply is visible when EITHER:
      * the variant radio differs from the initial snapshot, OR
      * any variant's labels were edited via the Configure picker
        (tracked in label_dirty — a set of variant filenames).

    Previously only the variant-changed branch existed, so a user who
    edited labels and then reverted their variant pick to initial lost
    the Apply button and dropped their label edits. Both conditions
    now count as dirty.
    """
    for idx, widget in variant_widgets.items():
        try:
            if widget.isChecked() != variant_initial.get(idx, False):
                return True
        except AttributeError:
            # test fixture may pass booleans directly
            if widget != variant_initial.get(idx, False):
                return True
    return bool(label_dirty)

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    FlowLayout,
    PrimaryPushButton,
    RadioButton,
    SingleDirectionScrollArea,
    SubtitleLabel,
    isDarkTheme,
)

from cdumm.i18n import tr

# Stable internal sentinel persisted to the config DB for the "manual
# per-checkbox control" preset radio. MUST stay "Custom" forever: the
# value is stored under ``mod_<id>_preset`` and compared on load, so
# existing user DBs already contain it. The DISPLAY text of the radio
# is translated separately (``config_panel.preset_custom``); the radio
# carries this sentinel in a ``preset_tag`` Qt property.
CUSTOM_PRESET_TAG = "Custom"


# ── Colour helpers ────────────────────────────────────────────────────

def _bg() -> str:
    return "#14171E" if isDarkTheme() else "#FAFBFC"


def _left_border() -> str:
    return "#2D3340" if isDarkTheme() else "#E5E7EB"


def _section_color() -> str:
    return "#5CB8F0" if isDarkTheme() else "#2878D0"


def _row_border() -> str:
    return "#252830" if isDarkTheme() else "#F3F4F6"


# ── Badge helper ──────────────────────────────────────────────────────

def _make_badge(text: str, bg: str = "#2878D0", fg: str = "#FFFFFF") -> QLabel:
    badge = QLabel(text)
    badge.setStyleSheet(
        f"background: {bg}; color: {fg}; border-radius: 4px; "
        f"padding: 2px 8px; font-size: 11px; font-weight: 600;"
    )
    badge.setFixedHeight(20)
    return badge


# ======================================================================
# Resize handle (Task 2.1)
# ======================================================================

class _ResizeHandle(QWidget):
    """An 8-pixel-wide drag handle on the panel's right edge with a
    1 px visible centerline so users can SEE where to grab.

    On press, snapshots the panel's current width and the global mouse
    X. On move, computes ``new_width = start_width + (cursor_dx)``.
    Because the panel is anchored on the RIGHT side of the main window
    (it grows leftward as width increases), dragging RIGHT shrinks the
    panel and dragging LEFT grows it from the user's perspective. We
    flip the delta sign so the gesture matches user intuition: drag
    LEFT to make the panel wider, drag RIGHT to shrink it.

    Bug #5 (scottykyzer + Bekwit, Nexus 2026-05-09 / 2026-05-10): the
    original 4 px translucent handle was invisible, so users reported
    "It can't be resized." Widening to 8 px and painting a faint
    centerline gives the gesture a discoverable affordance. Hover
    brightens the line for stronger feedback.
    """

    def __init__(self, parent_panel) -> None:
        super().__init__(parent_panel)
        self._panel = parent_panel
        self._drag_start_x: float | None = None
        self._drag_start_width: int | None = None
        self._hovered: bool = False
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        # 8 px gives both a usable hit zone and room for a visible
        # centerline that doesn't sit flush against the panel edge.
        self.setFixedWidth(8)
        # Track hover for paintEvent feedback. Without this we never
        # get enterEvent/leaveEvent on a plain QWidget.
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def enterEvent(self, e):  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):  # noqa: N802
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, e):  # noqa: N802
        # Paint a 1 px vertical line centered in the handle. Theme-aware
        # muted gray; brightens on hover so the user can confirm the
        # handle is interactive. We deliberately do NOT fill the whole
        # rect — the panel's own background should show through so the
        # handle reads as a subtle separator, not a chunky bar.
        painter = QPainter(self)
        try:
            dark = isDarkTheme()
            if dark:
                base = QColor(180, 180, 180, 90)
                hover = QColor(220, 220, 220, 180)
            else:
                base = QColor(120, 120, 120, 90)
                hover = QColor(70, 70, 70, 180)
            color = hover if self._hovered else base
            painter.setPen(color)
            cx = self.width() // 2
            painter.drawLine(cx, 0, cx, self.height())
        finally:
            painter.end()

    def mousePressEvent(self, e):  # noqa: N802
        self._drag_start_x = e.globalPosition().x()
        self._drag_start_width = self._panel.width()
        e.accept()

    def mouseMoveEvent(self, e):  # noqa: N802
        # Gate on left-button held. Without this guard, a press that's
        # dropped by Qt (focus loss, modal popup eating the release,
        # etc.) leaves _drag_start_x set, and the next stray move event
        # — even one with no buttons held — would resize the panel.
        # Treat any move without LeftButton as a stale event and bail.
        if not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._drag_start_x is None or self._drag_start_width is None:
            return
        # Cursor delta in screen coords. Panel is right-anchored, so
        # invert: dragging the handle LEFT (negative dx) widens the
        # panel. Mid-drag set_panel_width() updates the live width
        # without persisting; persist is reserved for release so we
        # get one SQLite write per gesture, not one per pixel.
        dx = e.globalPosition().x() - self._drag_start_x
        new_width = int(self._drag_start_width - dx)
        self._panel.set_panel_width(new_width)
        e.accept()

    def mouseReleaseEvent(self, e):  # noqa: N802
        # Persist the final width once on release. Mid-drag mouseMove
        # called set_panel_width() without persisting, so a 200-pixel
        # drag produced ~200 width updates but zero DB writes; this
        # release commits the chosen width with a single Config.set().
        if self._drag_start_x is not None:
            self._panel.persist_panel_width()
        self._drag_start_x = None
        self._drag_start_width = None
        e.accept()


# ======================================================================
# ConfigPanel
# ======================================================================

class ConfigPanel(QWidget):
    """Animated right-side panel for mod configuration.

    Width animates between 0 (closed) and 310 (open).
    """

    panel_closed = Signal()
    apply_clicked = Signal(int, list)  # mod_id, [{"label": str, "enabled": bool}]
    variants_apply_clicked = Signal(int, list)  # mod_id, [{label, filename, enabled, group}]

    # Bug from nknwn issue #48 (2026-04-26): config option labels
    # like 'Insect_Collect_Cooldown' truncated at ~16 chars in the
    # 400px panel. Bumped to 520 then to 640 because option labels
    # in non-Latin scripts (Korean, Japanese, Simplified Chinese)
    # render visually wider per-character than English at the same
    # font size, AND because the CC - Female Armor Expansion mod
    # ships option names with the full file path — both cases hit
    # the ellipsis at 520px even though wordWrap=True is set on the
    # label. Nexus reports: Malowded + AsteiosSaber 2026-05-05.
    _DEFAULT_PANEL_WIDTH = 640
    _MIN_PANEL_WIDTH = 480
    _MAX_PANEL_WIDTH = 1200
    # Class-level fallback so any pre-__init__ access (or subclass that
    # forgets to chain super) still finds a sane default. Instances
    # shadow this with their own _PANEL_WIDTH set in __init__ so the
    # drag handle can mutate it per-panel without touching the class.
    _PANEL_WIDTH = _DEFAULT_PANEL_WIDTH
    _ANIM_DURATION = 250

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Per-instance width — Task 2.1 made this user-resizable via the
        # drag handle on the right edge. Class-level _PANEL_WIDTH stays
        # as a fallback default but the instance attr is what the
        # animation and handle read/write.
        self._PANEL_WIDTH = self._DEFAULT_PANEL_WIDTH
        self.setMaximumWidth(0)
        self.setMinimumWidth(0)
        self.setVisible(False)

        # Optional DB ref for persisting per-mod preferences (e.g.
        # last-clicked preset radio). When None, persistence is a no-op
        # so legacy call sites that never wire a DB still work.
        self._db = None

        self._mod_id: int = 0
        self._initial_states: dict[int, bool] = {}
        self._toggles: dict[int, QCheckBox] = {}
        self._labels: dict[int, str] = {}
        self._value_inputs: dict[int, QSpinBox | QDoubleSpinBox] = {}
        self._initial_values: dict[int, int | float] = {}
        # Variant-mode bookkeeping (populated by show_variant_mod).
        self._variant_mode: bool = False
        self._variants_meta: list[dict] = []
        self._variant_widgets: dict[int, QCheckBox | QRadioButton] = {}
        self._variant_initial: dict[int, bool] = {}
        # Preset-selector state (populated by _add_preset_selector when
        # detect_preset_groups returns a non-None mapping). Task 1.3 will
        # consume these to map a clicked radio back to the patch indices
        # that its tag covers.
        self._preset_radio_group: QButtonGroup | None = None
        self._preset_groups: dict[str, list[int]] | None = None
        self._preset_always_on_indices: list[int] = []

        self._anim = QPropertyAnimation(self, b"maximumWidth")
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.setDuration(self._ANIM_DURATION)
        # Tracks whether _emit_closed is currently connected to
        # _anim.finished. close_panel() sets True; show_*() sets False
        # after disconnect. Without this flag, every show_*() unconditionally
        # called disconnect() on a fresh panel and PySide6 emitted
        # RuntimeWarning("Failed to disconnect ... from signal 'finished()'")
        # — the try/except RuntimeError caught the exception but the warning
        # is emitted before the raise.
        self._closed_handler_connected = False

        self._build_ui()
        self._apply_theme()

        # Drag handle on the right edge for user-resizable width. Created
        # AFTER _build_ui so it sits on top of the scroll area's right
        # margin. resizeEvent below repositions it whenever the panel
        # resizes (animation, parent layout, manual setMaximumWidth).
        self._resize_handle = _ResizeHandle(self)
        self._resize_handle.raise_()
        self._resize_handle.show()

        # Theme flip: collapsible section headers bake isDarkTheme()
        # into their stylesheet at build time, so the arrows and text
        # keep the OLD colour after a Windows theme flip. Rebuild the
        # variant view when qconfig.themeChanged fires so the headers
        # pick up the new colour immediately. GDS #12.
        try:
            from qfluentwidgets.common.config import qconfig
            qconfig.themeChanged.connect(self._on_theme_changed)
        except Exception as _e_th:
            logger.debug("themeChanged wiring skipped: %s", _e_th)

    def _on_theme_changed(self, *_a) -> None:
        """Re-apply theme-aware styles and rebuild the collapsible
        sections if we're showing a variant mod right now. The section
        headers inherit colour from the parent stylesheet at build
        time, so just calling _apply_theme isn't enough — we need
        to reconstruct the QPushButton stylesheets with the new
        isDarkTheme() value."""
        self._apply_theme()
        if (self._variant_mode and self._variants_meta
                and getattr(self, "_collapsible_sections", None)):
            # Re-sync each section header's stylesheet with the
            # current theme. Cheaper than a full show_variant_mod
            # rebuild and preserves the user's expand/collapse state.
            from qfluentwidgets import isDarkTheme
            _fg = "#E2E8F0" if isDarkTheme() else "#1A202C"
            for section in self._collapsible_sections:
                try:
                    section.header.setStyleSheet(
                        f"QPushButton {{ text-align: left; padding: 10px 8px; "
                        f"border: none; background: transparent; color: {_fg}; "
                        f"font-weight: bold; font-size: 13px; }} "
                        f"QPushButton:hover {{ background: rgba(128,128,128,0.08); "
                        f"border-radius: 4px; }}"
                    )
                except (AttributeError, RuntimeError):
                    continue

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        # ── Close button ──────────────────────────────────────────────
        close_row = QHBoxLayout()
        close_row.addStretch()
        self._close_btn = QPushButton("\u2715")
        self._close_btn.setFixedSize(28, 28)
        self._close_btn.clicked.connect(self.close_panel)
        close_row.addWidget(self._close_btn)
        root.addLayout(close_row)

        # ── Mod title + author ────────────────────────────────────────
        self._title_label = SubtitleLabel("")
        self._title_label.setWordWrap(True)
        root.addWidget(self._title_label)

        self._author_label = CaptionLabel("")
        self._author_label.setStyleSheet("font-size: 12px; color: #8B95A5;")
        root.addWidget(self._author_label)

        # ── Stat badges ───────────────────────────────────────────────
        self._badge_row = QHBoxLayout()
        self._badge_row.setSpacing(6)
        self._badge_row.addStretch()
        root.addLayout(self._badge_row)

        # ── Scrollable body ───────────────────────────────────────────
        scroll = SingleDirectionScrollArea(orient=Qt.Orientation.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(SingleDirectionScrollArea.Shape.NoFrame)
        # No horizontal scrollbar — long labels wrap instead of overflowing.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_layout = QVBoxLayout(self._body)
        # 14px right padding so the scrollbar track doesn't sit on top
        # of radio / checkbox indicators at the right edge of each row.
        # Qt's default vertical scrollbar is 12-14px wide on Windows;
        # this clearance keeps the scrollbar and the indicators in
        # separate columns.
        self._body_layout.setContentsMargins(0, 0, 14, 0)
        self._body_layout.setSpacing(0)
        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)

        # ── Apply button ──────────────────────────────────────────────
        self._apply_btn = PrimaryPushButton(tr("config_panel.apply_changes"))
        self._apply_btn.setVisible(False)
        self._apply_btn.clicked.connect(self._on_apply)
        root.addWidget(self._apply_btn)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_db(self, db) -> None:
        """Wire a Database instance into the panel.

        Used by ``mods_page`` so the panel can persist per-mod
        preferences (e.g. the last-clicked preset radio) via the
        existing key-value ``Config`` table. Safe to call before or
        after the first ``show_mod``. When a panel is constructed
        without a DB, persistence calls are no-ops.

        Also restores the global ``config_panel_width`` if previously
        saved. We write directly to ``self._PANEL_WIDTH`` instead of
        going through ``set_panel_width`` to avoid round-trip persisting
        the same value we just loaded.
        """
        self._db = db
        if db is None:
            return
        try:
            from cdumm.storage.config import Config
            saved = Config(db).get("config_panel_width")
            # Use ``is not None`` rather than truthy check: an empty
            # string saved value (e.g. from a stray Config.set("", ""))
            # would previously skip restoration entirely. Now we hand
            # any non-None value to int(); int("") raises ValueError
            # and we fall through to leave the default in place — which
            # is the correct behaviour for empty strings too, but via
            # the explicit error path instead of the truthy short-circuit.
            if saved is not None:
                try:
                    width = int(saved)
                    self._PANEL_WIDTH = max(
                        self._MIN_PANEL_WIDTH,
                        min(self._MAX_PANEL_WIDTH, width),
                    )
                except (ValueError, TypeError):
                    # Garbage value — leave the default in place.
                    pass
        except Exception as e:
            logger.debug("set_db: width restore failed: %s", e)

    def show_mod(
        self,
        mod_id: int,
        name: str,
        author: str,
        version: str,
        status: str,
        file_count: int,
        patches: list[dict],
        conflicts: list[str],
    ) -> None:
        """Populate the panel with mod data and animate it open."""
        self._mod_id = mod_id
        self._initial_states.clear()
        self._toggles.clear()
        self._labels.clear()
        self._value_inputs.clear()
        self._initial_values.clear()
        self._variant_mode = False
        # Reset preset-selector refs every open. Detection runs again
        # below; a flat-mod open after a preset-mod open must clear the
        # stale QButtonGroup.
        self._preset_radio_group = None
        self._preset_groups = None
        self._preset_always_on_indices = []
        self._apply_btn.setVisible(False)

        # Header
        self._title_label.setText(name)
        self._author_label.setText(
            tr("config_panel.by_author", author=author) if author else "")

        # Badges
        self._clear_badges()
        self._badge_row.insertWidget(0, _make_badge(status))
        self._badge_row.insertWidget(1, _make_badge(f"v{version}", "#444C5C"))
        self._badge_row.insertWidget(
            2, _make_badge(
                tr("config_panel.n_files", count=file_count), "#444C5C"),
        )

        # Rebuild body
        self._clear_body()

        # CONFIGURATION section
        if patches:
            self._add_section_header(tr("config_panel.section_config"))
            # Preset selector: when every label carries a [Tag] prefix
            # forming a recognisable preset family (percent / known
            # vocab / N-equal-sized groups), show a radio row above the
            # per-patch toggles. The radios themselves do nothing yet;
            # Task 1.3 wires them to actually drive the toggles.
            preset_groups = detect_preset_groups(patches)
            if preset_groups is not None:
                # Restore the last-clicked preset for this mod (Task
                # 1.4). When there is no saved value, _add_preset_selector
                # falls back to the "Custom" radio.
                current_preset = None
                if self._db is not None and self._mod_id is not None:
                    try:
                        from cdumm.storage.config import Config
                        current_preset = Config(self._db).get(
                            f"mod_{self._mod_id}_preset")
                    except Exception:
                        current_preset = None
                self._add_preset_selector(
                    preset_groups, current_preset=current_preset)
            # Select-All / Deselect-All bar. Only shows for mods with
            # 5+ independent toggles; under that threshold the user
            # can just click each one (scottykyzer Nexus 2026-05-09:
            # "scroll in a tiny panel for days and days clicking
            # and clicking" against Simple BackPack Visual Swap).
            independent_count = len(compute_bulk_toggle_indices(
                list(range(len(patches))), preset_groups,
                getattr(self, "_preset_always_on_indices", []),
                target=True))
            if independent_count >= 5:
                self._add_select_all_bar(patches, preset_groups)
            for i, p in enumerate(patches):
                ev = p.get("editable_value")
                if ev and isinstance(ev, dict) and "type" in ev:
                    self._add_editable_row(
                        i, p["label"], p.get("description", ""),
                        ev, p.get("custom_value"))
                else:
                    self._add_config_row(i, p["label"], p.get("description", ""), p["enabled"])

        # CONFLICTS section
        if conflicts:
            self._add_section_header(tr("config_panel.section_conflicts"))
            for desc in conflicts:
                lbl = CaptionLabel(desc)
                lbl.setWordWrap(True)
                lbl.setStyleSheet("color: #D04848; padding: 4px 0;")
                self._body_layout.addWidget(lbl)

        self._body_layout.addStretch()

        # Apply theme-aware background
        self._apply_theme()

        # Animate open (width + opacity)
        self.setVisible(True)
        self._anim.stop()
        self._disconnect_closed_handler()
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(self._PANEL_WIDTH)

        # Opacity fade-in
        # Create a fresh effect each time (previous one is deleted by setGraphicsEffect(None))
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(0.0)

        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_anim.setDuration(self._ANIM_DURATION)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(lambda: self.setGraphicsEffect(None))

        self._anim.start()
        self._fade_anim.start()

    def close_panel(self) -> None:
        """Animate the panel closed and emit ``panel_closed``."""
        self._anim.stop()
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(0)
        self._anim.finished.connect(self._emit_closed, Qt.ConnectionType.UniqueConnection)
        self._closed_handler_connected = True
        self._anim.start()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_closed(self) -> None:
        if self.maximumWidth() == 0:
            self.setVisible(False)
            self.panel_closed.emit()

    def _disconnect_closed_handler(self) -> None:
        """Disconnect _emit_closed from _anim.finished if currently
        connected. No-op (no warning) when not connected. Used by every
        show_*() method before re-arming the open animation."""
        if self._closed_handler_connected:
            try:
                self._anim.finished.disconnect(self._emit_closed)
            except RuntimeError:
                # Signal was destroyed between our flag and the call;
                # treat as already-disconnected.
                pass
            self._closed_handler_connected = False

    # ------------------------------------------------------------------
    # User-resizable width (Task 2.1)
    # ------------------------------------------------------------------

    def set_panel_width(self, width: int, *, persist: bool = False) -> None:
        """Set the panel's target width, clamped to [MIN, MAX].

        Updates the instance ``_PANEL_WIDTH`` so subsequent open
        animations land at the new width, and — when the panel is
        currently visible (maximumWidth > 0) — applies the new width
        immediately via ``setMaximumWidth`` so the drag feels live.

        ``persist`` defaults to ``False`` because the resize handle
        calls this method per pixel of cursor motion during a drag —
        persisting on every call would issue ~200 SQLite writes for a
        single 200-pixel gesture. Drag callers should call
        :meth:`persist_panel_width` once on mouse release to commit
        the final value. Direct callers (e.g. a settings dialog
        committing a numeric width) can pass ``persist=True`` for the
        legacy write-through behaviour.
        """
        clamped = max(self._MIN_PANEL_WIDTH,
                      min(self._MAX_PANEL_WIDTH, int(width)))
        self._PANEL_WIDTH = clamped
        # Apply live whenever the panel is visible (i.e. show_mod has
        # been called). The width animation runs on the same
        # ``maximumWidth`` property, so a plain setMaximumWidth would
        # be immediately overwritten by the next animation tick when
        # the open tween is still in flight. Stop the animation before
        # writing the new width — but ONLY when its target is non-zero
        # (the close animation targets 0 and we don't want a mid-drag
        # set to interfere with closing).
        if self.isVisible():
            anim = getattr(self, "_anim", None)
            if (anim is not None
                    and anim.state() == anim.State.Running
                    and anim.endValue() not in (None, 0)):
                anim.stop()
            self.setMaximumWidth(clamped)
        if persist:
            self.persist_panel_width()

    def persist_panel_width(self) -> None:
        """Write the current ``_PANEL_WIDTH`` to the config DB.

        Called by ``_ResizeHandle.mouseReleaseEvent`` once per drag
        gesture, and (via ``persist=True``) by direct width-setters
        like a future settings dialog. No-op when no DB has been wired
        in (legacy callers that constructed the panel without
        ``set_db``).
        """
        if self._db is None:
            return
        try:
            from cdumm.storage.config import Config
            Config(self._db).set(
                "config_panel_width", str(self._PANEL_WIDTH))
        except Exception as e:
            logger.debug("persist_panel_width failed: %s", e)

    def resizeEvent(self, event):  # noqa: N802
        """Reposition the right-edge drag handle on every resize."""
        super().resizeEvent(event)
        handle = getattr(self, "_resize_handle", None)
        if handle is not None:
            w = handle.width()
            handle.setGeometry(self.width() - w, 0, w, self.height())
            handle.raise_()

    def _apply_theme(self) -> None:
        dark = isDarkTheme()
        text_color = "#E0E0E0" if dark else "#1A1A2E"
        caption_color = "#9BA4B5" if dark else "#8B95A5"
        close_color = "#9BA4B5" if dark else "#6B7280"
        close_hover = "#5CB8F0" if dark else "#2878D0"
        self.setStyleSheet(
            f"ConfigPanel {{ background: {_bg()}; "
            f"border-left: 1px solid {_left_border()}; }}"
            f"ConfigPanel QLabel {{ color: {text_color}; }}"
        )
        self._title_label.setStyleSheet(f"color: {text_color}; font-size: 16px; font-weight: bold;")
        self._author_label.setStyleSheet(f"color: {caption_color}; font-size: 12px;")
        self._close_btn.setStyleSheet(
            f"QPushButton {{ border: none; font-size: 16px; color: {close_color}; }}"
            f"QPushButton:hover {{ color: {close_hover}; }}"
        )

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == event.Type.ApplicationPaletteChange:
            self._apply_theme()

    def _clear_badges(self) -> None:
        while self._badge_row.count() > 1:  # keep the stretch
            item = self._badge_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _clear_body(self) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                ConfigPanel._clear_layout(item.layout())

    def _add_preset_selector(
        self,
        groups: dict[str, list[int]],
        current_preset: str | None = None,
    ) -> None:
        """Render a horizontal radio row at the top of the body, one
        radio per preset group plus a final 'Custom' radio.

        - Each radio's text is the tag (e.g. "0%", "Off", "Lazy Run").
        - The final 'Custom' radio is selected by default. Custom means
          manual per-checkbox control (Task 1.3 will wire this).
        - When ``current_preset`` matches a tag in ``groups``, that
          radio is checked instead of Custom (used in Task 1.4 for
          restoring the last selection).
        - The ``QButtonGroup`` is stored at ``self._preset_radio_group``
          so Task 1.3 can connect to its ``buttonClicked`` signal.
        - The group dict is stashed at ``self._preset_groups`` so Task
          1.3 can look up the patch indices for each tag.
        """
        # The button group is parented to ``self`` so it survives the
        # body-clear cycle on the next show_mod call without leaking;
        # we reset the attr explicitly at the start of show_mod.
        # Magic key from detect_preset_groups: indices of patches that
        # live OUTSIDE the preset family (mod 356 Cooldown + HP Regen).
        # These must keep whatever per-checkbox state the user set;
        # picking a Ride Duration radio shouldn't unset Cooldown.
        always_on = list(groups.get("__always_on__", []))
        groups = {k: v for k, v in groups.items() if not k.startswith("__")}
        self._preset_groups = dict(groups)
        self._preset_always_on_indices = always_on
        button_group = QButtonGroup(self)
        button_group.setExclusive(True)
        button_group.buttonClicked.connect(self._on_preset_selected)
        self._preset_radio_group = button_group

        # FlowLayout instead of QHBoxLayout: when the row of radios
        # fits on one line it behaves identically; when it can't (lots
        # of preset tags, narrow panel width) it wraps to additional
        # rows so every radio stays reachable. wootwoots reported on
        # Nexus 2026-05-08 that mod 1103 (12 percent presets + Custom)
        # had the rightmost radios clipped off the panel with no
        # scroll, no wrap. Mod author's suggested workaround was a
        # vertical stack like JMM; FlowLayout is the same idea but
        # only wraps when actually needed.
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        flow = FlowLayout(container, needAni=False, isTight=True)
        flow.setContentsMargins(0, 4, 0, 8)
        flow.setHorizontalSpacing(12)
        flow.setVerticalSpacing(6)

        match_tag = current_preset if current_preset in groups else None
        for tag in groups.keys():
            rb = RadioButton(tag)
            # Stable lookup/persist value, independent of display text.
            rb.setProperty("preset_tag", tag)
            if match_tag is not None and tag == match_tag:
                rb.setChecked(True)
            button_group.addButton(rb)
            flow.addWidget(rb)

        # Display text is translated; the persisted sentinel stays the
        # stable CUSTOM_PRESET_TAG constant (see its definition above).
        # Same graceful fallback preset_picker uses: when translations
        # aren't loaded (bare unit tests), show the sentinel itself
        # instead of the raw i18n key.
        _custom_label = tr("config_panel.preset_custom")
        if _custom_label == "config_panel.preset_custom":
            _custom_label = CUSTOM_PRESET_TAG
        custom_rb = RadioButton(_custom_label)
        custom_rb.setProperty("preset_tag", CUSTOM_PRESET_TAG)
        # Custom is the default fallback when no current_preset matches
        # a known tag — i.e. on a fresh open before Task 1.4 lands the
        # restore-from-DB hook.
        if match_tag is None:
            custom_rb.setChecked(True)
        button_group.addButton(custom_rb)
        flow.addWidget(custom_rb)

        self._body_layout.addWidget(container)

    def _on_preset_selected(self, button) -> None:
        """Apply the clicked preset's toggle state. Custom = no-op.

        Looks up the clicked radio's tag in ``self._preset_groups`` and
        sets every patch toggle to checked iff its index is in that
        group's index list. The "Custom" radio means manual control —
        we leave existing toggles alone so the user retains whatever
        per-checkbox state they had.

        Also persists the clicked tag (including "Custom") to the
        config DB so the next ``show_mod`` for the same mod restores
        the same radio. No-op when no DB has been wired in.
        """
        # Read the stable tag from the Qt property, not the (possibly
        # translated) display text. Falls back to text() for safety.
        tag = button.property("preset_tag") or button.text()
        self._save_preset_selection(tag)
        if tag == CUSTOM_PRESET_TAG:
            return
        if not self._preset_groups:
            return
        enable_indices = set(self._preset_groups.get(tag, []))
        # Skip patches that aren't part of the preset family (mod 356
        # always-on patches like Cooldown / HP Regen). The user's
        # per-checkbox state for those is preserved.
        always_on = set(getattr(self, "_preset_always_on_indices", []))
        for idx, toggle in self._toggles.items():
            if idx in always_on:
                continue
            toggle.setChecked(idx in enable_indices)

    def _save_preset_selection(self, tag: str) -> None:
        """Persist the user's preset choice for the current mod.

        Stored as a plain string under ``mod_<id>_preset`` in the
        existing key-value ``config`` table. Silent no-op when the
        panel was opened without a DB or before a mod was loaded.
        """
        if self._db is None or not self._mod_id:
            return
        try:
            from cdumm.storage.config import Config
            Config(self._db).set(f"mod_{self._mod_id}_preset", tag)
        except Exception as e:
            logger.debug("Could not persist preset selection: %s", e)

    def _add_select_all_bar(
        self,
        patches: list[dict],
        preset_groups: dict[str, list[int]] | None,
    ) -> None:
        """Render a Select-All / Deselect-All button row above the
        patch list. Indices that belong to a preset family are
        skipped so the radio choice survives a bulk click.
        """
        from qfluentwidgets import PushButton

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 4, 0, 8)
        bar.setSpacing(8)

        target_indices = compute_bulk_toggle_indices(
            list(range(len(patches))), preset_groups,
            getattr(self, "_preset_always_on_indices", []),
            target=True)

        sel_btn = PushButton(tr("config_panel.select_all"))
        sel_btn.setFixedHeight(26)
        sel_btn.clicked.connect(
            lambda _checked=False, idxs=target_indices:
                self._bulk_set_toggles(idxs, True))
        bar.addWidget(sel_btn)

        des_btn = PushButton(tr("config_panel.deselect_all"))
        des_btn.setFixedHeight(26)
        des_btn.clicked.connect(
            lambda _checked=False, idxs=target_indices:
                self._bulk_set_toggles(idxs, False))
        bar.addWidget(des_btn)

        bar.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(bar)
        self._body_layout.addWidget(wrap)

    def _bulk_set_toggles(
        self, indices: list[int], target: bool,
    ) -> None:
        for idx in indices:
            tog = self._toggles.get(idx)
            if tog is not None:
                tog.setChecked(target)

    def _add_section_header(self, text: str) -> None:
        header = CaptionLabel(text)
        header.setStyleSheet(
            f"color: {_section_color()}; font-weight: 700; "
            f"letter-spacing: 0.5px; padding: 12px 0 6px 0; "
            f"text-transform: uppercase; font-size: 11px;"
        )
        self._body_layout.addWidget(header)

    def _add_config_row(self, index: int, label: str, description: str, enabled: bool) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(10)

        # Toggle checkbox — placed FIRST so it stays visible when the panel
        # gets clipped by a narrow main window. Right-aligned toggles used
        # to slide off-screen for users with small windows (vanishdark on
        # Nexus #26 — labels wrapped, indicator vanished).
        toggle = QCheckBox()
        toggle.setChecked(enabled)
        unchecked_border = "#5A6270" if isDarkTheme() else "#9CA3AF"
        toggle.setStyleSheet(
            "QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px; }"
            f"QCheckBox::indicator:checked {{ background: #2878D0; border: 2px solid #2878D0; border-radius: 4px; }}"
            f"QCheckBox::indicator:unchecked {{ background: transparent; border: 2px solid {unchecked_border}; border-radius: 4px; }}"
        )
        toggle.toggled.connect(self._on_toggle_changed)
        row.addWidget(toggle, 0, Qt.AlignmentFlag.AlignTop)

        # Label + description column (wraps to fit available width)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = CaptionLabel(label)
        name_lbl.setWordWrap(True)
        name_lbl.setMinimumWidth(1)
        nf = name_lbl.font()
        nf.setPixelSize(13)
        from PySide6.QtGui import QFont
        nf.setWeight(QFont.Weight.DemiBold)
        name_lbl.setFont(nf)
        text_col.addWidget(name_lbl)
        if description:
            desc_lbl = CaptionLabel(description)
            desc_lbl.setWordWrap(True)
            desc_lbl.setMinimumWidth(1)
            df = desc_lbl.font()
            df.setPixelSize(11)
            desc_lbl.setFont(df)
            text_col.addWidget(desc_lbl)
        row.addLayout(text_col, 1)

        self._toggles[index] = toggle
        self._labels[index] = label
        self._initial_states[index] = enabled

        # Wrap row in a widget for the bottom border
        container = QWidget()
        container.setLayout(row)
        container.setStyleSheet(
            f"border-bottom: 1px solid {_row_border()}; background: transparent;"
        )
        self._body_layout.addWidget(container)

    def _add_editable_row(
        self, index: int, label: str, description: str,
        editable_meta: dict, current_value: int | float | None,
    ) -> None:
        """Add a row with a numeric input for inline value editing."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 8)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = CaptionLabel(label)
        name_lbl.setWordWrap(True)
        nf = name_lbl.font()
        nf.setPixelSize(13)
        from PySide6.QtGui import QFont
        nf.setWeight(QFont.Weight.DemiBold)
        name_lbl.setFont(nf)
        text_col.addWidget(name_lbl)
        if description:
            desc_lbl = CaptionLabel(description)
            desc_lbl.setWordWrap(True)
            df = desc_lbl.font()
            df.setPixelSize(11)
            desc_lbl.setFont(df)
            text_col.addWidget(desc_lbl)
        # Show value range
        val_min = editable_meta.get("min", 0)
        val_max = editable_meta.get("max", 999999)
        # Validate min <= max
        if val_min > val_max:
            val_min, val_max = val_max, val_min
        range_lbl = CaptionLabel(
            tr("config_panel.value_range", min=val_min, max=val_max))
        rf = range_lbl.font()
        rf.setPixelSize(10)
        range_lbl.setFont(rf)
        range_lbl.setStyleSheet(f"color: {_section_color()}; opacity: 0.7;")
        text_col.addWidget(range_lbl)
        row.addLayout(text_col, 1)

        # Value input
        val_type = editable_meta.get("type", "int32_le")
        default_val = editable_meta.get("default", val_min)
        if current_value is not None:
            default_val = current_value

        if val_type == "float32_le":
            spinbox = QDoubleSpinBox()
            spinbox.setDecimals(3)
            spinbox.setMinimum(float(val_min))
            spinbox.setMaximum(float(val_max))
            spinbox.setValue(float(default_val))
        else:
            spinbox = QSpinBox()
            spinbox.setMinimum(int(val_min))
            spinbox.setMaximum(int(val_max))
            spinbox.setValue(int(default_val))

        spinbox.setFixedWidth(90)
        spinbox.valueChanged.connect(self._on_value_changed)
        row.addWidget(spinbox, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._value_inputs[index] = spinbox
        self._labels[index] = label
        self._initial_values[index] = default_val

        container = QWidget()
        container.setLayout(row)
        container.setStyleSheet(
            f"border-bottom: 1px solid {_row_border()}; background: transparent;"
        )
        self._body_layout.addWidget(container)

    def _on_value_changed(self) -> None:
        """Show Apply when any value input differs from initial."""
        self._check_any_changed()

    def _on_toggle_changed(self) -> None:
        """Show the Apply button when any toggle differs from its initial state."""
        self._check_any_changed()

    def _check_any_changed(self) -> None:
        """Show Apply when any toggle or value input differs from initial."""
        changed = any(
            cb.isChecked() != self._initial_states[idx]
            for idx, cb in self._toggles.items()
        )
        if not changed:
            changed = any(
                sb.value() != self._initial_values[idx]
                for idx, sb in self._value_inputs.items()
            )
        self._apply_btn.setVisible(changed)

    def _on_apply(self) -> None:
        # Variant-mode apply — emit dedicated signal with variant metadata.
        if getattr(self, "_variant_mode", False):
            out: list[dict] = []
            for idx, v in enumerate(self._variants_meta):
                widget = self._variant_widgets.get(idx)
                if widget is None:
                    enabled = bool(v.get("enabled"))
                else:
                    enabled = widget.isChecked()
                row = {
                    "label": v.get("label", ""),
                    "filename": v.get("filename", ""),
                    "enabled": enabled,
                    "group": v.get("group", -1),
                }
                # Pass through grid-variant metadata (Character Creator
                # style) so mods_page can tell a grid apply from a JSON
                # multi-variant apply.
                if "_level" in v:
                    row["_level"] = v["_level"]
                if "_header" in v:
                    row["_header"] = v["_header"]
                out.append(row)
            self.variants_apply_clicked.emit(self._mod_id, out)
            return

        # Emit each entry tagged with its ORIGINAL patch index so
        # mods_page can key custom_values by patch position rather
        # than by emitted-list position. Without `index`, mixing
        # toggles + editables in one mod silently misaligned the
        # custom_values dict (an editable's value got stored under
        # an unrelated toggle's slot, then apply-time looked up the
        # real index and found nothing).
        result = []
        for idx, cb in sorted(self._toggles.items()):
            result.append({
                "index": idx,
                "label": self._labels[idx],
                "enabled": cb.isChecked(),
            })
        for idx, sb in sorted(self._value_inputs.items()):
            result.append({
                "index": idx,
                "label": self._labels[idx],
                "enabled": True,
                "value": sb.value(),
            })
        self.apply_clicked.emit(self._mod_id, result)

    # ------------------------------------------------------------------
    # Variant-mode entry point
    # ------------------------------------------------------------------

    def show_variant_mod(
        self,
        mod_id: int,
        name: str,
        author: str,
        version: str,
        status: str,
        variants: list[dict],
        conflicts: list[str] | None = None,
    ) -> None:
        """Open the panel for a multi-variant JSON mod.

        ``variants`` is the list stored in ``mods.variants``:
        ``[{"label": str, "filename": str, "enabled": bool, "group": int}, ...]``.
        Variants that share a positive ``group`` are rendered as a radio
        group (only one may be enabled at a time); ``group == -1`` gets
        an independent checkbox.
        """
        self._mod_id = mod_id
        self._initial_states.clear()
        self._toggles.clear()
        self._labels.clear()
        self._value_inputs.clear()
        self._initial_values.clear()
        self._apply_btn.setVisible(False)
        self._variant_mode = True
        self._variants_meta = [dict(v) for v in variants]
        self._variant_widgets: dict[int, QCheckBox | QRadioButton] = {}
        self._variant_initial: dict[int, bool] = {
            i: bool(v.get("enabled")) for i, v in enumerate(self._variants_meta)
        }
        # Reset collapsible-section strong refs on every panel open.
        # Without this, switching from a mutex-pack mod to a plain
        # variant mod would leave dangling references to deleteLater'd
        # widgets. Any later iteration (e.g. a theme reapply pass)
        # would hit RuntimeError on a deleted C++ object. C-M2.
        self._collapsible_sections: list[_CollapsibleSection] = []
        # Per-variant label selections (populated by the "Configure..."
        # button and read by the page-level Apply handler). Keyed by
        # the variant's filename.
        self._variant_label_prev: dict[str, list[str]] = {}
        self._variant_label_dirty: set[str] = set()
        # Seed with any previously-persisted selections from mod_config
        # so the dialog pre-checks the user's last picks.
        try:
            import json as _j
            # Access the DB through the page parent if we can find it.
            db = None
            for cand in (getattr(self, "_db", None),
                         getattr(self.parent(), "_db", None) if self.parent() else None):
                if cand is not None:
                    db = cand
                    break
            if db is not None:
                row = db.connection.execute(
                    "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
                    (mod_id,)).fetchone()
                if row and row[0]:
                    sel = _j.loads(row[0])
                    # Accept the per-variant dict shape or a flat list
                    # (legacy single-JSON mods). Only the per-variant
                    # shape matters for variant mods.
                    if isinstance(sel, dict):
                        self._variant_label_prev = {
                            str(k): list(v) for k, v in sel.items()
                            if isinstance(v, list)
                        }
        except Exception as _e:
            logger.debug("Could not seed variant label prev: %s", _e)

        self._title_label.setText(name)
        self._author_label.setText(
            tr("config_panel.by_author", author=author) if author else "")

        self._clear_badges()
        self._badge_row.insertWidget(0, _make_badge(status))
        if version:
            self._badge_row.insertWidget(
                1, _make_badge(f"v{version}", "#444C5C"))
        n_enabled = sum(1 for v in self._variants_meta if v.get("enabled"))
        # Detect mutex-variant-pack mode (collapsibles will render) so
        # the badge doesn't read "1/144 variants" — which feels alarming,
        # like 143 mods are broken. Show the active loadout name instead,
        # matching the user's mental model ("I've picked ONE loadout").
        _labels = [str(v.get("label", "")) for v in self._variants_meta]
        _mutex_pack = (
            len(self._variants_meta) >= 4
            and all(" / " in lbl for lbl in _labels)
            and _group_variants_by_category_prefix(self._variants_meta)
                is not None
        )
        if _mutex_pack:
            _active = next(
                (v for v in self._variants_meta if v.get("enabled")), None)
            if _active:
                _short = _strip_category_prefix(_active.get("label", ""))
                _badge_text = tr("config_panel.active_loadout", name=_short)
                if _badge_text == "config_panel.active_loadout":
                    _badge_text = f"Active: {_short}"
            else:
                _badge_text = tr("config_panel.n_loadouts",
                                 count=len(self._variants_meta))
                if _badge_text == "config_panel.n_loadouts":
                    _badge_text = f"{len(self._variants_meta)} loadouts"
        else:
            _badge_text = tr("config_panel.n_of_total_variants",
                             enabled=n_enabled,
                             total=len(self._variants_meta))
            if _badge_text == "config_panel.n_of_total_variants":
                _badge_text = (
                    f"{n_enabled}/{len(self._variants_meta)} variants")
        self._badge_row.insertWidget(
            2, _make_badge(_badge_text, "#444C5C"),
        )

        self._clear_body()
        # Translation key may not exist in older locale files — fall back to
        # the English literal if tr() returns the key unchanged.
        # Skip the generic VARIANTS header when every group has its own
        # _header (Character-Creator-style gender/race per-axis headers).
        _every_group_has_header = bool(variants) and all(
            v.get("_header") for v in variants if v.get("group", -1) >= 0)
        if not _every_group_has_header:
            variants_header = tr("config_panel.section_variants")
            if variants_header == "config_panel.section_variants":
                variants_header = "VARIANTS"
            self._add_section_header(variants_header)

        # Render radio groups (positive group ids, size ≥ 2) first, then
        # independent checkboxes (group = -1). Each row uses the same
        # label-column + indicator layout as ``_add_config_row`` so long
        # labels wrap and text reads correctly on both light + dark themes.
        groups: dict[int, list[int]] = {}
        independents: list[int] = []
        for i, v in enumerate(self._variants_meta):
            g = v.get("group", -1)
            if g >= 0:
                groups.setdefault(g, []).append(i)
            else:
                independents.append(i)

        # Archive-wide mutex packs (GildsGear-style: 40+ variants in one
        # radio group with 'Category / Variant' labels) render as
        # collapsible category sections so the user isn't scrolling
        # through 40 radios. Triggers when we have exactly ONE radio
        # group, no independents, and every label parses into 2+
        # categories.
        cat_groups = None
        if len(groups) == 1 and not independents:
            only_members = next(iter(groups.values()))
            member_variants = [self._variants_meta[i] for i in only_members]
            parsed = _group_variants_by_category_prefix(member_variants)
            if parsed is not None:
                # Map local member indices back to full variants_meta
                # indices so downstream handlers still work.
                cat_groups = {
                    cat: [only_members[li] for li in idxs]
                    for cat, idxs in parsed.items()
                }

        if cat_groups:
            # Which category contains the currently-enabled variant?
            # That one starts expanded; the rest collapsed.
            active_cat = next(iter(cat_groups))
            for cat, idxs in cat_groups.items():
                if any(self._variants_meta[i].get("enabled") for i in idxs):
                    active_cat = cat
                    break
            button_group = QButtonGroup(self)
            button_group.setExclusive(True)
            # PySide6 signal connections hold a WEAK reference to
            # bound-method slots. If the section Python object goes out
            # of scope after this loop, GC collects it and the
            # header-button click becomes a silent no-op on Windows
            # (confirmed via Qt forum thread 154590). Keep a strong
            # reference on the panel so every section stays alive.
            self._collapsible_sections: list[_CollapsibleSection] = []
            for cat, idxs in cat_groups.items():
                section = _CollapsibleSection(
                    cat, len(idxs), start_expanded=(cat == active_cat))
                self._collapsible_sections.append(section)
                self._body_layout.addWidget(section.header)
                self._body_layout.addWidget(section.body)
                for idx in idxs:
                    v = dict(self._variants_meta[idx])
                    # Show just the right-hand side ('AbyssGear_1')
                    # since the section header already says
                    # 'Abyss Gears'.
                    v["label"] = _strip_category_prefix(v.get("label", ""))
                    rb = QRadioButton()
                    rb.setChecked(bool(self._variants_meta[idx].get("enabled")))
                    rb.toggled.connect(self._on_variant_changed)
                    button_group.addButton(rb, idx)
                    self._variant_widgets[idx] = rb
                    section.add_row(self._build_variant_row(rb, v))
            # Skip the flat-group render below.
            if conflicts:
                self._add_section_header(tr("config_panel.section_conflicts"))
                for desc in conflicts:
                    lbl = CaptionLabel(desc)
                    lbl.setWordWrap(True)
                    lbl.setStyleSheet("color: #D04848; padding: 4px 0;")
                    self._body_layout.addWidget(lbl)
            self._body_layout.addStretch()
            self._apply_theme()

            self.setVisible(True)
            self._anim.stop()
            self._disconnect_closed_handler()
            self._anim.setStartValue(self.maximumWidth())
            self._anim.setEndValue(self._PANEL_WIDTH)
            self._opacity_effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(self._opacity_effect)
            self._opacity_effect.setOpacity(0.0)
            self._fade_anim = QPropertyAnimation(
                self._opacity_effect, b"opacity")
            self._fade_anim.setDuration(self._ANIM_DURATION)
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._fade_anim.finished.connect(
                lambda: self.setGraphicsEffect(None))
            self._anim.start()
            self._fade_anim.start()
            return

        for g_id, members in sorted(groups.items()):
            # Per-group header (axis name like "Gender" / "Race") when
            # the variants were emitted with `_header` metadata.
            if members:
                first = self._variants_meta[members[0]]
                hdr = first.get("_header")
                if hdr:
                    self._add_section_header(hdr.upper())
            button_group = QButtonGroup(self)
            button_group.setExclusive(True)
            for idx in members:
                v = self._variants_meta[idx]
                rb = QRadioButton()
                rb.setChecked(bool(v.get("enabled")))
                rb.toggled.connect(self._on_variant_changed)
                button_group.addButton(rb, idx)
                self._variant_widgets[idx] = rb
                self._body_layout.addWidget(
                    self._build_variant_row(rb, v))

        for idx in independents:
            v = self._variants_meta[idx]
            cb = QCheckBox()
            cb.setChecked(bool(v.get("enabled")))
            cb.toggled.connect(self._on_variant_changed)
            self._variant_widgets[idx] = cb
            self._body_layout.addWidget(self._build_variant_row(cb, v))

        if conflicts:
            self._add_section_header(tr("config_panel.section_conflicts"))
            for desc in conflicts:
                lbl = CaptionLabel(desc)
                lbl.setWordWrap(True)
                lbl.setStyleSheet("color: #D04848; padding: 4px 0;")
                self._body_layout.addWidget(lbl)

        self._body_layout.addStretch()
        self._apply_theme()

        self.setVisible(True)
        self._anim.stop()
        self._disconnect_closed_handler()
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(self._PANEL_WIDTH)
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(0.0)
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_anim.setDuration(self._ANIM_DURATION)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(lambda: self.setGraphicsEffect(None))
        self._anim.start()
        self._fade_anim.start()

    def _build_variant_row(self, indicator, variant: dict) -> QWidget:
        """Build a label-left / indicator-right row for a variant.

        Matches the visual language of ``_add_config_row``. Label colors
        come from the parent ``ConfigPanel`` stylesheet (via ``_apply_theme``)
        rather than inline overrides — mirroring how the per-change toggle
        rows work, so dark-theme and light-theme both render correctly
        without us second-guessing ``isDarkTheme()`` at row-build time.
        """
        from PySide6.QtGui import QFont

        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        row.setSpacing(10)

        # Indicator FIRST so it stays visible when the panel slides
        # over a narrow main window (Nexus #26).
        # Indicator styling and addition handled below — we add it
        # to the row before the label column.

        # Label column (wraps)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        # Title — plain QLabel inherits ConfigPanel's QLabel color.
        # GitHub #112 Inosakiii: long variant labels (e.g. mutex preset
        # mods that ship "<source_filename> -> <dest_filename>" as the
        # label) get truncated at the default panel width even with
        # wordWrap on. Two fixes here:
        #   1. setSizePolicy(Preferred, Minimum + heightForWidth) so
        #      the label grows tall enough to fit the wrapped text
        #      instead of clipping rows.
        #   2. setToolTip(full label) so even if the label is still
        #      cut off (very narrow panel, very long string) the
        #      user can hover to read the full text.
        from PySide6.QtWidgets import QSizePolicy
        title_lbl = QLabel(variant.get("label", ""))
        title_lbl.setWordWrap(True)
        title_lbl.setMinimumWidth(1)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred,
                         QSizePolicy.Policy.MinimumExpanding)
        sp.setHeightForWidth(True)
        title_lbl.setSizePolicy(sp)
        title_lbl.setToolTip(variant.get("label", ""))
        tf = title_lbl.font()
        tf.setPixelSize(14)
        tf.setWeight(QFont.Weight.DemiBold)
        title_lbl.setFont(tf)
        text_col.addWidget(title_lbl)

        # Meta — CaptionLabel has qfluentwidgets' built-in subtle caption
        # color that follows the active theme automatically.
        meta_bits: list[str] = []
        if variant.get("version"):
            meta_bits.append(f"v{variant['version']}")
        if variant.get("author"):
            meta_bits.append(
                tr("config_panel.by_author", author=variant["author"]))
        if meta_bits:
            meta_lbl = CaptionLabel(" · ".join(meta_bits))
            meta_lbl.setWordWrap(True)
            mf = meta_lbl.font()
            mf.setPixelSize(11)
            meta_lbl.setFont(mf)
            text_col.addWidget(meta_lbl)

        # Indicator-only widget (no built-in text — the label column handles it).
        # Suppress Qt's default focus frame / background so the widget shows
        # JUST the round radio dot / square checkbox, no rectangular outline.
        accent = "#2878D0"
        # Medium gray reads as a subtle-but-visible outline on both themes.
        unchecked_border = "#7B8595"
        indicator.setStyleSheet(
            "QCheckBox, QRadioButton { "
            "  border: none; background: transparent; spacing: 0; "
            "  padding: 0; margin: 0; outline: none; "
            "}"
            "QCheckBox:focus, QRadioButton:focus { outline: none; border: none; }"
            "QCheckBox::indicator, QRadioButton::indicator { "
            "  width: 18px; height: 18px; "
            "}"
            "QCheckBox::indicator { border-radius: 4px; }"
            f"QCheckBox::indicator:unchecked {{ "
            f"  background: transparent; border: 2px solid {unchecked_border}; "
            f"}}"
            f"QCheckBox::indicator:checked {{ "
            f"  background: {accent}; border: 2px solid {accent}; "
            f"}}"
            "QRadioButton::indicator { border-radius: 10px; }"
            f"QRadioButton::indicator:unchecked {{ "
            f"  background: transparent; border: 2px solid {unchecked_border}; "
            f"}}"
            f"QRadioButton::indicator:checked {{ "
            f"  background: {accent}; border: 2px solid {accent}; "
            f"}}"
        )
        # Indicator goes at index 0 so it renders to the LEFT of the
        # label column (added after this with stretch=1).
        row.insertWidget(0, indicator, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(text_col, 1)

        # When the variant ships internal labeled changes (e.g. Unlimited
        # Dragon Flying's mutex Ride Duration presets), append a
        # second-row Configure button BELOW the title. Inline placement
        # competes with the variant title for horizontal space and
        # truncates text in the narrow side panel.
        cfg_btn_widget = None
        if variant.get("_has_labels") and variant.get("_json_path"):
            from qfluentwidgets import PushButton
            cfg_btn = PushButton(tr("config_panel.configure_options"))
            cfg_btn.setFixedHeight(28)
            jp_path = variant["_json_path"]
            v_fn = variant.get("filename", "")
            cfg_btn.clicked.connect(
                lambda _checked=False, p=jp_path, fn=v_fn:
                    self._open_variant_label_picker(p, fn))
            cfg_btn_widget = cfg_btn

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        # Top: title + indicator row
        top_row_widget = QWidget()
        top_row_widget.setLayout(row)
        outer.addWidget(top_row_widget)
        # Bottom: full-width Configure button if relevant
        if cfg_btn_widget is not None:
            outer.addWidget(cfg_btn_widget)

        container = QWidget()
        container.setLayout(outer)
        container.setStyleSheet(
            f"border-bottom: 1px solid {_row_border()}; background: transparent;")
        return container

    def _open_variant_label_picker(self, json_path: str, variant_filename: str) -> None:
        """Pop the TogglePickerDialog for a single variant's JSON.

        The dialog already has mutex-offset detection (multiple changes
        at the same byte offset become a radio group). User's picks get
        stashed on the panel so the page-level Apply handler can persist
        them to mod_config.selected_labels and regenerate merged.json
        through synthesize_merged_json's label_selections param.
        """
        import json as _json
        from pathlib import Path as _Path
        from cdumm.gui.preset_picker import TogglePickerDialog
        try:
            data = _json.loads(_Path(json_path).read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read variant JSON %s: %s",
                           json_path, e)
            return
        previous = self._variant_label_prev.get(variant_filename) or []
        # `previous` is now a list of [patch_idx, change_idx] pairs
        # (post Codex-P1 fix). TogglePickerDialog expects plain label
        # strings for pre-check display, so convert. Legacy DB rows
        # may still be plain strings — pass them through unchanged.
        prev_labels: list[str] = []
        if previous and isinstance(previous[0], str):
            prev_labels = list(previous)
        else:
            for pair in previous:
                try:
                    pi, ci = int(pair[0]), int(pair[1])
                    c = data["patches"][pi]["changes"][ci]
                    if "label" in c:
                        prev_labels.append(c["label"])
                except (IndexError, KeyError, TypeError):
                    continue
        # Parent the modal dialog to the TOP-LEVEL window, not to the
        # narrow side panel. Otherwise the dialog inherits the panel's
        # cramped width and can't render readable mutex / checkbox
        # rows (the user reported the dialog looking truncated).
        top_parent = self.window() or self
        dlg = TogglePickerDialog(data, parent=top_parent,
                                  previous_labels=prev_labels)
        if dlg.exec() and dlg.selected_data is not None:
            # Extract stable (patch_idx, change_idx) keys for each
            # picked change. Label-text matching (old approach) broke
            # on variants that reused a label — picking one silently
            # picked every sibling sharing the text. Codex P1 fix.
            # We look up the INDEX of each picked change inside the
            # ORIGINAL variant JSON so downstream
            # synthesize_merged_json can reproduce the exact pick.
            picked_keys: list[list[int]] = []
            # Build a lookup: (game_file, offset, patched) -> (p_idx, c_idx)
            # from the ORIGINAL data, then stamp those indices onto
            # each picked change. The picker returns a deep-copied
            # subset of the data, so identity (id()) doesn't apply —
            # but (game_file, offset, patched) is unique enough to
            # locate each picked change in the source.
            index_lookup: dict[tuple, list[int]] = {}
            for p_idx, p in enumerate(data.get("patches", [])):
                gf = p.get("game_file", "")
                for c_idx, c in enumerate(p.get("changes", [])):
                    if "label" not in c:
                        continue
                    key = (gf, c.get("offset"), c.get("patched"),
                           c.get("label"))
                    index_lookup.setdefault(key, [p_idx, c_idx])
            for p in dlg.selected_data.get("patches", []):
                gf = p.get("game_file", "")
                for c in p.get("changes", []):
                    if "label" not in c:
                        continue
                    key = (gf, c.get("offset"), c.get("patched"),
                           c.get("label"))
                    pair = index_lookup.get(key)
                    if pair is not None:
                        picked_keys.append(list(pair))
            self._variant_label_prev[variant_filename] = picked_keys
            self._variant_label_dirty.add(variant_filename)
            self._apply_btn.setVisible(True)

    def _on_variant_changed(self, *_a) -> None:
        # Apply stays visible if EITHER the variant pick differs from
        # initial OR any variant's labels were edited (tracked by
        # _variant_label_dirty). Previously only the variant-change
        # branch was checked, so reverting a variant after editing
        # labels hid Apply and dropped the label edits.
        self._apply_btn.setVisible(_is_apply_visible(
            self._variant_widgets,
            self._variant_initial,
            getattr(self, "_variant_label_dirty", set()),
        ))
