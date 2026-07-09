"""Central accent-colour plumbing.

CDUMM's brand blue (#2878D0) is hardcoded into many button stylesheets via
``setCustomStyleSheet``. Because that custom QSS sits *on top* of the
qfluentwidgets theme, calling ``setThemeColor`` from the Settings accent
picker recolours the native controls but leaves those hardcoded buttons
blue — the "main buttons stay blue" bug.

This module lets those buttons take their colour from the *current* theme
accent and re-apply it whenever the accent changes, so the accent picker
propagates to every main action button live.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor

DEFAULT_ACCENT = "#2878D0"


def current_accent() -> QColor:
    """The live theme accent colour (falls back to the brand blue)."""
    try:
        from qfluentwidgets import themeColor
        c = themeColor()
        if c.isValid():
            return c
    except Exception:
        pass
    return QColor(DEFAULT_ACCENT)


def accent_hex() -> str:
    return current_accent().name()


def accent_shades() -> tuple[str, str, str]:
    """Return ``(base, hover, pressed)`` hexes derived from the accent."""
    c = current_accent()
    return c.name(), c.lighter(115).name(), c.darker(118).name()


def _rel_luminance(c: QColor) -> float:
    """WCAG relative luminance of a colour (0..1)."""
    def _lin(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    return (0.2126 * _lin(c.red()) + 0.7152 * _lin(c.green())
            + 0.0722 * _lin(c.blue()))


def accent_fg() -> str:
    """Best-contrast text colour for text sitting *on* the accent.

    White text on a bright accent (e.g. teal/lime) is hard to read, so we
    switch to near-black once white's contrast drops off. A slight bias
    keeps the familiar white-on-blue look for the default accent.
    """
    L = _rel_luminance(current_accent())
    contrast_white = 1.05 / (L + 0.05)
    contrast_black = (L + 0.05) / 0.05
    return "#FFFFFF" if contrast_white * 1.1 >= contrast_black else "#141414"


class _AccentBus(QObject):
    changed = Signal()


_bus = _AccentBus()


def bus() -> _AccentBus:
    return _bus


def notify_changed() -> None:
    """Fire after ``setThemeColor`` so registered widgets restyle."""
    try:
        _bus.changed.emit()
    except Exception:
        pass


def style_primary_button(btn, radius: int = 20, padding: str = "0 28px") -> None:
    """Style a PrimaryPushButton from the accent and keep it in sync.

    Only the shape args are fixed; the colour tracks the theme accent, so
    the button recolours the moment the user picks a new accent.
    """
    from qfluentwidgets import setCustomStyleSheet

    def _apply() -> None:
        base, hover, pressed = accent_shades()
        fg = accent_fg()
        qss = (
            f"PrimaryPushButton {{ background: {base}; color: {fg}; "
            f"border-radius: {radius}px; border: none; padding: {padding}; }}"
            f"PrimaryPushButton:hover {{ background: {hover}; }}"
            f"PrimaryPushButton:pressed {{ background: {pressed}; }}"
        )
        try:
            setCustomStyleSheet(btn, qss, qss)
        except Exception:
            pass

    _apply()
    _bus.changed.connect(_apply)


def style_accent_pushbutton(btn, radius: int = 20, padding: str = "0 28px") -> None:
    """Style a (non-primary) PushButton that should look like an accent
    call-to-action, tracking the theme accent."""
    from qfluentwidgets import setCustomStyleSheet

    def _apply() -> None:
        base, hover, pressed = accent_shades()
        fg = accent_fg()
        qss = (
            f"PushButton {{ background: {base}; color: {fg}; "
            f"border: 1px solid {base}; border-radius: {radius}px; "
            f"padding: {padding}; }}"
            f"PushButton:hover {{ background: {hover}; border-color: {hover}; }}"
            f"PushButton:pressed {{ background: {pressed}; border-color: {pressed}; }}"
        )
        try:
            setCustomStyleSheet(btn, qss, qss)
        except Exception:
            pass

    _apply()
    _bus.changed.connect(_apply)


def style_chip_button(btn, radius: int = 16,
                      padding_css: str = "padding: 0 14px;") -> None:
    """Style a light "chip" PushButton (secondary pills like New Folder /
    Refresh) so its text, border and faint wash all track the theme accent
    instead of a hardcoded ``#2878D0`` blue.

    The wash is a translucent tint of the accent, so a single stylesheet
    reads correctly over both the light and dark card backgrounds. Pass the
    button's own ``padding_css`` fragment (property included) to preserve its
    existing geometry.
    """
    from qfluentwidgets import setCustomStyleSheet

    def _apply() -> None:
        c = current_accent()
        r, g, b = c.red(), c.green(), c.blue()
        qss = (
            f"PushButton {{ background: rgba({r},{g},{b},0.14); color: {c.name()}; "
            f"border: 1px solid rgba({r},{g},{b},0.55); "
            f"border-radius: {radius}px; {padding_css} }}"
            f"PushButton:hover {{ background: rgba({r},{g},{b},0.22); }}"
            f"PushButton:pressed {{ background: rgba({r},{g},{b},0.30); }}"
        )
        try:
            setCustomStyleSheet(btn, qss, qss)
        except Exception:
            pass

    _apply()
    _bus.changed.connect(_apply)
