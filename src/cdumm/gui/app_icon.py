"""Resolve the runtime application icon consistently across platforms."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

from cdumm.platform import IS_LINUX, IS_MACOS


def application_icon_path() -> Path | None:
    """Return the first icon asset that exists for the current platform.

    The macOS bundle deliberately does not ship the Windows ``cdumm.ico``.
    Its PNG logo is included as a normal PyInstaller data file, making it
    suitable for both ``QApplication`` and ``QSystemTrayIcon``.
    """
    if getattr(sys, "frozen", False):
        root = Path(sys._MEIPASS)
    else:
        root = Path(__file__).resolve().parents[3]

    if IS_MACOS:
        candidates = (
            root / "assets" / "cdumm-logo.png",
            root / "cdumm.icns",
        )
    elif IS_LINUX:
        candidates = (
            root / "assets" / "cdumm-icon-square.png",
            root / "assets" / "cdumm-logo.png",
            root / "cdumm.ico",
        )
    else:
        candidates = (
            root / "cdumm.ico",
            root / "assets" / "cdumm-logo.png",
        )

    return next((candidate for candidate in candidates if candidate.exists()), None)


def application_icon() -> QIcon:
    """Load the platform application icon, or return a null icon."""
    path = application_icon_path()
    return QIcon(str(path)) if path is not None else QIcon()


def apply_application_icon(target) -> bool:
    """Set a Qt application/window icon without overriding the macOS Dock.

    A bundled macOS app gets its Dock icon from ``CFBundleIconFile``. Calling
    ``setWindowIcon`` with the raw PNG replaces that native icon at runtime
    and makes it render noticeably larger than neighboring Dock icons. The
    PNG remains available through :func:`application_icon` for the separate
    menu-bar status item.
    """
    if IS_MACOS:
        return False
    icon = application_icon()
    if icon.isNull():
        return False
    target.setWindowIcon(icon)
    return True
