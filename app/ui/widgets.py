"""Glass-styled widgets: buttons, metric grids, sparklines and cards."""

from __future__ import annotations

from collections import deque
from html import escape
from typing import Callable, Iterable

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .theme import FONT_ICON, TEXT, TEXT_DIM, WHITE, font, rgba


class IconButton(QWidget):
    clicked = Signal()

    def __init__(
        self,
        glyph: str,
        size: int = 22,
        color: str = TEXT_DIM,
        parent=None,
        scale: float = 1.0,
    ) -> None:
        super().__init__(parent)
        self.glyph = glyph
        self.color = color
        self.checked = False
        self._hover = False
        self.scale = scale
        side = max(1, int(round(size * scale)))
        self.setFixedSize(side, side)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._hover or self.checked:
            p.setPen(Qt.NoPen)
            p.setBrush(WHITE if not self.checked else rgba(self.color, 60))
            p.setOpacity(0.10 if not self.checked else 1.0)
            radius = 6 * self.scale
            p.drawRoundedRect(
                QRectF(self.rect()).adjusted(1, 1, -1, -1), radius, radius
            )
            p.setOpacity(1.0)
        p.setPen(QPen(QColor(self.color if self.checked or self._hover else TEXT_DIM)))
        p.setFont(self._icon_font())
        p.drawText(self.rect(), Qt.AlignCenter, self.glyph)

    def _icon_font(self):
        f = font(9.5, scale=self.scale)
        f.setFamily(FONT_ICON)
        return f


class TitleBar(QWidget):
    """Draggable header with window action buttons."""

    lock_clicked = Signal()
    pin_clicked = Signal()
    collapse_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self._drag_offset: QPoint | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.px(2), 0, 0, 0)
        layout.setSpacing(theme.px(4))
        self.setFixedHeight(theme.px(28))

        dot = _Dot(self)
        layout.addWidget(dot)
        self.title = QLabel(title)
        self.title.setFont(font(10.5, 700))
        self.title.setStyleSheet(f"color:{TEXT};")
        layout.addWidget(self.title)
        layout.addStretch(1)

        icon_size = theme.px(22)
        self.collapse_btn = IconButton("\uE73F", icon_size, scale=theme.SCALE)
        self.pin_btn = IconButton("\uE840", icon_size, scale=theme.SCALE)
        self.lock_btn = IconButton("\uE785", icon_size, scale=theme.SCALE)
        self.close_btn = IconButton(
            "\uE8BB", icon_size, "#FF9AA8", scale=theme.SCALE
        )
        self.close_btn.setToolTip("最小化到系统托盘")
        self.collapse_btn.clicked.connect(self.collapse_clicked)
        self.pin_btn.clicked.connect(self.pin_clicked)
        self.lock_btn.clicked.connect(self.lock_clicked)
        self.close_btn.clicked.connect(self.close_clicked)
        for button in (self.collapse_btn, self.pin_btn, self.lock_btn, self.close_btn):
            layout.addWidget(button)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None


