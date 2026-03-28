"""Splash screen shown during app startup."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QSplashScreen


def show_splash() -> QSplashScreen:
    """Create and show a splash screen. Returns the splash so caller can finish() it."""
    pixmap = QPixmap(420, 200)
    pixmap.fill(QColor(30, 30, 30))

    painter = QPainter(pixmap)
    painter.setPen(QColor(76, 175, 80))
    painter.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "CDUMM")

    painter.setPen(QColor(180, 180, 180))
    painter.setFont(QFont("Segoe UI", 10))
    from cdumm import __version__
    painter.drawText(
        pixmap.rect().adjusted(0, 50, 0, 0),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        f"Crimson Desert Ultimate Mods Manager v{__version__}",
    )

    painter.setPen(QColor(120, 120, 120))
    painter.setFont(QFont("Segoe UI", 9))
    painter.drawText(
        pixmap.rect().adjusted(0, 0, 0, -15),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        "Loading...",
    )
    painter.end()

    splash = QSplashScreen(pixmap)
    splash.show()
    return splash
