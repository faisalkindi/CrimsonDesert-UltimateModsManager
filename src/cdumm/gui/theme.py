"""CDUMM Application Theme — Dark and Light modes with Amber Accents."""

DARK_PALETTE = {
    # Surface hierarchy — 4% lightness increments (GitHub-style blue-tint)
    "bg_deep": "#0D1117",        # deep base (GitHub proven, not pure black)
    "bg_dark": "#121820",        # main content bg
    "bg_mid": "#161B22",         # sidebar, headers (GitHub raised)
    "bg_elevated": "#1C2128",    # cards, elevated surfaces (GitHub modal)
    "bg_hover": "#262C36",       # hover states
    "border": "#30363D",         # borders (GitHub border)
    "border_dim": "#1E2430",     # subtle borders
    # Text — warm off-white, not pure white (reduces eye strain)
    "text_bright": "#F0F3F6",    # headings — ~15:1 contrast on base
    "text_primary": "#D0D7E0",   # body text — ~11:1 contrast
    "text_secondary": "#8B95A5", # labels — ~6:1 contrast
    "text_muted": "#4E5564",     # disabled — ~2.5:1
    # Amber accent — slightly desaturated to prevent blooming on dark
    "accent": "#D4A43C",
    "accent_hover": "#E4B44C",
    "accent_dim": "#9A7428",
    "green": "#48A858",
    "green_hover": "#58C068",
    "green_dim": "#388A48",
    "red": "#D04848",
    "red_hover": "#E05858",
    "selection": "#1A2840",
    "alt_row": "#141A22",        # subtle alternate row
    "item_hover": "#1A2230",     # hover with blue tint
    "scrollbar": "#343C4A",
    "scrollbar_hover": "#48526A",
    "btn_pressed": "#2A3248",
    "border_hover": "#3E4858",
    "revert_bg": "#201414",
    "groupbox_bg": "#10151C",
}

LIGHT_PALETTE = {
    # Warm neutrals — reduces eye fatigue vs pure white (2026 best practice)
    "bg_deep": "#FAFAF8",        # warm off-white canvas
    "bg_dark": "#F5F2EE",        # warm light background
    "bg_mid": "#EBE7E2",         # sidebar, headers
    "bg_elevated": "#FFFFFF",    # cards, elevated surfaces
    "bg_hover": "#E0DCD6",       # hover states
    "border": "#D6D3D1",         # warm borders
    "border_dim": "#E8E5E1",     # subtle borders
    # Near-black text — 17.5:1 contrast on warm white (AAA)
    "text_bright": "#1C1917",    # headings, important text
    "text_primary": "#2E2A26",   # body text
    "text_secondary": "#57534E", # labels — 6.8:1 contrast
    "text_muted": "#A8A29E",     # disabled
    # Amber accent adjusted for light backgrounds
    "accent": "#A3781C",         # darker amber for contrast
    "accent_hover": "#B8862C",
    "accent_dim": "#8A6420",
    "green": "#166534",          # dark green for contrast
    "green_hover": "#15803D",
    "green_dim": "#14532D",
    "red": "#B91C1C",            # dark red for contrast
    "red_hover": "#DC2626",
    "selection": "#DDD6C8",      # warm selection
    "alt_row": "#F0EDE8",        # warm alternate row
    "item_hover": "#E8E4DE",     # warm hover
    "scrollbar": "#C8C3BC",      # warm scrollbar
    "scrollbar_hover": "#A8A29E",
    "btn_pressed": "#D6D0C8",    # warm pressed
    "border_hover": "#B8B2AA",   # warm border hover
    "revert_bg": "#FFF5F5",      # light red bg
    "groupbox_bg": "#F5F2EE",    # warm groupbox
}

# Current active palette — module-level so other files can import colors
_current_palette = dict(DARK_PALETTE)


def get_color(key: str) -> str:
    """Get the current theme color by key."""
    return _current_palette.get(key, "#FF00FF")


