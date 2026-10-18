"""Glass-styled settings window with drill-down pages."""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..win32 import autostart_enabled, set_autostart
from .display_options import CATEGORIES, Category
from .glass import GlassPanel
from .theme import FONT_ICON, TEXT, TEXT_DIM, TEXT_FAINT, ui_font
from .widgets import IconButton

WINDOW_WIDTH = 360
PANEL_MARGIN = 16
CONTENT_WIDTH = WINDOW_WIDTH - 2 * PANEL_MARGIN

SLIDER_STYLE = """
QSlider::groove:horizontal {
    height: 4px;
    background: rgba(255, 255, 255, 34);
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #6FE3B7;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 12px;
    margin: -4px 0;
    border-radius: 6px;
    background: #E9EFFB;
}
"""


class _Switch(QWidget):
    """Small animated on/off switch matching the overlay accents."""

    toggled = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(36, 20)
        self.setCursor(Qt.PointingHandCursor)
        self._checked = False
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def setChecked(self, checked: bool, animate: bool = True) -> None:  # noqa: N802
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self._anim.stop()
        if not animate:
            self._set_offset(1.0 if checked else 0.0)
            return
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        off = (70, 80, 95)
        on = (111, 227, 183)
        color = QColor(
            int(off[0] + (on[0] - off[0]) * self._offset),
            int(off[1] + (on[1] - off[1]) * self._offset),
            int(off[2] + (on[2] - off[2]) * self._offset),
        )
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        x = 2 + (rect.width() - 16) * self._offset
        p.setBrush(QColor(233, 239, 251))
        p.drawEllipse(QRectF(x, 2, 16, 16))


class _Row(QWidget):
    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(38)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self._hover = False

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._hover:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


class _ToggleRow(_Row):
    toggled = Signal(str, bool)

    def __init__(self, key: str, label: str, checked: bool, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self._syncing = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 8, 0)
        layout.setSpacing(8)
        self.label = QLabel(label)
        self.label.setFont(ui_font(9.2))
        self.label.setStyleSheet(f"color:{TEXT};")
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.switch = _Switch(self)
        self.switch.setChecked(checked, animate=False)
        self.switch.toggled.connect(self._on_switch)
        layout.addWidget(self.switch, 0, Qt.AlignVCenter)
        self.clicked.connect(self._toggle)

    def _toggle(self) -> None:
        self.switch.setChecked(not self.switch.isChecked())
        self.switch.toggled.emit(self.switch.isChecked())

    def _on_switch(self, checked: bool) -> None:
        if not self._syncing:
            self.toggled.emit(self.key, checked)

    def sync(self, checked: bool) -> None:
        if self.switch.isChecked() == checked:
            return
        self._syncing = True
        self.switch.setChecked(checked, animate=False)
        self._syncing = False


class _NavRow(_Row):
    def __init__(
        self, title: str, subtitle: str = "", accent: str | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(8)
        if accent:
            dot = QLabel("\u25CF")
            dot.setFont(ui_font(6))
            dot.setStyleSheet(f"color:{accent};")
            dot.setFixedWidth(9)
            layout.addWidget(dot)
        self.title = QLabel(title)
        self.title.setFont(ui_font(9.2))
        self.title.setStyleSheet(f"color:{TEXT};")
        layout.addWidget(self.title)
        layout.addStretch(1)
        self._subtitle = QLabel(subtitle)
        self._subtitle.setFont(ui_font(8))
        self._subtitle.setStyleSheet(f"color:{TEXT_DIM};")
        layout.addWidget(self._subtitle)
        arrow = QLabel("\uE76C")
        arrow_font = ui_font(8)
        arrow_font.setFamily(FONT_ICON)
        arrow.setFont(arrow_font)
        arrow.setStyleSheet(f"color:{TEXT_DIM};")
        layout.addWidget(arrow)

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)


