"""Wi-Fi link details plus per-adapter internet latency.

Each adapter gets its own latency probe whose socket is bound to that
adapter's IPv4 address, so Wi-Fi and Ethernet latencies stay separate.
"""

from __future__ import annotations

import re
import socket
import statistics
import subprocess
import threading
import time

from ..win32 import system32
from .wlan import read_link

_KEY_ALIASES = {
    "signal": "signal",
    "信号": "signal",
    "rssi": "rssi",
    "state": "state",
    "状态": "state",
    "ssid": "ssid",
    "name": "name",
    "名称": "name",
    "channel": "channel",
    "信道": "channel",
}
_PREFIX_ALIASES = {
    "receivrate": "rx_rate",
    "receiverate": "rx_rate",
    "接收速率": "rx_rate",
    "transmitrate": "tx_rate",
    "传输速率": "tx_rate",
    "radiotype": "phy",
    "无线电类型": "phy",
    "radiostatus": "radio",
    "无线电状态": "radio",
}


def _norm_key(key: str) -> str:
    return key.replace(" ", "").replace("\t", "").lower()


def decode_console(data: bytes) -> str:
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "ignore")


def run_text(args: list[str], timeout: float = 4.0) -> str:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return decode_console(proc.stdout)


def parse_netsh(text: str) -> dict:
    """Pick the connected interface block out of `netsh wlan show interfaces`."""
    info: dict = {}
    for block in text.split("\n\n"):
        entry: dict = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            norm = _norm_key(key)
            if norm in ("bssid", "apbssid", "物理地址", "physicaladdress"):
                continue
            field = _KEY_ALIASES.get(norm)
            if field is None:
                for prefix, name in _PREFIX_ALIASES.items():
                    if norm.startswith(prefix):
                        field = name
                        break
            if field and field not in entry:
                entry[field] = value.strip()
        if entry.get("ssid") and entry.get("name"):
            info = entry
            if entry.get("state", "").lower() in ("connected", "已连接"):
                break
    return info


class LatencyProbe(threading.Thread):
    """Internet RTT from one adapter, measured with a TCP handshake."""

    _CANDIDATES = (
        ("223.5.5.5", 443),      # AliDNS
        ("119.29.29.29", 443),   # DNSPod
        ("1.1.1.1", 443),        # Cloudflare
        ("8.8.8.8", 443),        # Google
    )

    def __init__(self, source_ip: str, interval: float = 2.0) -> None:
        super().__init__(daemon=True, name=f"latency-{source_ip}")
        self.source_ip = source_ip
        self.interval = interval
        self.latency: float | None = None
        self.target: str | None = None
        self._stop = threading.Event()
        self._preferred = 0
        self._samples: list[float] = []

    def stop(self) -> None:
        self._stop.set()

    def _connect_ms(self, host: str, port: int) -> float | None:
        start = time.perf_counter()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                sock.bind((self.source_ip, 0))
            except OSError:
                pass
            sock.settimeout(1.2)
            sock.connect((host, port))
        except ConnectionRefusedError:
            pass  # RST still measures a full round trip.
        except OSError:
            return None
        finally:
            sock.close()
        return (time.perf_counter() - start) * 1000.0

    def run(self) -> None:
        while not self._stop.is_set():
            count = len(self._CANDIDATES)
            order = [
                self._CANDIDATES[(self._preferred + offset) % count]
                for offset in range(count)
            ]
            for host, port in order:
                value = self._connect_ms(host, port)
                if value is None:
                    continue
                self._samples.append(value)
                del self._samples[:-3]
                self.latency = statistics.median(self._samples)
                self.target = f"{host}:{port}"
                self._preferred = self._CANDIDATES.index((host, port))
                break
            else:
                self._samples.clear()
                self.latency = None
                self.target = "外网"
            self._stop.wait(self.interval)


