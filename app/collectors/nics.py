"""Per-adapter network info: separates Wi-Fi from wired Ethernet.

Windows adapter names are localized, so the adapter *type* is read from
IP Helper (IfType) instead of guessing from the display name.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, byref, wintypes
from dataclasses import dataclass

import psutil

IF_TYPE_ETHERNET = 6
IF_TYPE_IEEE80211 = 71
IF_TYPE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
IF_OPER_STATUS_UP = 1

ERROR_BUFFER_OVERFLOW = 111
_MAX_TRIES = 3
_AF_INET = 2
_GAA_SKIP_ANYCAST = 0x0002
_GAA_SKIP_MULTICAST = 0x0004
_GAA_SKIP_DNS_SERVER = 0x0008

_iphlpapi = ctypes.WinDLL("iphlpapi.dll")


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG),
    ("IfIndex", wintypes.DWORD),
    ("Next", POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.c_void_p),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG),
    ("Mtu", wintypes.ULONG),
    ("IfType", wintypes.ULONG),
    ("OperStatus", wintypes.ULONG),
]


def enumerate_adapters() -> list[dict]:
    """Adapters reported by IP Helper: friendly name, description, type, state."""
    _iphlpapi.GetAdaptersAddresses.argtypes = [
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        POINTER(_IP_ADAPTER_ADDRESSES),
        POINTER(wintypes.ULONG),
    ]
    _iphlpapi.GetAdaptersAddresses.restype = wintypes.ULONG

    size = wintypes.ULONG(16 * 1024)
    flags = _GAA_SKIP_ANYCAST | _GAA_SKIP_MULTICAST | _GAA_SKIP_DNS_SERVER
    for _ in range(_MAX_TRIES):
        buffer = ctypes.create_string_buffer(size.value)
        ptr = ctypes.cast(buffer, POINTER(_IP_ADAPTER_ADDRESSES))
        rc = _iphlpapi.GetAdaptersAddresses(_AF_INET, flags, None, ptr, byref(size))
        if rc == ERROR_BUFFER_OVERFLOW:
            continue
        if rc != 0:
            return []
        result: list[dict] = []
        node = ptr
        while node:
            item = node.contents
            result.append(
                {
                    "name": item.FriendlyName or "",
                    "description": item.Description or "",
                    "if_type": int(item.IfType),
                    "up": int(item.OperStatus) == IF_OPER_STATUS_UP,
                }
            )
            node = item.Next
        return result
    return []


@dataclass
class NicRate:
    name: str
    kind: str
    up: bool
    description: str = ""
    speed_mbps: float | None = None
    ipv4: str | None = None
    rx_bps: float = 0.0
    tx_bps: float = 0.0


class NicCollector:
    """Tracks throughput for each physical adapter, split by Wi-Fi / Ethernet."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[int, int, float]] = {}
        self._nic: dict = {}
        self._nic_at = 0.0
        self._links: list[dict] | None = None

    def _classify(self, adapter: dict) -> str:
        if_type = adapter.get("if_type")
        if if_type == IF_TYPE_IEEE80211:
            return "wifi"
        if if_type == IF_TYPE_ETHERNET:
            return "ethernet"
        name = (adapter.get("name") or "").lower()
        if any(token in name for token in ("wi-fi", "wifi", "wlan", "无线")):
            return "wifi"
        if any(token in name for token in ("ethernet", "以太网")):
            return "ethernet"
        return "other"

    def _refresh_meta(self) -> None:
        now = time.monotonic()
        if self._links is not None and now - self._nic_at < 10.0:
            return
        self._nic_at = now
        self._links = enumerate_adapters()

    def read(self) -> list[dict]:
        self._refresh_meta()
        adapters = self._links or []
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        counters = psutil.net_io_counters(pernic=True)
        now = time.monotonic()

        result: list[dict] = []
        for adapter in adapters:
            name = adapter["name"]
            description = adapter.get("description", "")
            kind = self._classify(adapter)
            if kind == "other":
                continue
            if adapter.get("if_type") in (IF_TYPE_LOOPBACK, IF_TYPE_TUNNEL):
                continue
            lowered = description.lower()
            if any(
                token in lowered
                for token in ("wi-fi direct", "bluetooth", "virtual", "loopback", "tap-", "miniport")
            ):
                continue
            if name not in counters:
                continue
            info = stats.get(name)
            is_up = bool(info.isup) if info else adapter["up"]
            if not is_up:
                continue
            entry = counters[name]
            prev = self._last.get(name)
            self._last[name] = (entry.bytes_recv, entry.bytes_sent, now)
            rx = tx = 0.0
            if prev is not None:
                elapsed = max(now - prev[2], 1e-3)
                rx = max(entry.bytes_recv - prev[0], 0) / elapsed
                tx = max(entry.bytes_sent - prev[1], 0) / elapsed
            ipv4 = next(
                (
                    a.address
                    for a in addrs.get(name, [])
                    if getattr(a.family, "name", "") == "AF_INET" and not a.address.startswith("169.254.")
                ),
                None,
            )
            result.append(
                {
                    "name": name,
                    "description": description,
                    "kind": kind,
                    "up": is_up,
                    "speed_mbps": float(info.speed) if info and info.speed > 0 else None,
                    "ipv4": ipv4,
                    "rx_bps": rx,
                    "tx_bps": tx,
                }
            )
        result.sort(key=lambda item: (item["kind"] != "wifi", item["name"]))
        return result