class _Dot(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(theme.px(10), theme.px(10))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(rgba("#6FE3B7", 60))
        p.drawEllipse(QRectF(self.rect()))
        p.setBrush(QColor("#6FE3B7"))
        inset = theme.px(2.5)
        p.drawEllipse(QRectF(inset, inset, self.width() - 2 * inset, self.height() - 2 * inset))


# Reserved widths keep changing values from reflowing the panel.
_VALUE_WIDTHS = {
    "usage": 52,
    "freq": 72,
    "temp": 56,
    "cores": 74,
    "mem": 92,
    "power": 56,
    "clock": 74,
    "fan": 52,
    "fps": 66,
    "refresh": 66,
    "app": 100,
    "signal": 74,
    "latency": 62,
    "target": 92,
    "down": 76,
    "up": 76,
    "link": 106,
    "channel": 40,
    "addr": 106,
}


class MetricGrid(QWidget):
    """Two-column name/value grid."""

    def __init__(self, columns: int = 2, parent=None) -> None:
        super().__init__(parent)
        self.columns = columns
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(theme.px(10))
        self._grid.setVerticalSpacing(theme.px(3))
        self._values: dict[str, QLabel] = {}
        self._items: list[tuple[str, QLabel, QLabel]] = []
        self._visibility: dict[str, bool] = {}

    def add(self, key: str, label: str, tip: str | None = None) -> None:
        name = QLabel(label)
        name.setFont(font(8.5))
        name.setStyleSheet(f"color:{TEXT_DIM};")
        value = QLabel("--")
        value.setTextFormat(Qt.PlainText)  # values include process / SSID names
        value.setFont(font(9.5, 600, mono=True))
        value.setStyleSheet(f"color:{TEXT};")
        value.setFixedWidth(theme.px(_VALUE_WIDTHS.get(key, 72)))
        if tip:
            name.setToolTip(tip)
            value.setToolTip(tip)
        self._values[key] = value
        self._items.append((key, name, value))
        self._relayout()

    def relayout(self, visible: dict[str, bool]) -> None:
        self._visibility = dict(visible)
        self._relayout()

    def set_columns(self, columns: int) -> None:
        columns = max(1, int(columns))
        if columns == self.columns:
            return
        self.columns = columns
        self._relayout()

    def _relayout(self) -> None:
        while self._grid.count():
            self._grid.takeAt(0)
        index = 0
        for key, name, value in self._items:
            shown = self._visibility.get(key, True)
            name.setVisible(shown)
            value.setVisible(shown)
            if not shown:
                continue
            row, col = divmod(index, self.columns)
            self._grid.addWidget(name, row, col * 2, Qt.AlignLeft | Qt.AlignVCenter)
            self._grid.addWidget(value, row, col * 2 + 1, Qt.AlignLeft | Qt.AlignVCenter)
            self._grid.setColumnStretch(col * 2 + 1, 1)
            index += 1

    def set(self, key: str, text: str, color: str | None = None) -> None:
        label = self._values.get(key)
        if label is None:
            return
        text = str(text)
        if label.text() != text:
            label.setText(text)
            label.setStyleSheet(f"color:{color or TEXT};")


class _Series:
    __slots__ = ("name", "color", "values")

    def __init__(self, name: str, color: str, capacity: int) -> None:
        self.name = name
        self.color = color
        self.values: deque[float | None] = deque(maxlen=capacity)


class Sparkline(QWidget):
    def __init__(
        self,
        capacity: int = 120,
        range_hint: tuple[float, float] | None = None,
        floor_span: float = 1.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.capacity = capacity
        self.range_hint = range_hint
        self.floor_span = floor_span
        self.series: list[_Series] = []
        self.setFixedHeight(theme.px(40))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._palette_index = 0

    def ensure_series(self, name: str, color: str) -> _Series:
        for series in self.series:
            if series.name == name:
                return series
        series = _Series(name, color, self.capacity)
        self.series.append(series)
        return series

    def push(self, values: dict[str, float | None]) -> None:
        for series in self.series:
            series.values.append(values.get(series.name))
        self.update()

    def _bounds(self) -> tuple[float, float]:
        if self.range_hint is not None:
            return self.range_hint
        numbers = [v for s in self.series for v in s.values if v is not None]
        if not numbers:
            return 0.0, self.floor_span
        low, high = min(numbers), max(numbers)
        if high - low < self.floor_span:
            mid = (high + low) / 2
            low, high = mid - self.floor_span / 2, mid + self.floor_span / 2
        pad = (high - low) * 0.15
        low -= pad
        if min(numbers) >= 0:
            low = max(low, 0.0)  # keep non-negative series on the baseline
        return low, high + pad

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = theme.px(8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, int(9 * theme.VEIL)))
        p.drawRoundedRect(rect, radius, radius)

        p.setPen(QPen(QColor(255, 255, 255, int(14 * theme.VEIL)), 1))
        gap = theme.px(6)
        for i in (1, 2):
            y = rect.top() + rect.height() * i / 3
            p.drawLine(QPointF(rect.left() + gap, y), QPointF(rect.right() - gap, y))

        low, high = self._bounds()
        span = max(high - low, 1e-6)
        pad = theme.px(5)
        inner = rect.adjusted(pad, pad, -pad, -pad)

        for series in self.series:
            points: list[QPointF] = []
            count = len(series.values)
            if count < 2:
                continue
            step = inner.width() / (self.capacity - 1)
            start_x = inner.left() + max(0, self.capacity - count) * step
            for i, value in enumerate(series.values):
                if value is None:
                    continue
                x = start_x + i * step
                y = inner.bottom() - (value - low) / span * inner.height()
                points.append(QPointF(x, y))
            if len(points) < 2:
                continue
            path = _smooth_path(points)
            area = QPainterPath(path)
            area.lineTo(points[-1].x(), inner.bottom())
            area.lineTo(points[0].x(), inner.bottom())
            area.closeSubpath()
            gradient = QLinearGradient(0, inner.top(), 0, inner.bottom())
            gradient.setColorAt(0, rgba(series.color, 90))
            gradient.setColorAt(1, rgba(series.color, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(gradient))
            p.drawPath(area)
            p.setPen(QPen(QColor(series.color), max(1.0, 1.6 * theme.SCALE)))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

            last = points[-1]
            p.setPen(Qt.NoPen)
            p.setBrush(rgba(series.color, 60))
            p.drawEllipse(last, 4.2 * theme.SCALE, 4.2 * theme.SCALE)
            p.setBrush(QColor(series.color))
            p.drawEllipse(last, 2.0 * theme.SCALE, 2.0 * theme.SCALE)


def _smooth_path(points: list[QPointF]) -> QPainterPath:
    path = QPainterPath(points[0])
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2
        c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0, p1.y() + (p2.y() - p0.y()) / 6.0)
        c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0, p2.y() - (p3.y() - p1.y()) / 6.0)
        path.cubicTo(c1, c2, p2)
    return path


