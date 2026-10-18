"""Rounded translucent panel shared by the overlay and the settings window."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import theme


class GlassPanel(QWidget):
    """Rounded translucent panel that lets the desktop tint through."""

    def __init__(self, parent=None, dim: bool = True) -> None:
        super().__init__(parent)
        self.radius = 18
        # The settings window keeps its own background readable even while the
        # overlay itself is faded by the lock transparency.
        self.dim = dim

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = self.radius

        veil = theme.VEIL if self.dim else 1.0
        base_alpha = int(226 * veil)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(38, 45, 62, base_alpha))
        gradient.setColorAt(0.55, QColor(24, 29, 41, base_alpha))
        gradient.setColorAt(1.0, QColor(15, 18, 27, int(min(255, 236) * veil)))

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.setClipPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(gradient)
        p.drawRect(rect)

        sheen = QLinearGradient(0, 0, self.width() * 0.9, self.height() * 0.6)
        sheen.setColorAt(0.0, QColor(255, 255, 255, int(30 * veil)))
        sheen.setColorAt(0.45, QColor(255, 255, 255, int(4 * veil)))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(sheen)
        p.drawRect(rect)
        p.setClipping(False)

        edge = int(48 * (0.35 + 0.65 * veil))
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor(255, 255, 255, edge))
        p.drawRoundedRect(rect, radius, radius)
        p.setPen(QColor(255, 255, 255, int(16 * (0.35 + 0.65 * veil))))
        p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius - 1, radius - 1)

        highlight = QLinearGradient(rect.left(), 0, rect.right(), 0)
        highlight.setColorAt(0.0, QColor(255, 255, 255, 0))
        highlight.setColorAt(0.5, QColor(255, 255, 255, int(120 * veil)))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(QPen(QBrush(highlight), max(1.0, radius * 0.07)))
        inset = max(4, int(radius))
        p.drawLine(
            QPoint(int(rect.left()) + inset, int(rect.top())),
            QPoint(int(rect.right()) - inset, int(rect.top())),
        )
