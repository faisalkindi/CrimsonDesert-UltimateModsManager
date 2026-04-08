"""CDUMM Application Theme — High Contrast Dark with Amber Accents."""

# Color palette — higher contrast
BG_DEEP = "#090B0E"       # deepest background
BG_DARK = "#0F1216"       # main content bg
BG_MID = "#171B22"        # sidebar, headers
BG_ELEVATED = "#1F242D"   # cards, elevated surfaces
BG_HOVER = "#262C38"      # hover states
BORDER = "#2E3440"        # strong borders
BORDER_DIM = "#1C2028"    # subtle borders
TEXT_BRIGHT = "#EAECF0"   # headings, important text
TEXT_PRIMARY = "#C0C6D2"  # body text
TEXT_SECONDARY = "#788090" # labels
TEXT_MUTED = "#4E5564"    # disabled
ACCENT = "#D4A43C"        # amber gold
ACCENT_HOVER = "#E4B44C"
ACCENT_DIM = "#9A7428"
GREEN = "#48A858"
GREEN_HOVER = "#58C068"
RED = "#D04848"
RED_HOVER = "#E05858"
SELECTION = "#1A2840"

STYLESHEET = f"""
/* ── Base ── */
QMainWindow {{
    background-color: {BG_DARK};
}}
QWidget {{
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}}

/* ── Sidebar ── */
QFrame#sidebar {{
    background-color: {BG_MID};
    border-right: 2px solid {BORDER};
}}
QFrame#sidebar QLabel#sidebarTitle {{
    color: {ACCENT};
    font-size: 16px;
    font-weight: 800;
    padding: 4px;
    letter-spacing: 1px;
}}
QFrame#sidebar QPushButton {{
    background: transparent;
    border: none;
    border-radius: 8px;
    color: {TEXT_SECONDARY};
    padding: 10px 4px;
    font-size: 12px;
    font-weight: 600;
    min-width: 72px;
    max-width: 72px;
    min-height: 40px;
}}
QFrame#sidebar QPushButton:hover {{
    background: {BG_ELEVATED};
    color: {TEXT_BRIGHT};
}}
QFrame#sidebar QPushButton:checked {{
    background: {BG_ELEVATED};
    color: {ACCENT};
    border-left: 3px solid {ACCENT};
    border-radius: 0px 8px 8px 0px;
}}

/* ── Action Bar ── */
QFrame#actionBar {{
    background: {BG_MID};
    border-top: 2px solid {BORDER};
}}
QPushButton#applyBtn {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {ACCENT}, stop:1 {ACCENT_DIM});
    border: none;
    border-radius: 8px;
    color: {BG_DEEP};
    font-weight: 700;
    font-size: 14px;
    padding: 10px 32px;
}}
QPushButton#applyBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {ACCENT_HOVER}, stop:1 {ACCENT});
}}
QPushButton#launchBtn {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {GREEN}, stop:1 #388A48);
    border: none;
    border-radius: 8px;
    color: white;
    font-weight: 700;
    font-size: 14px;
    padding: 10px 32px;
}}
QPushButton#launchBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {GREEN_HOVER}, stop:1 {GREEN});
}}
QPushButton#revertBtn {{
    background: transparent;
    border: 1px solid {RED};
    border-radius: 8px;
    color: {RED};
    padding: 8px 18px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#revertBtn:hover {{
    background: #201010;
    color: {RED_HOVER};
    border-color: {RED_HOVER};
}}

/* ── Table ── */
QTableView, QTableWidget {{
    background-color: {BG_DARK};
    alternate-background-color: #121620;
    border: 1px solid {BORDER};
    border-radius: 8px;
    gridline-color: {BORDER_DIM};
    selection-background-color: {SELECTION};
    selection-color: {TEXT_BRIGHT};
    outline: none;
}}
QTableView::item, QTableWidget::item {{
    padding: 8px 10px;
    border-bottom: 1px solid {BORDER_DIM};
}}
QTableView::item:hover, QTableWidget::item:hover {{
    background: #161C28;
}}
QTableView::item:selected, QTableWidget::item:selected {{
    background: {SELECTION};
    color: {TEXT_BRIGHT};
}}
QHeaderView {{
    background: transparent;
}}
QHeaderView::section {{
    background: {BG_MID};
    color: {TEXT_SECONDARY};
    border: none;
    border-bottom: 2px solid {BORDER};
    border-right: 1px solid {BORDER_DIM};
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
    color: {TEXT_BRIGHT};
    background: {BG_ELEVATED};
}}

/* ── Buttons (general) ── */
QPushButton {{
    background: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 8px;
    color: {TEXT_PRIMARY};
    padding: 8px 18px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {BG_HOVER};
    border-color: #3A4050;
    color: {TEXT_BRIGHT};
}}
QPushButton:pressed {{
    background: #2A3248;
}}
QPushButton:disabled {{
    background: {BG_DARK};
    color: {TEXT_MUTED};
    border-color: {BORDER_DIM};
}}

/* ── Splitter ── */
QSplitter::handle {{
    background: {BORDER};
    height: 3px;
}}
QSplitter::handle:hover {{
    background: {ACCENT};
}}

/* ── ScrollBar ── */
QScrollBar:vertical {{
    background: {BG_DARK};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: #333A48;
    border-radius: 4px;
    min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{
    background: #44506A;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {BG_DARK};
    height: 8px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: #333A48;
    border-radius: 4px;
    min-width: 40px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #44506A;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── Status Bar ── */
QStatusBar {{
    background: {BG_DEEP};
    border-top: 1px solid {BORDER_DIM};
    color: {TEXT_SECONDARY};
    font-size: 11px;
}}
QStatusBar QLabel {{
    color: {TEXT_SECONDARY};
    font-size: 11px;
    padding: 0 6px;
}}

/* ── Dialog / MessageBox ── */
QDialog {{
    background: {BG_DARK};
}}
QMessageBox {{
    background: {BG_MID};
}}
QMessageBox QLabel {{
    color: {TEXT_BRIGHT};
    font-size: 13px;
}}
QMessageBox QPushButton {{
    min-width: 80px;
}}

/* ── Input ── */
QLineEdit, QTextEdit {{
    background: {BG_DEEP};
    border: 1px solid {BORDER};
    border-radius: 8px;
    color: {TEXT_BRIGHT};
    padding: 7px 10px;
    selection-background-color: {SELECTION};
}}
QLineEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
}}

/* ── Menu ── */
QMenu {{
    background: {BG_MID};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{
    padding: 8px 28px 8px 16px;
    border-radius: 6px;
    color: {TEXT_PRIMARY};
}}
QMenu::item:selected {{
    background: {SELECTION};
    color: {TEXT_BRIGHT};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

/* ── ToolTip ── */
QToolTip {{
    background: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 4px;
    color: {TEXT_BRIGHT};
    padding: 6px 10px;
    font-size: 12px;
}}

/* ── Progress ── */
QProgressBar {{
    background: {BG_DEEP};
    border: none;
    border-radius: 4px;
    height: 6px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {ACCENT}, stop:1 {ACCENT_HOVER});
    border-radius: 4px;
}}

/* ── List / Tree ── */
QListWidget, QTreeWidget {{
    background: {BG_DARK};
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item, QTreeWidget::item {{
    padding: 7px 12px;
    border-bottom: 1px solid {BORDER_DIM};
    color: {TEXT_PRIMARY};
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background: #161C28;
}}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {SELECTION};
    color: {TEXT_BRIGHT};
}}

/* ── GroupBox ── */
QGroupBox {{
    background: #10131A;
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 16px;
    padding-top: 20px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 16px;
    padding: 0 8px;
    color: {TEXT_SECONDARY};
    font-weight: 600;
}}

/* ── ComboBox ── */
QComboBox {{
    background: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 8px;
    color: {TEXT_BRIGHT};
    padding: 6px 10px;
    min-height: 20px;
}}
QComboBox QAbstractItemView {{
    background: {BG_MID};
    border: 1px solid {BORDER};
    selection-background-color: {SELECTION};
}}

/* ── CheckBox ── */
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 2px solid #3A4050;
    border-radius: 5px;
    background: {BG_DEEP};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}

/* ── Tools page label ── */
QLabel#toolsHeader {{
    color: {TEXT_BRIGHT};
    font-size: 18px;
    font-weight: 700;
    padding: 4px 0px 12px 0px;
    min-height: 28px;
}}
"""