def build_stylesheet(p: dict) -> str:
    """Build the full QSS stylesheet from a palette dict."""
    return f"""
/* ── Base ── */
QMainWindow {{
    background-color: {p["bg_dark"]};
}}
QWidget {{
    color: {p["text_primary"]};
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}}

/* ── Sidebar ── */
QFrame#sidebar {{
    background-color: {p["bg_mid"]};
    border-right: 2px solid {p["border"]};
}}
QFrame#sidebar QLabel#sidebarTitle {{
    color: {p["accent"]};
    font-size: 16px;
    font-weight: 800;
    padding: 4px;
    letter-spacing: 1px;
}}
QFrame#sidebar QPushButton {{
    background: transparent;
    border: none;
    border-radius: 8px;
    color: {p["text_secondary"]};
    padding: 10px 4px;
    font-size: 12px;
    font-weight: 600;
    min-width: 72px;
    max-width: 72px;
    min-height: 40px;
}}
QFrame#sidebar QPushButton:hover {{
    background: {p["bg_elevated"]};
    color: {p["text_bright"]};
}}
QFrame#sidebar QPushButton:checked {{
    background: {p["bg_elevated"]};
    color: {p["accent"]};
    border-left: 3px solid {p["accent"]};
    border-radius: 0px 8px 8px 0px;
}}

/* ── Action Bar ── */
QFrame#actionBar {{
    background: {p["bg_mid"]};
    border-top: 2px solid {p["border"]};
}}
QPushButton#applyBtn {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {p["accent"]}, stop:1 {p["accent_dim"]});
    border: none;
    border-radius: 8px;
    color: {p["bg_deep"]};
    font-weight: 700;
    font-size: 14px;
    padding: 10px 32px;
}}
QPushButton#applyBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {p["accent_hover"]}, stop:1 {p["accent"]});
}}
QPushButton#launchBtn {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {p["green"]}, stop:1 {p["green_dim"]});
    border: none;
    border-radius: 8px;
    color: white;
    font-weight: 700;
    font-size: 14px;
    padding: 10px 32px;
}}
QPushButton#launchBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {p["green_hover"]}, stop:1 {p["green"]});
}}
QPushButton#revertBtn {{
    background: transparent;
    border: 1px solid {p["red"]};
    border-radius: 8px;
    color: {p["red"]};
    padding: 8px 18px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#revertBtn:hover {{
    background: {p["revert_bg"]};
    color: {p["red_hover"]};
    border-color: {p["red_hover"]};
}}

/* ── Table ── */
QTableView, QTableWidget {{
    background-color: {p["bg_dark"]};
    alternate-background-color: {p["alt_row"]};
    border: 1px solid {p["border"]};
    border-radius: 8px;
    gridline-color: {p["border_dim"]};
    selection-background-color: {p["selection"]};
    selection-color: {p["text_bright"]};
    outline: none;
}}
QTableView::item, QTableWidget::item {{
    padding: 8px 10px;
    border-bottom: 1px solid {p["border_dim"]};
}}
QTableView::item:hover, QTableWidget::item:hover {{
    background: {p["item_hover"]};
}}
QTableView::item:selected, QTableWidget::item:selected {{
    background: {p["selection"]};
    color: {p["text_bright"]};
}}
QHeaderView {{
    background: transparent;
}}
QHeaderView::section {{
    background: {p["bg_mid"]};
    color: {p["text_secondary"]};
    border: none;
    border-bottom: 2px solid {p["border"]};
    border-right: 1px solid {p["border_dim"]};
    padding: 9px 10px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
}}
QHeaderView::section:first {{
    border-top-left-radius: 8px;
}}
QHeaderView::section:last {{
    border-top-right-radius: 8px;
}}
QHeaderView::section:hover {{
    color: {p["text_bright"]};
    background: {p["bg_elevated"]};
}}

/* ── Buttons (general) ── */
QPushButton {{
    background: {p["bg_elevated"]};
    border: 1px solid {p["border"]};
    border-radius: 8px;
    color: {p["text_primary"]};
    padding: 8px 18px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {p["bg_hover"]};
    border-color: {p["border_hover"]};
    color: {p["text_bright"]};
}}
QPushButton:pressed {{
    background: {p["btn_pressed"]};
}}
QPushButton:disabled {{
    background: {p["bg_dark"]};
    color: {p["text_muted"]};
    border-color: {p["border_dim"]};
}}

/* ── Splitter ── */
QSplitter::handle {{
    background: {p["border"]};
    height: 3px;
}}
QSplitter::handle:hover {{
    background: {p["accent"]};
}}

/* ── ScrollBar ── */
QScrollBar:vertical {{
    background: {p["bg_dark"]};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {p["scrollbar"]};
    border-radius: 4px;
    min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p["scrollbar_hover"]};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {p["bg_dark"]};
    height: 8px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {p["scrollbar"]};
    border-radius: 4px;
    min-width: 40px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p["scrollbar_hover"]};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── Status Bar ── */
QStatusBar {{
    background: {p["bg_deep"]};
    border-top: 1px solid {p["border_dim"]};
    color: {p["text_secondary"]};
    font-size: 11px;
}}
QStatusBar QLabel {{
    color: {p["text_secondary"]};
    font-size: 11px;
    padding: 0 6px;
}}

/* ── Dialog / MessageBox ── */
QDialog {{
    background: {p["bg_dark"]};
}}
QMessageBox {{
    background: {p["bg_mid"]};
}}
QMessageBox QLabel {{
    color: {p["text_bright"]};
    font-size: 13px;
}}
QMessageBox QPushButton {{
    min-width: 80px;
}}

/* ── Input ── */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background: {p["bg_deep"]};
    border: 1px solid {p["border"]};
    border-radius: 8px;
    color: {p["text_bright"]};
    padding: 7px 10px;
    selection-background-color: {p["selection"]};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {p["accent"]};
}}

/* ── Menu ── */
QMenu {{
    background: {p["bg_mid"]};
    border: 1px solid {p["border"]};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{
    padding: 8px 28px 8px 16px;
    border-radius: 6px;
    color: {p["text_primary"]};
}}
QMenu::item:selected {{
    background: {p["selection"]};
    color: {p["text_bright"]};
}}
QMenu::separator {{
    height: 1px;
    background: {p["border"]};
    margin: 4px 8px;
}}

/* ── ToolTip ── */
QToolTip {{
    background: {p["bg_elevated"]};
    border: 1px solid {p["border"]};
    border-radius: 4px;
    color: {p["text_bright"]};
    padding: 6px 10px;
    font-size: 12px;
}}

/* ── Progress ── */
QProgressBar {{
    background: {p["bg_deep"]};
    border: none;
    border-radius: 4px;
    height: 6px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {p["accent"]}, stop:1 {p["accent_hover"]});
    border-radius: 4px;
}}

/* ── List / Tree ── */
QListWidget, QTreeWidget, QTreeView {{
    background: {p["bg_dark"]};
    border: 1px solid {p["border"]};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item, QTreeWidget::item, QTreeView::item {{
    padding: 7px 12px;
    border-bottom: 1px solid {p["border_dim"]};
    color: {p["text_primary"]};
}}
QListWidget::item:hover, QTreeWidget::item:hover, QTreeView::item:hover {{
    background: {p["item_hover"]};
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTreeView::item:selected {{
    background: {p["selection"]};
    color: {p["text_bright"]};
}}

/* ── GroupBox ── */
QGroupBox {{
    background: {p["groupbox_bg"]};
    border: 1px solid {p["border"]};
    border-radius: 8px;
    margin-top: 16px;
    padding-top: 20px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 16px;
    padding: 0 8px;
    color: {p["text_secondary"]};
    font-weight: 600;
}}

/* ── ComboBox ── */
QComboBox {{
    background: {p["bg_elevated"]};
    border: 1px solid {p["border"]};
    border-radius: 8px;
    color: {p["text_bright"]};
    padding: 6px 10px;
    min-height: 20px;
}}
QComboBox QAbstractItemView {{
    background: {p["bg_mid"]};
    border: 1px solid {p["border"]};
    selection-background-color: {p["selection"]};
}}

/* ── CheckBox ── */
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 2px solid {p["border_hover"]};
    border-radius: 5px;
    background: {p["bg_deep"]};
}}
QCheckBox::indicator:checked {{
    background: {p["accent"]};
    border-color: {p["accent"]};
}}
QCheckBox::indicator:hover {{
    border-color: {p["accent"]};
}}

/* ── Tools page label ── */
QLabel#toolsHeader {{
    color: {p["text_bright"]};
    font-size: 18px;
    font-weight: 700;
    padding: 4px 0px 12px 0px;
    min-height: 28px;
}}

/* ── Activity panel ── */
QLabel#activityHeader {{
    font-size: 15px;
    font-weight: bold;
    color: {p["text_bright"]};
}}
QTextBrowser#activityLog {{
    background: {p["bg_deep"]};
    border: 1px solid {p["border"]};
    border-radius: 6px;
    padding: 8px;
    font-family: 'Consolas', 'Cascadia Mono', monospace;
    font-size: 12px;
    color: {p["text_primary"]};
}}
QLabel#filterDot {{
    font-size: 11px;
    color: {p["text_secondary"]};
}}

/* ── Drop zone ── */
QLabel#dropLabel {{
    border: 3px dashed {p["border"]};
    border-radius: 10px;
    padding: 28px;
    color: {p["text_secondary"]};
    background: {p["bg_deep"]};
    font-size: 16px;
    font-weight: 700;
}}
"""


