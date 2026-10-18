"""Readout catalogue used by the settings drill-down."""

from __future__ import annotations

from dataclasses import dataclass

from . import theme


@dataclass(frozen=True)
class Item:
    key: str
    label: str


@dataclass(frozen=True)
class Category:
    key: str
    title: str
    accent: str
    items: tuple[Item, ...]


CATEGORIES: tuple[Category, ...] = (
    Category(
        "cpu",
        "CPU",
        theme.CPU,
        (
            Item("cpu.usage", "使用率"),
            Item("cpu.freq", "频率（含睿频）"),
            Item("cpu.temp", "热区温度"),
            Item("cpu.cores", "核心 / 线程"),
        ),
    ),
    Category(
        "gpu",
        "GPU",
        theme.GPU,
        (
            Item("gpu.usage", "使用率"),
            Item("gpu.temp", "温度"),
            Item("gpu.mem", "显存"),
            Item("gpu.power", "功耗"),
            Item("gpu.clock", "核心频率"),
            Item("gpu.fan", "风扇"),
            Item("gpu.fps", "帧率（含曲线）"),
            Item("gpu.refresh", "刷新率"),
            Item("gpu.app", "当前进程"),
        ),
    ),
    Category(
        "wifi",
        "Wi-Fi",
        theme.WIFI,
        (
            Item("wifi.signal", "信号（含曲线）"),
            Item("wifi.latency", "延迟（含曲线）"),
            Item("wifi.target", "测速 IP"),
            Item("wifi.down", "下行（含曲线）"),
            Item("wifi.up", "上行"),
            Item("wifi.link", "链路速率"),
            Item("wifi.channel", "信道"),
        ),
    ),
    Category(
        "eth",
        "有线网络",
        theme.DOWN,
        (
            Item("eth.latency", "延迟（含曲线）"),
            Item("eth.target", "测速 IP"),
            Item("eth.down", "下行"),
            Item("eth.up", "上行"),
            Item("eth.link", "链路速率"),
            Item("eth.addr", "IPv4 地址"),
        ),
    ),
    Category(
        "bt",
        "蓝牙",
        theme.BT,
        (
            Item("bt.devices", "设备列表（含电量）"),
            Item("bt.chart", "信号曲线"),
        ),
    ),
)

CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}
ALL_ITEMS = [(category, item) for category in CATEGORIES for item in category.items]


def item_label(key: str) -> str:
    for _category, item in ALL_ITEMS:
        if item.key == key:
            return item.label
    return key