class ChartBlock(QWidget):
    """One collapsible chart: title + live legend + sparkline."""

    def __init__(
        self,
        title: str,
        palette: Iterable[str],
        range_hint: tuple[float, float] | None = None,
        floor_span: float = 1.0,
        formatter: Callable[[float], str] = lambda v: f"{v:.0f}",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._palette = list(palette)
        self._formatter = formatter
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.px(3))

        header = QHBoxLayout()
        header.setContentsMargins(theme.px(2), 0, theme.px(2), 0)
        self._title = QLabel(title)
        self._title.setFont(font(8.5))
        self._title.setStyleSheet(f"color:{TEXT_DIM};")
        header.addWidget(self._title)
        header.addStretch(1)
        self._legend = QLabel("")
        self._legend.setFont(font(8.5, 600, mono=True))
        self._legend.setStyleSheet(f"color:{TEXT};")
        self._legend.setTextFormat(Qt.RichText)
        header.addWidget(self._legend)
        layout.addLayout(header)

        self.spark = Sparkline(range_hint=range_hint, floor_span=floor_span)
        layout.addWidget(self.spark)
        self.setFixedHeight(theme.px(66))

    def push(self, values: dict[str, float | None]) -> None:
        for name in values:
            self.spark.ensure_series(name, self._palette[len(self.spark.series) % len(self._palette)])
        for series in self.spark.series:
            if len(series.values) == 0:
                series.values.extend([None] * (self.spark.capacity - 1))
        self.spark.push(values)
        parts = []
        for name, value in values.items():
            color = next((s.color for s in self.spark.series if s.name == name), TEXT)
            shown = "--" if value is None else self._formatter(value)
            label = name if len(name) <= 10 else name[:9] + "…"
            parts.append(
                f'<span style="color:{color}">&#9679;</span> {escape(label)} {shown}'
            )
        self._legend.setText("&nbsp;&nbsp;".join(parts))


class Collapsible(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._content = QWidget(self)
        self._inner = QVBoxLayout(self._content)
        self._inner.setContentsMargins(0, theme.px(2), 0, 0)
        self._inner.setSpacing(theme.px(4))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._content)
        self._content.setMaximumHeight(0)
        self._content.setVisible(False)
        self._expanded = False
        self._anim = QPropertyAnimation(self._content, b"maximumHeight", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)

    @property
    def expanded(self) -> bool:
        return self._expanded

    def add(self, widget: QWidget) -> None:
        self._inner.addWidget(widget)

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        target = self._content.sizeHint().height() if expanded else 0
        self._anim.stop()
        if expanded:
            self._content.setVisible(True)
        if not animate:
            self._content.setMaximumHeight(target)
            self._content.setVisible(expanded)
            return
        self._anim.setStartValue(self._content.maximumHeight())
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if not self._expanded:
            self._content.setVisible(False)


class _Header(QWidget):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


