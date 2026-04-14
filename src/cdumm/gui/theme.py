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


def set_theme(theme_name: str) -> None:
    """Set the active palette."""
    global _current_palette
    if theme_name == "light":
        _current_palette = dict(LIGHT_PALETTE)
    else:
        _current_palette = dict(DARK_PALETTE)


def is_dark() -> bool:
    """Check if the current theme is dark."""
    return _current_palette.get("bg_deep") == DARK_PALETTE["bg_deep"]


# Backward compatibility constants — used by legacy v2 main_window.py
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