class _Header(QWidget):
    back_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(28)
        self._drag_offset: QPoint | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.back_btn = IconButton("\uE72B", 22)
        self.back_btn.setToolTip("返回")
        self.back_btn.clicked.connect(self.back_clicked)
        self.back_btn.setVisible(False)
        layout.addWidget(self.back_btn)
        dot = QLabel("\u25CF")
        dot.setFont(ui_font(6.5))
        dot.setStyleSheet("color:#6FE3B7;")
        dot.setFixedWidth(10)
        layout.addWidget(dot)
        self.title = QLabel("设置")
        self.title.setFont(ui_font(11, 700))
        self.title.setStyleSheet(f"color:{TEXT};")
        layout.addWidget(self.title)
        layout.addStretch(1)
        self.close_btn = IconButton("\uE8BB", 22, "#FF9AA8")
        self.close_btn.setToolTip("关闭设置")
        self.close_btn.clicked.connect(self.close_clicked)
        layout.addWidget(self.close_btn)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None


class SettingsDialog(QWidget):
    def __init__(self, window) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint,
        )
        self._window = window
        self._syncing = False
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(600)
        self._scale_timer.timeout.connect(self._apply_scale)
        self._page = 0
        self._category: Category | None = None
        self._detail_rows: dict[str, _ToggleRow] = {}
        self.setWindowTitle("性能监控 - 设置")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(WINDOW_WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.panel = GlassPanel(self, dim=False)
        self.panel.radius = 16
        outer.addWidget(self.panel)

        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(PANEL_MARGIN, 14, PANEL_MARGIN, 14)
        layout.setSpacing(6)

        self.header = _Header(self.panel)
        self.header.back_clicked.connect(self._back)
        self.header.close_clicked.connect(self.close)
        layout.addWidget(self.header)

        self.stack = QStackedWidget(self.panel)
        self.stack.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(self.stack)

        self._main_page = self._build_main_page()
        self._display_page = self._build_display_page()
        self.stack.addWidget(self._main_page)
        self.stack.addWidget(self._display_page)
        self._category_pages: dict[str, QWidget] = {}
        for category in CATEGORIES:
            page = self._build_category_page(category)
            self.stack.addWidget(page)
            self._category_pages[category.key] = page

        self.alpha.valueChanged.connect(self._on_alpha)
        self._set_page(0)

    # ------------------------------------------------------------- pages
    @staticmethod
    def _caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(ui_font(8, 700))
        label.setStyleSheet(f"color:{TEXT_FAINT};")
        label.setContentsMargins(2, 4, 0, 0)
        return label

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(ui_font(7.8))
        label.setStyleSheet(f"color:{TEXT_FAINT};")
        label.setWordWrap(True)
        label.setFixedWidth(CONTENT_WIDTH)
        return label

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        layout.addWidget(self._caption("行为"))
        self.autostart = _ToggleRow("autostart", "开机自启动", autostart_enabled())
        self.autostart.toggled.connect(self._on_autostart)
        layout.addWidget(self.autostart)
        layout.addWidget(self._hint("登录 Windows 后自动在后台运行"))
        self.topmost = _ToggleRow(
            "topmost", "始终置顶", bool(self._window.config.get("topmost", True))
        )
        self.topmost.toggled.connect(self._on_topmost)
        layout.addWidget(self.topmost)
        self.locked = _ToggleRow(
            "locked", "鼠标穿透（锁定）", bool(self._window.config.get("locked"))
        )
        self.locked.toggled.connect(self._on_lock)
        layout.addWidget(self.locked)

        layout.addWidget(self._caption("外观"))
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 2, 10, 0)
        row_layout.setSpacing(8)
        label = QLabel("锁定后透明度")
        label.setFont(ui_font(9.2))
        label.setStyleSheet(f"color:{TEXT};")
        row_layout.addWidget(label)
        row_layout.addStretch(1)
        self.alpha_value = QLabel("50%")
        self.alpha_value.setFont(ui_font(9, 600, mono=True))
        self.alpha_value.setStyleSheet(f"color:{TEXT};")
        row_layout.addWidget(self.alpha_value)
        layout.addWidget(row)

        self.alpha = QSlider(Qt.Horizontal)
        self.alpha.setRange(0, 100)
        self.alpha.setSingleStep(5)
        self.alpha.setPageStep(10)
        self.alpha.setStyleSheet(SLIDER_STYLE)
        layout.addWidget(self.alpha)
        layout.addWidget(self._hint("0% 为正常玻璃背景，100% 时背景完全透明，只保留文字"))

        layout.addWidget(self._caption("布局"))
        self.horizontal = _ToggleRow(
            "horizontal", "卡片横向排列", self._window.horizontal_layout()
        )
        self.horizontal.toggled.connect(self._on_horizontal)
        layout.addWidget(self.horizontal)
        layout.addWidget(self._hint("关闭为竖向堆叠；横向时 CPU / GPU / 网络 / 蓝牙卡片并排"))

        layout.addWidget(self._caption("显示内容"))
        self.display_nav = _NavRow("自定义显示内容", "选择要看的指标")
        self.display_nav.clicked.connect(lambda: self._set_page(1))
        layout.addWidget(self.display_nav)

        layout.addWidget(self._caption("界面"))
        scale_row = QWidget()
        scale_layout = QHBoxLayout(scale_row)
        scale_layout.setContentsMargins(10, 2, 10, 0)
        scale_layout.setSpacing(8)
        scale_label = QLabel("界面缩放")
        scale_label.setFont(ui_font(9.2))
        scale_label.setStyleSheet(f"color:{TEXT};")
        scale_layout.addWidget(scale_label)
        scale_layout.addStretch(1)
        self.scale_value = QLabel("100%")
        self.scale_value.setFont(ui_font(9, 600, mono=True))
        self.scale_value.setStyleSheet(f"color:{TEXT};")
        scale_layout.addWidget(self.scale_value)
        layout.addWidget(scale_row)

        self.scale = QSlider(Qt.Horizontal)
        self.scale.setRange(60, 140)
        self.scale.setSingleStep(5)
        self.scale.setPageStep(10)
        self.scale.setStyleSheet(SLIDER_STYLE)
        self.scale.setValue(self._window_scale_percent())
        self.scale.valueChanged.connect(self._on_scale)
        self.scale.sliderReleased.connect(self._apply_scale)
        layout.addWidget(self.scale)
        layout.addWidget(
            self._hint("调整后应用会自动重启生效；只缩放性能监控悬浮窗，设置窗口和托盘不受影响")
        )

        self.status = QLabel("")
        self.status.setFont(ui_font(7.8))
        self.status.setStyleSheet(f"color:{TEXT_DIM};")
        self.status.setWordWrap(True)
        self.status.setFixedWidth(CONTENT_WIDTH)
        layout.addWidget(self.status)

        self.alpha.setValue(self._window.transparency_percent())
        return page

    def _build_display_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        layout.addWidget(self._caption("选择要显示的信息"))
        self._nav_rows: dict[str, _NavRow] = {}
        for category in CATEGORIES:
            row = _NavRow(category.title, "", category.accent)
            row.clicked.connect(lambda c=category: self._open_category(c))
            layout.addWidget(row)
            self._nav_rows[category.key] = row

        self.reset_row = _NavRow("恢复全部显示", "重新打开所有指标")
        self.reset_row.clicked.connect(self._reset_visible)
        layout.addWidget(self.reset_row)
        layout.addWidget(
            self._hint("关闭某项后，完整面板和小窗轮播都会跳过它；整组关闭会隐藏对应卡片。")
        )
        return page

    def _build_category_page(self, category: Category) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        for item in category.items:
            row = _ToggleRow(item.key, item.label, self._window.item_visible(item.key))
            row.toggled.connect(self._on_item_toggled)
            layout.addWidget(row)
            self._detail_rows[item.key] = row
        layout.addStretch(1)
        return page

    def _open_category(self, category: Category) -> None:
        self._category = category
        self._set_page(self.stack.indexOf(self._category_pages[category.key]))

    def _reset_visible(self) -> None:
        self._window.show_all_items()
        self._update_counts()
        for row in self._detail_rows.values():
            row.sync(True)

    def _set_page(self, index: int) -> None:
        self._page = index
        self.stack.setCurrentIndex(index)
        self.header.back_btn.setVisible(index != 0)
        if index == 0:
            self.header.title.setText("设置")
        elif index == 1:
            self.header.title.setText("显示内容")
        elif self._category is not None:
            self.header.title.setText(self._category.title)
        self.header.title.adjustSize()
        page = self.stack.currentWidget()
        if page.layout() is not None:
            page.layout().activate()
        page.adjustSize()
        height = page.sizeHint().height()
        screen = self._window.screen() or QApplication.primaryScreen()
        limit = screen.availableGeometry().height() - 20
        height = max(0, min(height, limit - 62))
        self.stack.setFixedHeight(height)
        self.setFixedSize(WINDOW_WIDTH, height + 62)
        self._keep_on_screen()

    def _back(self) -> None:
        if self._page >= 2:
            self._set_page(1)
        elif self._page == 1:
            self._set_page(0)
        else:
            self.close()

    def _keep_on_screen(self) -> None:
        screen = self._window.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = min(max(self.x(), geo.left()), geo.right() - self.width() + 1)
        y = min(max(self.y(), geo.top()), geo.bottom() - self.height() + 1)
        self.move(x, y)

    # ------------------------------------------------------------- state
    def prepare(self) -> None:
        self._set_page(0)
        self.sync_state()

    def sync_state(self) -> None:
        self._syncing = True
        try:
            self.autostart.sync(autostart_enabled())
            self.topmost.sync(bool(self._window.config.get("topmost", True)))
            self.locked.sync(bool(self._window.config.get("locked")))
            self.horizontal.sync(self._window.horizontal_layout())
            percent = self._window.transparency_percent()
            self.alpha.setValue(percent)
            self.alpha_value.setText(f"{percent}%")
            scale = self._window_scale_percent()
            self.scale.setValue(scale)
            self.scale_value.setText(f"{scale}%")
            self._update_counts()
            for key, row in self._detail_rows.items():
                row.sync(self._window.item_visible(key))
        finally:
            self._syncing = False

    def _window_scale_percent(self) -> int:
        try:
            scale = float(self._window.config.get("ui_scale", 1.0))
        except (TypeError, ValueError):
            scale = 1.0
        return int(round(max(0.6, min(1.5, scale)) * 100))

    def _on_scale(self, percent: int) -> None:
        self.scale_value.setText(f"{percent}%")
        if self._syncing:
            return
        if not self.scale.isSliderDown():
            self._scale_timer.start()

    def _apply_scale(self) -> None:
        self._scale_timer.stop()
        self._window.apply_ui_scale(self.scale.value() / 100.0)

    def _update_counts(self) -> None:
        for category in CATEGORIES:
            shown = sum(
                1 for item in category.items if self._window.item_visible(item.key)
            )
            self._nav_rows[category.key].set_subtitle(f"{shown}/{len(category.items)}")

    def showEvent(self, event) -> None:  # noqa: N802
        self.sync_state()
        super().showEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self._back()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()
        self._window.end_veil_preview()
        self._window.flush_pending_save()

    # ------------------------------------------------------------ actions
    def _on_autostart(self, _key: str, checked: bool) -> None:
        if self._syncing:
            return
        try:
            set_autostart(checked)
        except Exception as exc:
            self.autostart.sync(not checked)
            self.status.setText(f"自启动设置失败：{exc}")
            return
        self.status.setText("已开启开机自启动" if checked else "已关闭开机自启动")

    def _on_topmost(self, _key: str, checked: bool) -> None:
        if not self._syncing:
            self._window.set_topmost_enabled(checked)

    def _on_lock(self, _key: str, checked: bool) -> None:
        if not self._syncing:
            self._window.set_locked(checked)

    def _on_horizontal(self, _key: str, checked: bool) -> None:
        if not self._syncing:
            self._window.set_horizontal_layout(checked)

    def _on_alpha(self, percent: int) -> None:
        if self._syncing:
            return
        self.alpha_value.setText(f"{percent}%")
        self._window.set_lock_transparency(percent)

    def _on_item_toggled(self, key: str, checked: bool) -> None:
        if not self._syncing:
            self._window.set_item_visible(key, checked)
            self._update_counts()