class Section(QWidget):
    """Rounded card with a clickable header and optional collapsible charts."""

    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        accent: str,
        collapsible: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.accent = accent
        self.collapsible = collapsible
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            theme.px(11), theme.px(9), theme.px(11), theme.px(9)
        )
        outer.setSpacing(theme.px(5))

        self._header = _Header(self)
        header = QHBoxLayout(self._header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.px(6))

        bar = QWidget(self._header)
        bar.setFixedSize(theme.px(3), theme.px(12))
        bar.setStyleSheet(
            f"background:{accent}; border-radius:{max(1, theme.px(1.5))}px;"
        )
        header.addWidget(bar, 0, Qt.AlignVCenter)

        self.title = QLabel(title)
        self.title.setFont(font(9.5, 700))
        self.title.setStyleSheet(f"color:{TEXT};")
        header.addWidget(self.title)

        self.subtitle = QLabel("")
        self.subtitle.setTextFormat(Qt.PlainText)  # subtitle shows SSID / device names
        self.subtitle.setFont(font(8))
        self.subtitle.setStyleSheet(f"color:{TEXT_DIM};")
        header.addWidget(self.subtitle, 1, Qt.AlignLeft | Qt.AlignVCenter)
        header.addStretch(1)

        self.chevron = QLabel("\uE70D" if collapsible else "")
        chevron_font = font(8)
        chevron_font.setFamily(FONT_ICON)
        self.chevron.setFont(chevron_font)
        self.chevron.setStyleSheet(f"color:{TEXT_DIM};")
        header.addWidget(self.chevron, 0, Qt.AlignVCenter)
        outer.addWidget(self._header)

        self.body = MetricGrid()
        outer.addWidget(self.body)

        self.charts = Collapsible(self)
        outer.addWidget(self.charts)
        outer.addStretch(1)

        if collapsible:
            self._header.setCursor(Qt.PointingHandCursor)
            self._header.clicked.connect(self._on_header_click)

    def _on_header_click(self) -> None:
        self.set_expanded(not self.charts.expanded)

    def add_chart(self, chart: ChartBlock) -> None:
        self.charts.add(chart)

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        if not self.collapsible:
            return
        self.charts.set_expanded(expanded, animate)
        self.chevron.setText("\uE70E" if self.charts.expanded else "\uE70D")
        self.toggled.emit(self.charts.expanded)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        veil = theme.VEIL
        radius = theme.px(12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(13, 17, 26, int(158 * veil)))
        p.drawRoundedRect(rect, radius, radius)
        p.setBrush(QColor(255, 255, 255, int(16 * veil)))
        p.drawRoundedRect(rect, radius, radius)
        p.setPen(QPen(QColor(255, 255, 255, int(30 * (0.35 + 0.65 * veil))), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, radius, radius)


class DeviceRow(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.px(6))
        self.dot = QLabel("\u25CF")
        self.dot.setFont(font(7))
        self.dot.setFixedWidth(theme.px(9))
        layout.addWidget(self.dot)
        self.name = QLabel("")
        self.name.setTextFormat(Qt.PlainText)  # BLE-advertised device name
        self.name.setFont(font(8.8))
        self.name.setStyleSheet(f"color:{TEXT};")
        self.name.setMinimumWidth(theme.px(110))
        layout.addWidget(self.name, 1)
        self.battery = QLabel("")
        self.battery.setFont(font(8.5, 600, mono=True))
        self.battery.setStyleSheet(f"color:{TEXT};")
        layout.addWidget(self.battery)
        self.signal = QLabel("")
        self.signal.setFont(font(8.5, 600, mono=True))
        self.signal.setStyleSheet(f"color:{TEXT};")
        self.signal.setMinimumWidth(theme.px(58))
        self.signal.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.signal)

    def update_device(self, device: dict, color: str) -> None:
        connected = device.get("connected")
        self.dot.setStyleSheet(f"color:{color if connected else TEXT_DIM};")
        name = device.get("name") or "未知设备"
        if len(name) > 16:
            name = name[:15] + "…"
        self.name.setText(name)
        battery = device.get("battery")
        self.battery.setText("--" if battery is None else f"{battery:.0f}%")
        rssi = device.get("rssi")
        self.signal.setText("--" if rssi is None else f"{rssi:.0f} dBm")
        if device.get("rssi_live"):
            self.signal.setToolTip("BLE 广播实测信号强度（实时）")
        else:
            self.signal.setToolTip(
                "来自 Windows 设备关联缓存：是系统最近一次测量值，"
                "可能略有延迟；设备重新广播时会自动切换为实时值"
            )
        tint = TEXT if connected else TEXT_DIM
        self.battery.setStyleSheet(f"color:{tint};")
        self.signal.setStyleSheet(f"color:{tint};")
        self.name.setStyleSheet(f"color:{tint};")


COMPACT_WIDTH = 176
COMPACT_HEIGHT = 56


