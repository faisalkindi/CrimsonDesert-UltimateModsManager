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
        qss = (
            f"PrimaryPushButton {{ background: {base}; color: white; "
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
        qss = (
            f"PushButton {{ background: {base}; color: #FFFFFF; "
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
