"""Splash screen shown during app startup."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QSplashScreen


def show_splash() -> QSplashScreen:
    """Create and show a splash screen."""
    pixmap = QPixmap(440, 220)

    painter = QPainter(pixmap)
    # Dark gradient background
    grad = QLinearGradient(0, 0, 440, 220)
    grad.setColorAt(0, QColor(13, 15, 19))
    grad.setColorAt(1, QColor(19, 22, 28))
    painter.fillRect(pixmap.rect(), grad)

    # Subtle top accent line
    painter.setPen(Qt.PenStyle.NoPen)
    accent_grad = QLinearGradient(0, 0, 440, 0)
    accent_grad.setColorAt(0, QColor(212, 162, 76, 0))
    accent_grad.setColorAt(0.5, QColor(212, 162, 76, 200))
    accent_grad.setColorAt(1, QColor(212, 162, 76, 0))
    painter.setBrush(accent_grad)
    painter.drawRect(0, 0, 440, 2)

    # App name
    painter.setPen(QColor(212, 162, 76))
    painter.setFont(QFont("Segoe UI", 26, QFont.Weight.Bold))
    painter.drawText(pixmap.rect().adjusted(0, -10, 0, 0),
                     Qt.AlignmentFlag.AlignCenter, "CDUMM")

    # Subtitle
    painter.setPen(QColor(150, 155, 165))
    painter.setFont(QFont("Segoe UI", 10))
    from cdumm import __version__
    painter.drawText(
        pixmap.rect().adjusted(0, 40, 0, 0),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignCenter,
        f"Crimson Desert Ultimate Mods Manager  v{__version__}",
    )

    # Loading text
    painter.setPen(QColor(75, 80, 96))
    painter.setFont(QFont("Segoe UI", 9))
    painter.drawText(
        pixmap.rect().adjusted(0, 0, 0, -16),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        "Loading...",
    )
    painter.end()

    splash = QSplashScreen(pixmap)
    splash.show()
    return splash
