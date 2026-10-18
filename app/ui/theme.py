"""Colours and fonts for the glass panel."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont

TEXT = "#E9EFFB"
TEXT_DIM = "#96A4BD"
TEXT_FAINT = "#6E7C95"

CPU = "#7FB2FF"
GPU = "#6FE3B7"
WIFI = "#FFC66B"
BT = "#C79BFF"

SIGNAL = "#6FE3B7"
DOWN = "#6EA8FF"
UP = "#FF9DB0"
LATENCY = "#FFC66B"

WHITE = QColor(255, 255, 255)

# Background opacity multiplier: 1.0 is the normal glass, lower values let the
# desktop/game behind show through while the click-through lock is active.
VEIL = 1.0

# Overlay-only scale (font sizes, paddings and fixed widget metrics). The
# settings window and tray deliberately ignore it.
SCALE = 1.0


def set_veil(value: float) -> None:
    global VEIL
    VEIL = max(0.0, min(1.0, value))


def set_scale(value: float) -> None:
    global SCALE
    SCALE = max(0.6, min(1.5, value))


def px(value: float) -> int:
    return max(1, int(round(value * SCALE)))


FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"
FONT_ICON = "Segoe MDL2 Assets"


def font(
    size: float,
    weight: int = QFont.Normal,
    mono: bool = False,
    scale: float | None = None,
) -> QFont:
    f = QFont(FONT_MONO if mono else FONT_UI)
    factor = SCALE if scale is None else scale
    f.setPointSizeF(max(4.0, size * factor))
    f.setWeight(QFont.Weight(weight))
    if mono:
        f.setStyleHint(QFont.Monospace)
    return f


def ui_font(
    size: float, weight: int = QFont.Normal, mono: bool = False
) -> QFont:
    """Unscaled font for the settings window and tray-facing UI."""
    return font(size, weight, mono, scale=1.0)


def rgba(hex_color: str, alpha: int) -> QColor:
    color = QColor(hex_color)
    color.setAlpha(alpha)
    return color