def set_theme(theme_name: str) -> None:
    """Set the active palette. Call before build_stylesheet."""
    global _current_palette
    if theme_name == "light":
        _current_palette = dict(LIGHT_PALETTE)
    else:
        _current_palette = dict(DARK_PALETTE)


def is_dark() -> bool:
    """Check if the current theme is dark."""
    return _current_palette.get("bg_deep") == DARK_PALETTE["bg_deep"]


# Backward compatibility — default dark stylesheet
# Old code imports: from cdumm.gui.theme import STYLESHEET
# Keep these module-level constants for backward compatibility
# Backward compatibility constants
BG_DEEP = DARK_PALETTE["bg_deep"]
BG_DARK = DARK_PALETTE["bg_dark"]
BG_MID = DARK_PALETTE["bg_mid"]
BG_ELEVATED = DARK_PALETTE["bg_elevated"]
BG_HOVER = DARK_PALETTE["bg_hover"]
BORDER = DARK_PALETTE["border"]
BORDER_DIM = DARK_PALETTE["border_dim"]
TEXT_BRIGHT = DARK_PALETTE["text_bright"]
TEXT_PRIMARY = DARK_PALETTE["text_primary"]
TEXT_SECONDARY = DARK_PALETTE["text_secondary"]
TEXT_MUTED = DARK_PALETTE["text_muted"]
ACCENT = DARK_PALETTE["accent"]
ACCENT_HOVER = DARK_PALETTE["accent_hover"]
ACCENT_DIM = DARK_PALETTE["accent_dim"]
GREEN = DARK_PALETTE["green"]
GREEN_HOVER = DARK_PALETTE["green_hover"]
RED = DARK_PALETTE["red"]
RED_HOVER = DARK_PALETTE["red_hover"]
SELECTION = DARK_PALETTE["selection"]

STYLESHEET = build_stylesheet(DARK_PALETTE)