class CompactView(QWidget):
    """The always-on-top mini readout: one rotating metric, click to expand."""

    clicked = Signal()
    drag_requested = Signal(QPoint)
    drag_finished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(theme.px(COMPACT_WIDTH), theme.px(COMPACT_HEIGHT))
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

        self._title = "CPU 使用率"
        self._value = "--"
        self._unit = ""
        self._sub = ""
        self._accent = "#7FB2FF"
        self._progress = 0.0
        self._alpha = 1.0
        self._hover = False
        self._press: QPoint | None = None
        self._moved = False
        self._locked = False

        self._fade = QPropertyAnimation(self, b"alpha", self)
        self._fade.setDuration(160)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

    def _get_alpha(self) -> float:
        return self._alpha

    def _set_alpha(self, value: float) -> None:
        self._alpha = value
        self.update()

    alpha = Property(float, _get_alpha, _set_alpha)

    def set_item(
        self,
        title: str,
        value: str,
        unit: str = "",
        accent: str = TEXT,
        sub: str = "",
        fade: bool = True,
    ) -> None:
        self._title = title
        self._value = value
        self._unit = unit
        self._accent = accent
        self._sub = sub
        if not fade:
            self._alpha = 1.0
            self.update()
            return
        self._fade.stop()
        self._fade.setStartValue(0.25)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def set_progress(self, fraction: float) -> None:
        self._progress = max(0.0, min(1.0, fraction))
        self.update()

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self.setToolTip("已锁定：鼠标可穿透 · Ctrl+Alt+L 解锁" if locked else "")
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._moved = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press is not None and event.buttons() & Qt.LeftButton:
            current = event.globalPosition().toPoint()
            if self._moved or (current - self._press).manhattanLength() > 4:
                self._moved = True
                self.drag_requested.emit(current - self._press)
                self._press = current

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            was_moved = self._moved
            self._press = None
            self._moved = False
            if was_moved:
                self.drag_finished.emit()
            else:
                self.clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        width = self.width()
        height = self.height()

        if self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 14))
            radius = theme.px(13)
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._accent))
        bar_height = theme.px(18)
        p.drawRoundedRect(
            QRectF(
                theme.px(9),
                (height - bar_height) / 2,
                theme.px(3),
                bar_height,
            ),
            max(1.0, 1.5 * theme.SCALE),
            max(1.0, 1.5 * theme.SCALE),
        )

        p.setOpacity(self._alpha)
        p.setPen(QColor(TEXT_DIM))
        p.setFont(font(8.2))
        p.drawText(
            QRectF(theme.px(19), theme.px(7), width - theme.px(30), theme.px(14)),
            Qt.AlignLeft | Qt.AlignVCenter,
            self._title,
        )

        value_font = font(15.5, 700, mono=True)
        p.setFont(value_font)
        p.setPen(QColor(self._accent))
        metrics = p.fontMetrics()
        p.drawText(
            QRectF(theme.px(18), theme.px(20), width - theme.px(28), theme.px(26)),
            Qt.AlignLeft | Qt.AlignVCenter,
            self._value,
        )
        if self._unit:
            unit_x = theme.px(18) + metrics.horizontalAdvance(self._value) + theme.px(3)
            p.setPen(QColor(TEXT_DIM))
            p.setFont(font(8.8))
            p.drawText(
                QRectF(
                    unit_x,
                    theme.px(22),
                    width - unit_x - theme.px(8),
                    theme.px(22),
                ),
                Qt.AlignLeft | Qt.AlignBottom,
                self._unit,
            )
        if self._sub:
            p.setPen(QColor(TEXT_DIM))
            p.setFont(font(7.8))
            p.drawText(
                QRectF(
                    width - theme.px(74),
                    theme.px(20),
                    theme.px(62) - (theme.px(10) if self._locked else 0),
                    theme.px(24),
                ),
                Qt.AlignRight | Qt.AlignVCenter,
                self._sub,
            )
        if self._locked:
            lock_font = font(7.5)
            lock_font.setFamily(FONT_ICON)
            p.setFont(lock_font)
            p.setPen(QColor(TEXT_DIM))
            p.drawText(
                QRectF(width - theme.px(16), theme.px(7), theme.px(12), theme.px(12)),
                Qt.AlignRight | Qt.AlignTop,
                "\uE72E",
            )
        p.setOpacity(1.0)

        bar = QRectF(
            theme.px(9), height - theme.px(8), width - theme.px(18), theme.px(2)
        )
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(bar, 1, 1)
        if self._progress > 0:
            filled = QRectF(bar)
            filled.setWidth(bar.width() * self._progress)
            p.setBrush(rgba(self._accent, 200))
            p.drawRoundedRect(filled, 1, 1)
