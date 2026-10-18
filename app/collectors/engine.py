"""Background data collection loop."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QThread, Signal

from ..config import BUNDLE
from .bluetooth import BluetoothCollector
from .fps import FpsCollector
from .network import LatencyMonitor, NetworkCollector
from .nics import NicCollector
from .system import SystemCollector


@dataclass
class Snapshot:
    ts: float = 0.0
    cpu: dict = field(default_factory=dict)
    gpu: dict = field(default_factory=dict)
    wifi: dict = field(default_factory=dict)
    bluetooth: list = field(default_factory=list)
    fps: dict = field(default_factory=dict)
    nics: list = field(default_factory=list)
    bt_error: str | None = None


class CollectorEngine(QThread):
    snapshot = Signal(object)

    def __init__(self, interval: float = 1.0, bt_interval: float = 3.0) -> None:
        super().__init__()
        self.interval = interval
        self.bt_interval = bt_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        system = SystemCollector()
        network = NetworkCollector()
        nics = NicCollector()
        latency = LatencyMonitor()
        bluetooth = BluetoothCollector()
        fps = FpsCollector(BUNDLE / "tools" / "PresentMon.exe")
        fps.start()
        devices: list[dict] = []
        next_bt = 0.0
        first_pass = True
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                snap = Snapshot(ts=time.time())
                try:
                    snap.cpu = system.cpu()
                except Exception as exc:
                    snap.cpu = {"name": getattr(system, "cpu_name", "CPU"), "error": str(exc)}
                try:
                    snap.gpu = system.gpu()
                except Exception as exc:
                    snap.gpu = {"name": "GPU", "error": str(exc)}
                try:
                    snap.nics = nics.read()
                    latency.update(snap.nics)
                    latency.attach(snap.nics)
                except Exception:
                    snap.nics = []
                try:
                    snap.wifi = network.read(snap.nics)
                except Exception as exc:
                    snap.wifi = {"ssid": "读取失败", "error": str(exc)}
                try:
                    snap.fps = fps.read()
                except Exception as exc:
                    snap.fps = {"fps": None, "source": "refresh", "detail": str(exc)}
                if started >= next_bt:
                    if first_pass:
                        # Keep the very first paint fast; enumerate radios next round.
                        next_bt = started + 0.4
                        first_pass = False
                    else:
                        next_bt = started + self.bt_interval
                        try:
                            devices = bluetooth.read()
                        except Exception:
                            devices = []
                        snap.bt_error = bluetooth.error
                snap.bluetooth = devices
                self.snapshot.emit(snap)
                elapsed = time.monotonic() - started
                self._stop.wait(max(0.05, self.interval - elapsed))
        finally:
            fps.stop()
            network.stop()
            latency.stop()
            bluetooth.stop()
            system.close()