class LatencyMonitor:
    """Keeps one probe alive per adapter that currently has an IPv4 address."""

    def __init__(self) -> None:
        self._probes: dict[str, LatencyProbe] = {}

    def update(self, nics: list[dict]) -> None:
        wanted = {
            nic["name"]: nic["ipv4"]
            for nic in nics
            if nic.get("ipv4") and nic.get("up")
        }
        for name, ip in wanted.items():
            probe = self._probes.get(name)
            if probe is not None and probe.source_ip == ip and probe.is_alive():
                continue
            if probe is not None:
                probe.stop()
            probe = LatencyProbe(ip)
            probe.start()
            self._probes[name] = probe
        for name in list(self._probes):
            if name not in wanted:
                self._probes.pop(name).stop()

    def attach(self, nics: list[dict]) -> None:
        """Copy each adapter's latency onto its nic entry."""
        for nic in nics:
            probe = self._probes.get(nic["name"])
            nic["latency"] = probe.latency if probe else None
            nic["latency_target"] = probe.target if probe else None

    def stop(self) -> None:
        for probe in self._probes.values():
            probe.stop()
        self._probes.clear()


class NetworkCollector:
    def __init__(self) -> None:
        self._netsh: dict = {}
        self._netsh_at = 0.0

    def stop(self) -> None:
        pass

    def _netsh_interfaces(self) -> dict:
        now = time.monotonic()
        if now - self._netsh_at < 5.0:
            return self._netsh
        self._netsh_at = now
        self._netsh = parse_netsh(
            run_text([system32("netsh.exe"), "wlan", "show", "interfaces"])
        )
        return self._netsh

    @staticmethod
    def _rate(text: str | None) -> float | None:
        if not text:
            return None
        match = re.search(r"(\d+(?:[.,]\d+)?)", text)
        return float(match.group(1).replace(",", ".")) if match else None

    def read(self, nics: list[dict] | None = None) -> dict:
        netsh = self._netsh_interfaces()
        link = read_link(netsh_ok=bool(netsh.get("ssid")))
        wifi_nic = next((n for n in (nics or []) if n["kind"] == "wifi"), None)

        signal_pct = link.signal_pct
        if signal_pct is None and netsh.get("signal"):
            match = re.search(r"(\d+)", netsh["signal"])
            if match:
                signal_pct = int(match.group(1))
        rssi = link.rssi
        if rssi is None and netsh.get("rssi"):
            match = re.search(r"(-?\d+)", netsh["rssi"])
            if match and -120 < int(match.group(1)) < 0:
                rssi = int(match.group(1))
        estimated = rssi is None
        if rssi is None and signal_pct is not None:
            rssi = signal_pct / 2.0 - 100.0

        connected = link.connected or bool(wifi_nic and wifi_nic.get("up"))
        if link.permission or netsh.get("ssid"):
            status = "ok" if connected else "disconnected"
        elif connected:
            status = "permission"
        else:
            status = "disconnected"

        ssid = link.ssid or netsh.get("ssid") or ""
        channel = link.channel
        if channel is None and netsh.get("channel"):
            channel = netsh["channel"]

        rx_rate = link.rx_mbps if link.rx_mbps is not None else self._rate(netsh.get("rx_rate"))
        tx_rate = link.tx_mbps if link.tx_mbps is not None else self._rate(netsh.get("tx_rate"))
        if rx_rate is None and wifi_nic and wifi_nic.get("speed_mbps"):
            rx_rate = tx_rate = wifi_nic["speed_mbps"]

        return {
            "status": status,
            "ssid": ssid,
            "connected": connected,
            "signal_pct": signal_pct,
            "rssi": rssi,
            "rssi_estimated": estimated,
            "rx_rate": rx_rate,
            "tx_rate": tx_rate,
            "phy": link.phy or netsh.get("phy") or "",
            "latency": (wifi_nic or {}).get("latency"),
            "latency_target": (wifi_nic or {}).get("latency_target"),
            "channel": channel,
            "nic": (wifi_nic or {}).get("name"),
            "ipv4": (wifi_nic or {}).get("ipv4"),
            "rx_bps": (wifi_nic or {}).get("rx_bps", 0.0),
            "tx_bps": (wifi_nic or {}).get("tx_bps", 0.0),
        }
