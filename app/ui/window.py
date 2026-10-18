"""Desktop widget window: a tiny rotating readout that expands into a panel."""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

import psutil
from PySide6.QtCore import (
    Property,
    QAbstractNativeEventFilter,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from ..collectors.engine import CollectorEngine, Snapshot
from ..config import Config
from ..win32 import (
    WM_HOTKEY,
    display_refresh_rate,
    integrity_rid,
    open_uri,
    register_hotkey,
    set_click_through,
    set_no_activate,
    set_topmost,
)
from . import theme
from .display_options import CATEGORIES
from .glass import GlassPanel
from .theme import TEXT, TEXT_DIM, TEXT_FAINT, font
from .widgets import COMPACT_HEIGHT, COMPACT_WIDTH, ChartBlock, DeviceRow, Section, TitleBar

WINDOW_WIDTH = 408
SHADOW_MARGIN = 16
COMPACT_MARGIN = 9
ROTATE_MS = 3000

LOCATION_SETTINGS = "ms-settings:privacy-location"


def human_rate(value: float | None) -> str:
    if value is None:
        return "--"
    if value < 1024:
        return f"{value:.0f} B/s"
    if value < 1024**2:
        return f"{value / 1024:.0f} KB/s"
    return f"{value / 1024**2:.1f} MB/s"


def _fmt(value, spec: str, suffix: str = "", none: str = "--") -> str:
    if value is None:
        return none
    try:
        return f"{value:{spec}}{suffix}"
    except (ValueError, TypeError):
        return none


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if event_type == b"windows_generic_MSG" and message:
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == WM_HOTKEY and msg.wParam == 1:
                QTimer.singleShot(0, self.callback)
        return False, 0


class MonitorWindow(QWidget):
    def __init__(self, config: Config, watch_pid: int | None = None) -> None:
        super().__init__()
        self.config = config
        self.setWindowTitle("性能监控")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        self._drag_offset: QPoint | None = None
        self._device_rows: list[DeviceRow] = []
        self._hotkey_ok = False
        self._style_done = False
        self._low_integrity = (integrity_rid() or 0x2000) < 0x2000
        self._integrity = integrity_rid()
        self._watch: dict[int, float] = {}
        self._tray_tip_shown = False
        self._track_lifetime(watch_pid)
        self._track_lifetime(self._parent_pid())
        self._mode = "compact"
        self._compact_pos = QPoint()
        self._snap: Snapshot | None = None
        self._items: list[dict] = []
        self._rot_index = 0
        self._shown_index = -1
        self._ticks = 0
        self._eth_present = False
        self._user_moved = False
        self._veil = 1.0
        self._veil_anim = QPropertyAnimation(self, b"veil", self)
        self._veil_anim.setDuration(200)
        self._veil_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._alpha_save_timer = QTimer(self)
        self._alpha_save_timer.setSingleShot(True)
        self._alpha_save_timer.setInterval(400)
        self._alpha_save_timer.timeout.connect(self.config.save)

        self.panel = GlassPanel(self)
        self._build_ui()
        self._build_tray()
        self._build_timers()
        self._restore_geometry()
        self._apply_lock(bool(self.config.get("locked")), save=False)

        self.engine = CollectorEngine()
        self.engine.snapshot.connect(self.on_snapshot)
        self.engine.start()

        self._hotkey_filter = _HotkeyFilter(self._toggle_lock)
        QApplication.instance().installNativeEventFilter(self._hotkey_filter)
        QApplication.instance().aboutToQuit.connect(self._shutdown)

        self._life_timer = QTimer(self)
        self._life_timer.setInterval(2000)
        self._life_timer.timeout.connect(self._check_lifetime)
        self._life_timer.start()

    # ------------------------------------------------------------- lifetime
    @staticmethod
    def _parent_pid() -> int | None:
        try:
            return psutil.Process(os.getpid()).ppid() or None
        except Exception:
            return None

    def _track_lifetime(self, pid: int | None) -> None:
        if not pid or pid == os.getpid():
            return
        try:
            self._watch[pid] = psutil.Process(pid).create_time()
        except Exception:
            pass

    def _check_lifetime(self) -> None:
        """Quit when the launcher (or the session that started us) is gone."""
        for pid, created in list(self._watch.items()):
            try:
                alive = psutil.Process(pid).create_time() == created
            except Exception:
                alive = False
            if not alive:
                self._watch.clear()
                self._quit()
                return

    # ---------------------------------------------------------------- layout
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        margin = theme.px(SHADOW_MARGIN)
        outer.setContentsMargins(margin, margin, margin, margin)
        outer.addWidget(self.panel)

        self.panel_layout = QVBoxLayout(self.panel)
        self.panel_layout.setContentsMargins(
            theme.px(13), theme.px(10), theme.px(13), theme.px(11)
        )
        self.panel_layout.setSpacing(7)

        from .widgets import CompactView

        self.compact_view = CompactView(self.panel)
        self.compact_view.clicked.connect(lambda: self._set_mode("full"))
        self.compact_view.drag_requested.connect(self._drag_compact)
        self.compact_view.drag_finished.connect(self._remember_position)
        self.panel_layout.addWidget(self.compact_view)

        self.full_page = QWidget(self.panel)
        self.full_page.setMinimumWidth(theme.px(350))
        page_layout = QVBoxLayout(self.full_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(theme.px(7))

        self.title_bar = TitleBar("性能监控", self)
        self.title_bar.lock_clicked.connect(self._toggle_lock)
        self.title_bar.pin_clicked.connect(self._toggle_topmost)
        self.title_bar.collapse_clicked.connect(lambda: self._set_mode("compact"))
        self.title_bar.close_clicked.connect(self.hide)
        page_layout.addWidget(self.title_bar)

        self._horizontal_layout = self.horizontal_layout()
        self.content = QWidget(self.full_page)
        inner = self._new_content_layout(self._horizontal_layout)
        page_layout.addWidget(self.content)

        self.cpu_section = Section("CPU", theme.CPU)
        self.cpu_section.body.add("usage", "使用率", "全核平均占用率，与任务管理器同源")
        self.cpu_section.body.add("freq", "频率", "基频 × PDH %ProcessorPerformance，包含睿频")
        self.cpu_section.body.add("temp", "热区温度", "ACPI 热区传感器读数，不是 CPU 核心封装温度")
        self.cpu_section.body.add("cores", "核心")
        self._add_section(inner, self.cpu_section)

        self.gpu_section = Section("GPU", theme.GPU, collapsible=True)
        self.gpu_section.body.add("usage", "使用率", "NVIDIA NVML 读数，仅独显")
        self.gpu_section.body.add("temp", "温度", "NVIDIA NVML 核心温度")
        self.gpu_section.body.add("mem", "显存")
        self.gpu_section.body.add("power", "功耗")
        self.gpu_section.body.add("clock", "频率", "SM 核心频率")
        self.gpu_section.body.add("fan", "风扇")
        self.gpu_section.body.add("fps", "帧率", "当前前台应用的实时帧率（PresentMon 统计近 1 秒出帧数）")
        self.gpu_section.body.add("refresh", "刷新率", "当前显示模式的实际刷新率")
        self.gpu_section.body.add("app", "进程")
        self.gpu_section.body.set("app", "--", TEXT_FAINT)
        self.fps_chart = ChartBlock(
            "帧率",
            [theme.GPU],
            floor_span=30,
            formatter=lambda v: f"{v:.0f} fps",
        )
        self.gpu_section.add_chart(self.fps_chart)
        self._add_section(inner, self.gpu_section)

        self.wifi_section = Section("Wi-Fi", theme.WIFI, collapsible=True)
        self.wifi_section.body.add("signal", "信号", "WlanAPI 真实 RSSI；无定位权限时为估算值")
        self.wifi_section.body.add("latency", "Wi-Fi 延迟", "绑定 Wi-Fi 网卡测得的公网 TCP 往返时间")
        self.wifi_section.body.add("target", "测速 IP", "外网延迟实际访问的地址")
        self.wifi_section.body.add("down", "下行", "仅统计 Wi-Fi 网卡自身的接收速率")
        self.wifi_section.body.add("up", "上行", "仅统计 Wi-Fi 网卡自身的发送速率")
        self.wifi_section.body.add("link", "链路", "WlanAPI 协商速率")
        self.wifi_section.body.add("channel", "信道")
        self.wifi_signal_chart = ChartBlock(
            "信号强度",
            [theme.SIGNAL],
            range_hint=(-100, -30),
            formatter=lambda v: f"{v:.0f} dBm",
        )
        self.wifi_rate_chart = ChartBlock(
            "收发速率",
            [theme.DOWN, theme.UP],
            floor_span=64 * 1024,
            formatter=human_rate,
        )
        self.wifi_latency_chart = ChartBlock(
            "Wi-Fi 延迟",
            [theme.LATENCY],
            floor_span=20,
            formatter=lambda v: f"{v:.0f} ms",
        )
        for chart in (self.wifi_signal_chart, self.wifi_rate_chart, self.wifi_latency_chart):
            self.wifi_section.add_chart(chart)
        self.wifi_hint = QLabel("")
        self.wifi_hint.setFont(font(8))
        self.wifi_hint.setTextFormat(Qt.RichText)
        self.wifi_hint.setWordWrap(True)
        self.wifi_hint.setOpenExternalLinks(False)
        self.wifi_hint.linkActivated.connect(self._open_location_settings)
        self.wifi_hint.setStyleSheet(f"color:{TEXT_DIM};")
        self.wifi_hint.setVisible(False)
        self.wifi_section.layout().insertWidget(2, self.wifi_hint)
        self._add_section(inner, self.wifi_section)

        self.eth_section = Section("有线网络", theme.DOWN, collapsible=True)
        self.eth_section.body.add("latency", "有线延迟", "绑定有线网卡测得的公网 TCP 往返时间")
        self.eth_section.body.add("target", "测速 IP", "外网延迟实际访问的地址")
        self.eth_section.body.add("down", "下行", "仅统计有线网卡自身的接收速率")
        self.eth_section.body.add("up", "上行", "仅统计有线网卡自身的发送速率")
        self.eth_section.body.add("link", "链路")
        self.eth_section.body.add("addr", "地址")
        self.eth_latency_chart = ChartBlock(
            "有线延迟",
            [theme.LATENCY],
            floor_span=20,
            formatter=lambda v: f"{v:.0f} ms",
        )
        self.eth_section.add_chart(self.eth_latency_chart)
        self.eth_section.setVisible(False)
        self._add_section(inner, self.eth_section)

        self.bt_section = Section("蓝牙", theme.BT, collapsible=True)
        self.bt_section.body.hide()
        self.device_box = QWidget(self.bt_section)
        self.device_layout = QVBoxLayout(self.device_box)
        self.device_layout.setContentsMargins(0, 0, 0, 0)
        self.device_layout.setSpacing(2)
        self.bt_section.layout().insertWidget(2, self.device_box)
        self.bt_hint = QLabel("正在读取蓝牙设备…")
        self.bt_hint.setTextFormat(Qt.PlainText)  # shows exception text
        self.bt_hint.setFont(font(8.3))
        self.bt_hint.setStyleSheet(f"color:{TEXT_DIM};")
        self.device_layout.addWidget(self.bt_hint)
        self.bt_signal_chart = ChartBlock(
            "信号强度",
            [theme.BT, theme.SIGNAL, theme.WIFI],
            range_hint=(-100, -30),
            formatter=lambda v: f"{v:.0f} dBm",
        )
        self.bt_section.add_chart(self.bt_signal_chart)
        self._add_section(inner, self.bt_section)

        self.footer = QLabel("")
        self.footer.setFont(font(7.8))
        self.footer.setStyleSheet(f"color:{TEXT_FAINT};")
        self.footer.setWordWrap(False)
        page_layout.addWidget(self.footer)

        self.panel_layout.addWidget(self.full_page)

        collapsed = self.config.get("collapsed", {})
        self.wifi_section.set_expanded(not collapsed.get("wifi", False), animate=False)
        self.bt_section.set_expanded(not collapsed.get("bt", True), animate=False)
        self.eth_section.set_expanded(not collapsed.get("eth", False), animate=False)
        self.gpu_section.set_expanded(not collapsed.get("gpu", False), animate=False)
        self.wifi_section.toggled.connect(
            lambda expanded: self._on_chart_toggle("wifi", expanded)
        )
        self.bt_section.toggled.connect(lambda expanded: self._on_chart_toggle("bt", expanded))
        self.eth_section.toggled.connect(
            lambda expanded: self._on_chart_toggle("eth", expanded)
        )
        self.gpu_section.toggled.connect(
            lambda expanded: self._on_chart_toggle("gpu", expanded)
        )
        self._update_footer(None)
        self._veiled_widgets = self.findChildren(Section) + self.findChildren(ChartBlock)
        self._set_grid_columns(self._horizontal_layout)
        self._apply_visibility()

    def horizontal_layout(self) -> bool:
        return self.config.get("orientation") == "horizontal"

    def _new_content_layout(self, horizontal: bool):
        layout = QHBoxLayout(self.content) if horizontal else QVBoxLayout(self.content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.px(7))
        return layout

    def _add_section(self, layout, section) -> None:
        layout.addWidget(section, 1 if self._horizontal_layout else 0)

    def _sections(self) -> list[Section]:
        return [
            self.cpu_section,
            self.gpu_section,
            self.wifi_section,
            self.eth_section,
            self.bt_section,
        ]

    def _update_full_width(self) -> None:
        if not hasattr(self, "full_page"):
            return
        for section in self._sections():
            section.setMinimumWidth(0)
            section.setMaximumWidth(16777215)
        if self.horizontal_layout():
            visible = [section for section in self._sections() if not section.isHidden()]
            count = max(1, len(visible))
            card = theme.px(268)
            for section in visible:
                section.setFixedWidth(card)
            width = count * card + max(0, count - 1) * theme.px(7)
            self.full_page.setFixedWidth(max(width, theme.px(350)))
        else:
            self.full_page.setFixedWidth(theme.px(350))

    def _set_grid_columns(self, horizontal: bool) -> None:
        for section in (
            self.cpu_section,
            self.gpu_section,
            self.wifi_section,
            self.eth_section,
        ):
            section.body.set_columns(1 if horizontal else 2)

    def apply_ui_scale(self, scale: float) -> None:
        try:
            scale = max(0.6, min(1.5, float(scale)))
        except (TypeError, ValueError):
            return
        try:
            current = float(self.config.get("ui_scale", 1.0))
        except (TypeError, ValueError):
            current = 1.0
        if abs(scale - current) < 1e-3:
            return
        self.config["ui_scale"] = scale
        self.config.save()
        self._restart()

    def set_horizontal_layout(self, horizontal: bool) -> None:
        if horizontal == self.horizontal_layout():
            return
        self.config["orientation"] = "horizontal" if horizontal else "vertical"
        self.config.save()
        self._user_moved = False
        self._rebuild_content(horizontal)
        self._apply_mode(self._mode)
        if self._mode == "full":
            self._schedule_resize()
        self._sync_settings()

    def _rebuild_content(self, horizontal: bool) -> None:
        sections = [
            self.cpu_section,
            self.gpu_section,
            self.wifi_section,
            self.eth_section,
            self.bt_section,
        ]
        page_layout = self.full_page.layout()
        index = max(1, page_layout.indexOf(self.content))
        old_layout = self.content.layout()
        if old_layout is not None:
            while old_layout.count():
                old_layout.takeAt(0)
        page_layout.removeWidget(self.content)
        self.content.hide()
        self.content.deleteLater()

        self.content = QWidget(self.full_page)
        layout = QHBoxLayout(self.content) if horizontal else QVBoxLayout(self.content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.px(7))
        self._set_grid_columns(horizontal)
        for section in sections:
            layout.addWidget(section, 1 if horizontal else 0)
        self.content_layout = layout
        page_layout.insertWidget(index, self.content)
        self._horizontal_layout = horizontal
        self._update_full_width()
        self.content.show()

    def _on_chart_toggle(self, key: str, expanded: bool) -> None:
        collapsed = dict(self.config.get("collapsed", {}))
        collapsed[key] = not expanded
        self.config["collapsed"] = collapsed
        self.config.save()
        if self._mode == "full":
            QTimer.singleShot(0, self._resize_full)

    def _build_timers(self) -> None:
        self._rot_timer = QTimer(self)
        self._rot_timer.setInterval(ROTATE_MS)
        self._rot_timer.timeout.connect(self._rotate)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(60)
        self._tick_timer.timeout.connect(self._tick)

    # ---------------------------------------------------------------- tray
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip("性能监控")
        self.tray.activated.connect(self._on_tray_activated)

        self.act_show = QAction("显示 / 隐藏", self)
        self.act_show.triggered.connect(self._toggle_visible)
        self.act_mode = QAction("展开面板", self)
        self.act_mode.triggered.connect(self._toggle_mode)
        self.act_lock = QAction("鼠标穿透（锁定）", self)
        self.act_lock.setCheckable(True)
        self.act_lock.triggered.connect(lambda _checked: self._toggle_lock())
        self.act_settings = QAction("设置…", self)
        self.act_settings.triggered.connect(self._open_settings)
        self.act_location = QAction("打开定位权限设置", self)
        self.act_location.triggered.connect(self._open_location_settings)
        self.act_reset = QAction("重置位置", self)
        self.act_reset.triggered.connect(self._reset_position)
        self.act_restart = QAction("重启程序", self)
        self.act_restart.triggered.connect(self._restart)
        self.act_quit = QAction("退出程序", self)
        self.act_quit.triggered.connect(self._quit)

        # Right-click on the overlay: quick actions only.
        self.menu = QMenu()
        for action in (self.act_show, self.act_mode, self.act_lock):
            self.menu.addAction(action)
        self.menu.addSeparator()
        for action in (self.act_location, self.act_reset, self.act_restart):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.act_quit)

        # Tray menu adds the entry point to the settings window.
        self.tray_menu = QMenu()
        for action in (self.act_show, self.act_mode, self.act_lock):
            self.tray_menu.addAction(action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.act_settings)
        self.tray_menu.addSeparator()
        for action in (self.act_location, self.act_reset, self.act_restart):
            self.tray_menu.addAction(action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.act_quit)

        self.tray.setContextMenu(self.tray_menu)
        self.tray.show()

    def _build_settings(self) -> None:
        from .settings import SettingsDialog

        self._settings = SettingsDialog(self)

    def _open_settings(self) -> None:
        if not hasattr(self, "_settings"):
            self._build_settings()
        self._settings.prepare()
        self._settings.adjustSize()
        screen = self.screen() or QApplication.primaryScreen()
        self._settings.move(
            screen.availableGeometry().center() - self._settings.rect().center()
        )
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    def _sync_settings(self) -> None:
        settings = getattr(self, "_settings", None)
        if settings is not None and settings.isVisible():
            settings.sync_state()

    def _lock_alpha(self) -> float:
        try:
            return max(0.0, min(1.0, float(self.config.get("lock_alpha", 0.5))))
        except (TypeError, ValueError):
            return 0.5

    def transparency_percent(self) -> int:
        return int(round((1.0 - self._lock_alpha()) * 100))

    def set_lock_transparency(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        alpha = (100 - percent) / 100.0
        self.config["lock_alpha"] = alpha
        self._set_veil(alpha)
        self._alpha_save_timer.start()

    def end_veil_preview(self) -> None:
        if not self.config.get("locked") and self._veil < 1.0:
            self._animate_veil(1.0)

    def flush_pending_save(self) -> None:
        if self._alpha_save_timer.isActive():
            self._alpha_save_timer.stop()
            self.config.save()

    def hidden_items(self) -> set[str]:
        hidden = self.config.get("hidden")
        if not isinstance(hidden, list):
            return set()
        return {item for item in hidden if isinstance(item, str)}

    def item_visible(self, key: str) -> bool:
        return key not in self.hidden_items()

    def set_item_visible(self, key: str, visible: bool) -> None:
        hidden = self.hidden_items()
        if visible:
            hidden.discard(key)
        else:
            hidden.add(key)
        self.config["hidden"] = sorted(hidden)
        self.config.save()
        self._apply_visibility()

    def show_all_items(self) -> None:
        self.config["hidden"] = []
        self.config.save()
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        hidden = self.hidden_items()

        def visible(key: str) -> bool:
            return key not in hidden

        grids = {
            "cpu": self.cpu_section.body,
            "gpu": self.gpu_section.body,
            "wifi": self.wifi_section.body,
            "eth": self.eth_section.body,
        }
        for category in CATEGORIES:
            if category.key not in grids:
                continue
            grids[category.key].relayout(
                {
                    item.key.split(".", 1)[1]: visible(item.key)
                    for item in category.items
                }
            )

        def any_visible(category_key: str) -> bool:
            category = next(c for c in CATEGORIES if c.key == category_key)
            return any(visible(item.key) for item in category.items)

        self.cpu_section.setVisible(any_visible("cpu"))
        self.gpu_section.setVisible(any_visible("gpu"))
        self.wifi_section.setVisible(any_visible("wifi"))
        self.eth_section.setVisible(self._eth_present and any_visible("eth"))
        self.bt_section.setVisible(any_visible("bt"))

        self.fps_chart.setVisible(visible("gpu.fps"))
        self.wifi_signal_chart.setVisible(visible("wifi.signal"))
        self.wifi_latency_chart.setVisible(visible("wifi.latency"))
        self.wifi_rate_chart.setVisible(visible("wifi.down") or visible("wifi.up"))
        self.eth_latency_chart.setVisible(visible("eth.latency"))
        self.bt_signal_chart.setVisible(visible("bt.chart"))
        self.device_box.setVisible(visible("bt.devices"))

        self.gpu_section.charts.setVisible(visible("gpu.fps"))
        self.wifi_section.charts.setVisible(
            any(
                visible(key)
                for key in ("wifi.signal", "wifi.latency", "wifi.down", "wifi.up")
            )
        )
        self.eth_section.charts.setVisible(visible("eth.latency"))
        self.bt_section.charts.setVisible(visible("bt.chart"))

        self._update_full_width()
        if self._mode == "full":
            QTimer.singleShot(0, self._resize_full)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._toggle_visible()

    @staticmethod
    def _make_icon() -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing)
        gradient = QLinearGradient(0, 0, 64, 64)
        gradient.setColorAt(0, QColor("#6FE3B7"))
        gradient.setColorAt(1, QColor("#6EA8FF"))
        p.setBrush(gradient)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(4, 4, 56, 56))
        p.setPen(QColor(12, 18, 26))
        p.setFont(font(22, 700, scale=1.0))
        p.drawText(pixmap.rect(), Qt.AlignCenter, "P")
        p.end()
        return QIcon(pixmap)

    # ---------------------------------------------------------------- modes
    def _toggle_mode(self) -> None:
        self._set_mode("compact" if self._mode == "full" else "full")

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._apply_mode(mode)
        self.config["mode"] = mode
        self.config.save()
        if mode == "full":
            self._schedule_resize()

    def _apply_mode(self, mode: str, remember: bool = True) -> None:
        compact = mode == "compact"
        self._mode = mode
        self.compact_view.setVisible(compact)
        self.full_page.setVisible(not compact)
        self.panel.radius = theme.px(13 if compact else 18)
        self.act_mode.setText("展开面板" if compact else "收起为小窗")

        margin = theme.px(COMPACT_MARGIN if compact else SHADOW_MARGIN)
        self.layout().setContentsMargins(margin, margin, margin, margin)
        if compact:
            self.panel_layout.setContentsMargins(0, 0, 0, 0)
        else:
            self.panel_layout.setContentsMargins(
                theme.px(13), theme.px(10), theme.px(13), theme.px(11)
            )
        # Let the layout own the window size so expanding a chart resizes smoothly.
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.layout().setSizeConstraint(QLayout.SetFixedSize)
        self.adjustSize()

        size = self.size()
        screen = QApplication.primaryScreen().availableGeometry()
        if compact:
            x, y = self._compact_pos.x(), self._compact_pos.y()
        elif self.horizontal_layout():
            x = screen.left() + max(0, (screen.width() - size.width()) // 2)
            y = self._compact_pos.y()
        else:
            x = (
                self._compact_pos.x()
                + theme.px(COMPACT_WIDTH)
                + 2 * theme.px(COMPACT_MARGIN)
                - size.width()
            )
            y = self._compact_pos.y()
        x = max(screen.left(), min(x, screen.right() - size.width() + 1))
        y = max(screen.top(), min(y, screen.bottom() - size.height() + 1))
        self.move(x, y)

        if compact:
            self._ticks = 0
            self._shown_index = -1
            self._rot_timer.start()
            self._tick_timer.start()
            self._refresh_compact()
        else:
            self._rot_timer.stop()
            self._tick_timer.stop()
        if remember and not compact:
            self._remember_position()

    def _schedule_resize(self) -> None:
        for delay in (0, 80, 400):
            QTimer.singleShot(delay, self._resize_full)

    def _resize_full(self) -> None:
        self.layout().activate()
        self.adjustSize()
        screen = QApplication.primaryScreen().availableGeometry()
        if self.horizontal_layout():
            if self._user_moved:
                x = min(
                    max(self.x(), screen.left()),
                    max(screen.left(), screen.right() - self.width() + 1),
                )
            else:
                x = screen.left() + max(0, (screen.width() - self.width()) // 2)
            self.move(x, self.y())
        if self.y() + self.height() > screen.bottom():
            self.move(self.x(), max(screen.top(), screen.bottom() - self.height()))

    def _drag_compact(self, delta: QPoint) -> None:
        self.move(self.pos() + delta)
        self._compact_pos = self.pos()

    # ---------------------------------------------------------------- state
    def _get_veil(self) -> float:
        return self._veil

    def _set_veil(self, value: float) -> None:
        self._veil = value
        theme.set_veil(value)
        self.panel.update()
        for widget in getattr(self, "_veiled_widgets", ()):
            widget.update()
        self.update()

    veil = Property(float, _get_veil, _set_veil)

    def _animate_veil(self, value: float, animate: bool = True) -> None:
        value = max(0.0, min(1.0, value))
        self._veil_anim.stop()
        if not animate:
            self._set_veil(value)
            return
        self._veil_anim.setStartValue(self._veil)
        self._veil_anim.setEndValue(value)
        self._veil_anim.start()

    def _apply_lock(self, locked: bool, save: bool = True) -> None:
        self.config["locked"] = locked
        try:
            set_click_through(int(self.winId()), locked)
        except Exception:
            pass
        # Locked: fade the glass out to the remembered transparency level.
        self._animate_veil(
            self._lock_alpha() if locked else 1.0, animate=self.isVisible()
        )
        self.compact_view.set_locked(locked)
        self.title_bar.lock_btn.checked = locked
        self.title_bar.lock_btn.color = theme.GPU if locked else TEXT_DIM
        self.title_bar.lock_btn.glyph = "\uE72E" if locked else "\uE785"
        self.title_bar.lock_btn.update()
        if hasattr(self, "act_lock"):
            self.act_lock.setChecked(locked)
            self.act_lock.setText("鼠标穿透（已锁定）" if locked else "鼠标穿透（锁定）")
        self.setWindowTitle("性能监控（已锁定）" if locked else "性能监控")
        if save:
            self.config.save()
        self._sync_settings()

    def set_locked(self, locked: bool) -> None:
        self._apply_lock(locked)

    def _toggle_lock(self) -> None:
        self._apply_lock(not bool(self.config.get("locked")))

    def _toggle_topmost(self) -> None:
        self.set_topmost_enabled(not bool(self.config.get("topmost", True)))

    def set_topmost_enabled(self, topmost: bool) -> None:
        topmost = bool(topmost)
        self.config["topmost"] = topmost
        self.config.save()
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, topmost)
        if visible:
            self.show()
        try:
            # Re-creating the native window drops the manual ex-styles.
            hwnd = int(self.winId())
            set_no_activate(hwnd)
            set_click_through(hwnd, bool(self.config.get("locked")))
        except Exception:
            pass
        set_topmost(int(self.winId()), topmost)
        self.title_bar.pin_btn.checked = topmost
        self.title_bar.pin_btn.color = theme.CPU if topmost else TEXT_DIM
        self.title_bar.pin_btn.update()
        self._sync_settings()

    def _toggle_visible(self) -> None:
        self.hide() if self.isVisible() else self.show()

    def _reset_position(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self._user_moved = False
        self._compact_pos = QPoint(
            screen.right()
            - theme.px(COMPACT_WIDTH)
            - 2 * theme.px(COMPACT_MARGIN)
            - theme.px(24),
            screen.top() + theme.px(90),
        )
        self._apply_mode(self._mode)
        self._remember_position()

    def _restore_geometry(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        x = self.config.get("x")
        y = self.config.get("y")
        if x is None or y is None:
            x = (
                screen.right()
                - theme.px(COMPACT_WIDTH)
                - 2 * theme.px(COMPACT_MARGIN)
                - theme.px(24)
            )
            y = screen.top() + theme.px(90)
        else:
            x = max(screen.left(), min(int(x), screen.right() - theme.px(120)))
            y = max(screen.top(), min(int(y), screen.bottom() - theme.px(80)))
        self._compact_pos = QPoint(int(x), int(y))
        self._mode = "full" if self.config.get("mode") == "full" else "compact"
        self._apply_mode(self._mode, remember=False)
        topmost = bool(self.config.get("topmost", True))
        set_topmost(int(self.winId()), topmost)
        self.title_bar.pin_btn.checked = topmost
        self.title_bar.pin_btn.color = theme.CPU if topmost else TEXT_DIM
        if self._mode == "full":
            self._schedule_resize()

    def _remember_position(self) -> None:
        if self._mode == "compact":
            self._compact_pos = self.pos()
        else:
            self._compact_pos = QPoint(
                self.x()
                + self.width()
                - (theme.px(COMPACT_WIDTH) + 2 * theme.px(COMPACT_MARGIN)),
                self.y(),
            )
        self.config["x"] = self._compact_pos.x()
        self.config["y"] = self._compact_pos.y()
        self.config.save()

    def _shutdown(self) -> None:
        try:
            self.engine.stop()
            self.engine.wait(2500)
        except Exception:
            pass

    def _quit(self) -> None:
        self._remember_position()
        self._shutdown()
        QApplication.quit()

    def _restart(self) -> None:
        from PySide6.QtCore import QProcess

        from ..config import ROOT

        self._remember_position()
        self._shutdown()
        QProcess.startDetached(
            sys.executable,
            [str(ROOT / "main.py"), "--replace"],
            str(ROOT),
        )
        QApplication.quit()

    # ---------------------------------------------------------------- events
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._style_done:
            self._style_done = True
            try:
                set_no_activate(int(self.winId()))
            except Exception:
                pass
        if not self._hotkey_ok:
            self._hotkey_ok = register_hotkey(int(self.winId()))

    def nativeEvent(self, event_type, message):  # noqa: N802
        return super().nativeEvent(event_type, message)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        geometry = self.panel.geometry()
        spread = theme.px(10 if self._mode == "compact" else 14)
        radius = self.panel.radius
        offset = theme.px(2)
        for i in range(spread, 0, -1):
            alpha = int((int(8 * (1 - (i - 1) / spread) ** 1.6) + 1) * theme.VEIL)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(
                QRectF(geometry).adjusted(-i, -i + offset, i, i + offset),
                radius + i,
                radius + i,
            )

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape and self._mode == "full":
            self._set_mode("compact")
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._mode == "full":
            if event.position().y() < theme.px(SHADOW_MARGIN) * 2:
                self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            self._user_moved = True
            self._remember_position()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.act_mode.setText("收起为小窗" if self._mode == "full" else "展开面板")
        self.menu.exec(event.globalPos())

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()
        if not self._tray_tip_shown:
            self._tray_tip_shown = True
            self.tray.showMessage(
                "性能监控",
                "已最小化到系统托盘。右键托盘图标（可能在任务栏 ^ 里）→ 退出程序。",
                QSystemTrayIcon.Information,
                5000,
            )

    # ---------------------------------------------------------------- data
    def on_snapshot(self, snap: Snapshot) -> None:
        self._snap = snap
        self._update_cpu(snap.cpu)
        self._update_gpu(snap.gpu, snap.fps)
        self._update_wifi(snap.wifi)
        self._update_eth(snap.nics)
        self._update_bluetooth(snap.bluetooth, snap.bt_error)
        self._refresh_compact()

    def _update_cpu(self, cpu: dict) -> None:
        grid = self.cpu_section.body
        self.cpu_section.subtitle.setText(self._elide(cpu.get("name", "CPU"), 24))
        grid.set("usage", _fmt(cpu.get("usage"), ".0f", "%"), theme.CPU)
        freq = cpu.get("freq")
        grid.set("freq", _fmt(freq / 1000.0 if freq else None, ".2f", " GHz"))
        temp = cpu.get("temp")
        grid.set("temp", _fmt(temp, ".0f", " °C"), self._temp_color(temp))
        grid.set("cores", f"{cpu.get('cores', 0)}C / {cpu.get('threads', 0)}T", TEXT_DIM)

    def _update_gpu(self, gpu: dict, fps_info: dict) -> None:
        grid = self.gpu_section.body
        self.gpu_section.subtitle.setText(self._elide(gpu.get("name", "GPU"), 22))
        grid.set("usage", _fmt(gpu.get("usage"), ".0f", "%"), theme.GPU)
        temp = gpu.get("temp")
        grid.set("temp", _fmt(temp, ".0f", " °C"), self._temp_color(temp))
        if gpu.get("mem_used") is not None and gpu.get("mem_total"):
            grid.set("mem", f"{gpu['mem_used']:.0f}/{gpu['mem_total']:.0f} MB")
        else:
            grid.set("mem", "--", TEXT_FAINT)
        grid.set("power", _fmt(gpu.get("power"), ".0f", " W"))
        grid.set("clock", _fmt(gpu.get("clock"), ".0f", " MHz"))
        grid.set("fan", _fmt(gpu.get("fan"), ".0f", "%"))
        if fps_info.get("fps") is not None:
            fps_value = fps_info.get("fps")
            grid.set("fps", _fmt(fps_value, ".0f", " FPS"), theme.GPU)
            app = (fps_info.get("app") or "--").replace(".exe", "")
            if fps_info.get("detail"):
                app = f"{app} · {fps_info['detail']}"
            grid.set("app", self._elide(app, 18), TEXT_DIM)
            self.fps_chart.push({"FPS": fps_value})
        else:
            grid.set("fps", "--", TEXT_FAINT)
            detail = fps_info.get("detail") or ""
            grid.set("app", self._elide(detail or "需管理员", 14), TEXT_FAINT)
            self.fps_chart.push({"FPS": None})
        grid.set("refresh", _fmt(self._refresh_rate(), ".0f", " Hz"), TEXT_DIM)
        self._update_footer(fps_info)

    def _open_location_settings(self, _link: str = "") -> None:
        open_uri(LOCATION_SETTINGS)

    def _update_wifi(self, wifi: dict) -> None:
        grid = self.wifi_section.body
        status = wifi.get("status")
        connected = wifi.get("connected")
        ssid = wifi.get("ssid") or ""
        if connected:
            subtitle = self._elide(ssid, 16) if ssid else "已连接"
            if status == "permission":
                subtitle = f"{subtitle} · 详情需权限" if ssid else "已连接 · 详情需权限"
        else:
            subtitle = "未连接"
        self.wifi_section.subtitle.setText(subtitle)

        rssi = wifi.get("rssi")
        pct = wifi.get("signal_pct")
        if rssi is not None:
            suffix = "~" if wifi.get("rssi_estimated") else ""
            grid.set("signal", f"{suffix}{rssi:.0f} dBm" + (f" ({pct}%)" if pct else ""),
                     self._signal_color(pct))
        elif connected:
            grid.set("signal", "需定位权限", theme.WIFI)
        else:
            grid.set("signal", "--", TEXT_FAINT)
        grid.set(
            "latency",
            _fmt(wifi.get("latency"), ".0f", " ms"),
            self._latency_color(wifi.get("latency")),
        )
        grid.set("target", (wifi.get("latency_target") or "--").split(":")[0], TEXT_DIM)
        if connected:
            grid.set("down", human_rate(wifi.get("rx_bps")), theme.DOWN)
            grid.set("up", human_rate(wifi.get("tx_bps")), theme.UP)
        else:
            grid.set("down", "--", TEXT_FAINT)
            grid.set("up", "--", TEXT_FAINT)
        rx, tx = wifi.get("rx_rate"), wifi.get("tx_rate")
        if rx is not None and tx is not None:
            phy = f" {wifi['phy']}" if wifi.get("phy") else ""
            grid.set("link", f"{rx:.0f}/{tx:.0f} Mbps{phy}")
        else:
            grid.set("link", "--", TEXT_FAINT)
        grid.set("channel", str(wifi.get("channel") or "--"))

        if status == "permission":
            if self._low_integrity:
                self.wifi_hint.setText(
                    f'<a href="open" style="color:{theme.WIFI};text-decoration:none;">'
                    f"当前进程权限受限（完整性 0x{self._integrity or 0:X}），Windows 拒绝读取 Wi-Fi</a><br>"
                    "请用「以管理员身份启动.bat」或从资源管理器双击「启动监控.bat」重新启动。"
                )
            else:
                self.wifi_hint.setText(
                    f'<a href="open" style="color:{theme.WIFI};text-decoration:none;">'
                    "定位权限未开启，点此打开设置</a><br>"
                    "需要同时打开「定位服务」和「让桌面应用访问你的位置」，"
                    "开启后几秒内自动生效。"
                )
            self._set_wifi_hint(True)
        else:
            self._set_wifi_hint(False)

        self.wifi_signal_chart.push({"信号": rssi if rssi is not None else None})
        self.wifi_rate_chart.push(
            {"下行": wifi.get("rx_bps") if connected else None,
             "上行": wifi.get("tx_bps") if connected else None}
        )
        self.wifi_latency_chart.push({"Wi-Fi": wifi.get("latency") if connected else None})

    def _update_eth(self, nics: list[dict]) -> None:
        eth = next((n for n in nics if n.get("kind") == "ethernet"), None)
        present = eth is not None
        if present != self._eth_present:
            self._eth_present = present
            self._apply_visibility()
        if not eth:
            return
        grid = self.eth_section.body
        self.eth_section.subtitle.setText(self._elide(eth.get("description") or eth["name"], 22))
        grid.set(
            "latency",
            _fmt(eth.get("latency"), ".0f", " ms"),
            self._latency_color(eth.get("latency")),
        )
        grid.set("target", (eth.get("latency_target") or "--").split(":")[0], TEXT_DIM)
        grid.set("down", human_rate(eth.get("rx_bps")), theme.DOWN)
        grid.set("up", human_rate(eth.get("tx_bps")), theme.UP)
        speed = eth.get("speed_mbps")
        grid.set("link", _fmt(speed, ".0f", " Mbps") if speed else "--", TEXT_DIM)
        grid.set("addr", eth.get("ipv4") or "--", TEXT_DIM)
        self.eth_latency_chart.push({"有线": eth.get("latency")})

    def _set_wifi_hint(self, visible: bool) -> None:
        if self.wifi_hint.isVisible() == visible:
            return
        self.wifi_hint.setVisible(visible)
        if self._mode == "full":
            QTimer.singleShot(0, self._resize_full)

    def _update_bluetooth(self, devices: list[dict], error: str | None) -> None:
        self.bt_section.subtitle.setText(f"{len(devices)} 个已连接" if devices else "")

        while len(self._device_rows) < min(4, len(devices)):
            row = DeviceRow(self.device_box)
            self.device_layout.insertWidget(self.device_layout.count() - 1, row)
            self._device_rows.append(row)
        self.bt_hint.setVisible(not devices)
        if error:
            self.bt_hint.setText(f"蓝牙不可用：{self._elide(error, 30)}")
        elif not devices:
            self.bt_hint.setText("没有已连接的蓝牙设备")

        palette = [theme.BT, theme.SIGNAL, theme.WIFI, theme.DOWN]
        for index, row in enumerate(self._device_rows):
            if index < len(devices):
                row.update_device(devices[index], palette[index % len(palette)])
                row.show()
            else:
                row.hide()

        signals: dict[str, float | None] = {}
        for device in devices[:3]:
            signals[device["name"]] = device.get("rssi")
        self.bt_signal_chart.push(signals)

    # ---------------------------------------------------------------- compact
    def _compact_entries(self) -> list[dict]:
        snap = self._snap
        if snap is None:
            return [dict(title="正在采集", value="…", unit="", accent=TEXT_DIM, sub="")]

        entries: list[dict] = []
        cpu = snap.cpu or {}
        gpu = snap.gpu or {}
        wifi = snap.wifi or {}
        fps = snap.fps or {}

        usage = cpu.get("usage")
        if usage is not None:
            entries.append(
                dict(
                    key="cpu.usage",
                    title="CPU 使用率",
                    value=f"{usage:.0f}",
                    unit="%",
                    accent=theme.CPU,
                    sub=f"{cpu.get('cores', 0)}C/{cpu.get('threads', 0)}T",
                )
            )
        temp = cpu.get("temp")
        entries.append(
            dict(
                key="cpu.temp",
                title="CPU 热区温度",
                value="--" if temp is None else f"{temp:.0f}",
                unit="°C",
                accent=self._temp_color(temp),
                sub="",
            )
        )
        freq = cpu.get("freq")
        entries.append(
            dict(
                key="cpu.freq",
                title="CPU 频率",
                value="--" if not freq else f"{freq / 1000:.2f}",
                unit="GHz",
                accent=theme.CPU,
                sub=f"基准{(cpu.get('rated') or 0) / 1000:.1f}G",
            )
        )

        gpu_usage = gpu.get("usage")
        entries.append(
            dict(
                key="gpu.usage",
                title="GPU 使用率",
                value="--" if gpu_usage is None else f"{gpu_usage:.0f}",
                unit="%",
                accent=theme.GPU,
                sub="",
            )
        )
        gpu_temp = gpu.get("temp")
        entries.append(
            dict(
                key="gpu.temp",
                title="GPU 温度",
                value="--" if gpu_temp is None else f"{gpu_temp:.0f}",
                unit="°C",
                accent=self._temp_color(gpu_temp),
                sub="",
            )
        )
        if gpu.get("mem_used") is not None and gpu.get("mem_total"):
            entries.append(
                dict(
                    key="gpu.mem",
                    title="显存占用",
                    value=f"{gpu['mem_used'] / 1024:.1f}",
                    unit="GB",
                    accent=theme.GPU,
                    sub=f"/{gpu['mem_total'] / 1024:.0f}G",
                )
            )
        if gpu.get("power") is not None:
            entries.append(
                dict(
                    key="gpu.power",
                    title="GPU 功耗",
                    value=f"{gpu['power']:.0f}",
                    unit="W",
                    accent=theme.GPU,
                    sub="",
                )
            )

        if fps.get("fps") is not None:
            entries.append(
                dict(
                    key="gpu.fps",
                    title="帧率",
                    value=f"{fps['fps']:.0f}",
                    unit="FPS",
                    accent=theme.GPU,
                    sub=(fps.get("app") or "").replace(".exe", "")[:9],
                )
            )
        else:
            rate = self._refresh_rate()
            entries.append(
                dict(
                    key="gpu.refresh",
                    title="刷新率",
                    value="--" if rate is None else f"{rate:.0f}",
                    unit="Hz",
                    accent=TEXT,
                    sub=(fps.get("detail") or "FPS需管理员")[:9],
                )
            )

        status = wifi.get("status")
        wifi_connected = wifi.get("connected")
        if wifi_connected:
            if wifi.get("rssi") is not None:
                entries.append(
                    dict(
                        key="wifi.signal",
                        title="Wi-Fi 信号",
                        value=f"{wifi['rssi']:.0f}",
                        unit="dBm",
                        accent=self._signal_color(wifi.get("signal_pct")),
                        sub=self._elide(wifi.get("ssid") or "", 7),
                    )
                )
            else:
                entries.append(
                    dict(
                        key="wifi.signal",
                        title="Wi-Fi 状态",
                        value="已连接",
                        unit="",
                        accent=theme.WIFI,
                        sub=(
                            "权限受限"
                            if status == "permission" and self._low_integrity
                            else ("需定位权限" if status == "permission" else "")
                        ),
                    )
                )
            if wifi.get("latency") is not None:
                entries.append(
                    dict(
                        key="wifi.latency",
                        title="Wi-Fi 延迟",
                        value=f"{wifi['latency']:.0f}",
                        unit="ms",
                        accent=self._latency_color(wifi.get("latency")),
                        sub=(wifi.get("latency_target") or "").split(":")[0],
                    )
                )
            rate_text = human_rate(wifi.get("rx_bps"))
            rate_value, _, rate_unit = rate_text.partition(" ")
            entries.append(
                dict(
                    key="wifi.down",
                    title="Wi-Fi 下行",
                    value=rate_value,
                    unit=rate_unit,
                    accent=theme.DOWN,
                    sub="",
                )
            )
        else:
            entries.append(
                dict(
                    key="wifi.signal",
                    title="Wi-Fi 状态",
                    value="未连接",
                    unit="",
                    accent=TEXT_DIM,
                    sub="",
                )
            )

        if not wifi_connected and wifi.get("latency") is not None:
            entries.append(
                dict(
                    key="wifi.latency",
                    title="Wi-Fi 延迟",
                    value=f"{wifi['latency']:.0f}",
                    unit="ms",
                    accent=self._latency_color(wifi.get("latency")),
                    sub="",
                )
            )

        eth = next((n for n in (snap.nics or []) if n.get("kind") == "ethernet"), None)
        if eth:
            if eth.get("latency") is not None:
                entries.append(
                    dict(
                        key="eth.latency",
                        title="有线延迟",
                        value=f"{eth['latency']:.0f}",
                        unit="ms",
                        accent=self._latency_color(eth.get("latency")),
                        sub=(eth.get("latency_target") or "").split(":")[0],
                    )
                )
            down_text = human_rate(eth.get("rx_bps"))
            down_value, _, down_unit = down_text.partition(" ")
            entries.append(
                dict(
                    key="eth.down",
                    title="有线下行",
                    value=down_value,
                    unit=down_unit,
                    accent=theme.DOWN,
                    sub=eth.get("ipv4") or "",
                )
            )
            up_text = human_rate(eth.get("tx_bps"))
            up_value, _, up_unit = up_text.partition(" ")
            entries.append(
                dict(
                    key="eth.up",
                    title="有线上行",
                    value=up_value,
                    unit=up_unit,
                    accent=theme.UP,
                    sub="",
                )
            )

        devices = snap.bluetooth or []
        if devices:
            with_battery = [d for d in devices if d.get("battery") is not None]
            target = with_battery[0] if with_battery else devices[0]
            connected_count = sum(1 for d in devices if d.get("connected"))
            if target.get("battery") is not None:
                entries.append(
                    dict(
                        key="bt.devices",
                        title="蓝牙电量",
                        value=f"{target['battery']:.0f}",
                        unit="%",
                        accent=theme.BT,
                        sub=self._elide(target.get("name") or "", 7),
                    )
                )
            else:
                entries.append(
                    dict(
                        key="bt.devices",
                        title="蓝牙设备",
                        value=str(len(devices)),
                        unit="台",
                        accent=theme.BT,
                        sub=f"{connected_count} 已连接",
                    )
                )
        entries = [entry for entry in entries if self.item_visible(entry["key"])]
        if not entries:
            entries.append(
                dict(
                    title="显示内容",
                    value="已全部隐藏",
                    unit="",
                    accent=TEXT_DIM,
                    sub="托盘 → 设置",
                )
            )
        return entries

    def _refresh_compact(self) -> None:
        if self._mode != "compact":
            return
        entries = self._compact_entries()
        if not entries:
            return
        self._items = entries
        if self._rot_index >= len(entries):
            self._rot_index = 0
        fade = self._rot_index != self._shown_index
        entry = entries[self._rot_index]
        self.compact_view.set_item(
            entry["title"], entry["value"], entry["unit"], entry["accent"], entry["sub"], fade=fade
        )
        self._shown_index = self._rot_index

    def _rotate(self) -> None:
        if self._mode != "compact" or not self._items:
            return
        self._rot_index = (self._rot_index + 1) % len(self._items)
        self._ticks = 0
        self._refresh_compact()

    def _tick(self) -> None:
        self._ticks = (self._ticks + 1) % (ROTATE_MS // self._tick_timer.interval())
        self.compact_view.set_progress(self._ticks / max(1, ROTATE_MS // self._tick_timer.interval()))

    # ---------------------------------------------------------------- helpers
    def _update_footer(self, fps_info: dict | None) -> None:
        hint = "点击 ⤢ 收起为小窗 · 右键菜单 · Ctrl+Alt+L 穿透锁定"
        if fps_info and fps_info.get("source") == "screen":
            hint += " · 帧率来自屏幕刷新采样"
        elif fps_info and fps_info.get("fps") is None and fps_info.get("source") != "starting":
            hint += " · 管理员运行显示 FPS"
        self.footer.setText(hint)

    def _refresh_rate(self) -> float | None:
        rate = display_refresh_rate()
        if rate is not None:
            return rate
        screen = self.screen() or QApplication.primaryScreen()
        try:
            return float(screen.refreshRate())
        except Exception:
            return None

    @staticmethod
    def _elide(text: str, limit: int) -> str:
        text = str(text)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _temp_color(temp: float | None) -> str:
        if temp is None:
            return TEXT_FAINT
        if temp >= 88:
            return "#FF7A7A"
        if temp >= 75:
            return "#FFC66B"
        return TEXT

    @staticmethod
    def _latency_color(latency: float | None) -> str:
        if latency is None:
            return TEXT_FAINT
        if latency >= 120:
            return "#FF7A7A"
        if latency >= 60:
            return "#FFC66B"
        return theme.LATENCY

    @staticmethod
    def _signal_color(pct: int | None) -> str:
        if pct is None:
            return TEXT_FAINT
        if pct < 35:
            return "#FF7A7A"
        if pct < 60:
            return "#FFC66B"
        return theme.SIGNAL
