"""Bluetooth device battery level and signal strength via WinRT.

Classic and BLE devices live in different AQS namespaces, so both paired and
connected selectors are merged. Connected BLE devices expose their battery
through the GATT battery service. Signal strength comes from live BLE
advertisements (a real measurement); the cached AEP value is never shown.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

try:
    from winrt._winrt import unbox_boolean, unbox_int32, unbox_string, unbox_uint32
    from winrt.windows.devices.bluetooth import (
        BluetoothCacheMode,
        BluetoothDevice,
        BluetoothLEDevice,
    )
    from winrt.windows.devices.enumeration import DeviceInformation

    WINRT_OK = True
except Exception:  # pragma: no cover - optional dependency
    WINRT_OK = False

_EXTRA = [
    "System.Devices.Aep.SignalStrength",
    "System.Devices.Aep.IsConnected",
    "System.Devices.Aep.IsPresent",
    "System.Devices.Aep.IsPaired",
    "System.Devices.Aep.DeviceAddress",
    "System.Devices.BatteryLife",
]

_BATTERY_SERVICE = uuid.UUID("0000180f-0000-1000-8000-00805f9b34fb")
_BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"
_BATTERY_TTL = 180.0
_RSSI_TTL = 12.0
_RSSI_MAX_ENTRIES = 4096
_SCAN_TICK = 0.4


def _unbox(value):
    for convert in (unbox_int32, unbox_uint32, unbox_string, unbox_boolean):
        try:
            return convert(value)
        except Exception:
            continue
    return None


class _BleRssiWatcher(threading.Thread):
    """Collect live signal strength from BLE advertisements.

    Windows only exposes per-connection RSSI through advertisements; the
    cached `System.Devices.Aep.SignalStrength` property does not refresh, so it
    is deliberately not used for display.
    """

    def __init__(self) -> None:
        super().__init__(name="ble-rssi", daemon=True)
        self._samples: dict[int, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.error: str | None = None
        self.ready = threading.Event()

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.ready.set()

    async def _run(self) -> None:
        from winrt._winrt import MTA, init_apartment
        from winrt.windows.devices.bluetooth.advertisement import (
            BluetoothLEAdvertisementWatcher,
            BluetoothLEScanningMode,
        )

        init_apartment(MTA)
        watcher = BluetoothLEAdvertisementWatcher()
        watcher.scanning_mode = BluetoothLEScanningMode.ACTIVE

        def received(sender, args) -> None:
            address = int(args.bluetooth_address) & 0xFFFFFFFFFFFF
            now = time.monotonic()
            with self._lock:
                if len(self._samples) >= _RSSI_MAX_ENTRIES:
                    # BLE random addresses rotate constantly; drop stale rows
                    # so a dense radio environment cannot grow this unbounded.
                    cutoff = now - _RSSI_TTL
                    for key in [
                        k for k, (_rssi, seen) in self._samples.items() if seen < cutoff
                    ]:
                        del self._samples[key]
                    if len(self._samples) >= _RSSI_MAX_ENTRIES:
                        self._samples.clear()
                self._samples[address] = (
                    int(args.raw_signal_strength_in_dbm),
                    now,
                )

        watcher.add_received(received)
        watcher.start()
        self.ready.set()
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(_SCAN_TICK)
        finally:
            try:
                watcher.stop()
                watcher.remove_received(received)
            except Exception:
                pass

    def sample(self, address: str | None) -> int | None:
        if not address:
            return None
        try:
            key = int(address.replace(":", ""), 16)
        except (AttributeError, ValueError):
            return None
        with self._lock:
            hit = self._samples.get(key)
        if hit is None:
            return None
        rssi, seen = hit
        if time.monotonic() - seen > _RSSI_TTL:
            return None
        return rssi

    def stop(self) -> None:
        self._stopping.set()


class BluetoothCollector:
    def __init__(self) -> None:
        self.available = WINRT_OK
        self.error: str | None = None if WINRT_OK else "缺少 winrt 运行库"
        self._battery_cache: dict[str, tuple[int, float]] = {}
        self._local = threading.local()
        self._watcher: _BleRssiWatcher | None = None

    def _ensure_watcher(self) -> None:
        if self._watcher is None and WINRT_OK:
            self._watcher = _BleRssiWatcher()
            self._watcher.start()

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _ensure_apartment(self) -> None:
        if getattr(self._local, "ready", False):
            return
        try:
            from winrt._winrt import MTA, init_apartment

            init_apartment(MTA)
        except Exception:
            pass
        self._local.ready = True

    def read(self) -> list[dict]:
        if not WINRT_OK:
            return []
        self._ensure_watcher()
        self._ensure_apartment()
        try:
            return asyncio.run(self._read_async())
        except Exception as exc:  # keep the widget alive on transient radio errors
            self.error = f"{type(exc).__name__}: {exc}"
            return []

    @staticmethod
    def _selectors() -> list[str]:
        selectors: list[str] = []
        for factory in (BluetoothDevice, BluetoothLEDevice):
            for build in (
                lambda f=factory: f.get_device_selector_from_pairing_state(True),
                lambda f=factory: f.get_device_selector_from_connection_status(True),
            ):
                try:
                    selector = build()
                except Exception:
                    continue
                if selector not in selectors:
                    selectors.append(selector)
        return selectors

    async def _read_async(self) -> list[dict]:
        devices: dict[str, dict] = {}
        for selector in self._selectors():
            try:
                found = await DeviceInformation.find_all_async_aqs_filter_and_additional_properties(
                    selector, _EXTRA
                )
            except Exception:
                continue
            for info in found:
                props = info.properties
                entry = devices.setdefault(
                    info.id,
                    {
                        "id": info.id,
                        "name": (info.name or "").strip(),
                        "connected": bool(info.is_enabled),
                        "paired": False,
                        "rssi": None,
                        "rssi_cached": None,
                        "battery": None,
                        "address": None,
                    },
                )
                if not entry["paired"]:
                    try:
                        entry["paired"] = bool(_unbox(props["System.Devices.Aep.IsPaired"]))
                    except Exception:
                        pass
                if not entry["name"]:
                    entry["name"] = (info.name or "").strip()
                try:
                    connected = _unbox(props["System.Devices.Aep.IsConnected"])
                    if isinstance(connected, bool):
                        entry["connected"] = connected
                except Exception:
                    pass
                try:
                    rssi = _unbox(props["System.Devices.Aep.SignalStrength"])
                    if isinstance(rssi, int) and -120 < rssi < 0:
                        entry["rssi_cached"] = rssi
                except Exception:
                    pass
                try:
                    address = _unbox(props["System.Devices.Aep.DeviceAddress"])
                    if isinstance(address, str) and address:
                        entry["address"] = address
                except Exception:
                    pass
                try:
                    battery = _unbox(props["System.Devices.BatteryLife"])
                    if isinstance(battery, int) and 0 <= battery <= 100:
                        entry["battery"] = battery
                except Exception:
                    pass
                if not entry["address"]:
                    tail = info.id.split("-")[-1]
                    if tail.count(":") == 5:
                        entry["address"] = tail

        result = [d for d in devices.values() if d["name"] and d["connected"]]
        for device in result:
            # Prefer a fresh advertisement; connected devices often stop
            # advertising, so fall back to the system association cache.
            live = self._watcher.sample(device.get("address")) if self._watcher else None
            device["rssi_live"] = live is not None
            device["rssi"] = live if live is not None else device.get("rssi_cached")
            if device["battery"] is None:
                device["battery"] = await self._gatt_battery(device)
        result.sort(key=lambda d: d["name"].lower())
        if result and not self.available:
            self.available = True
            self.error = None
        return result

    async def _gatt_battery(self, device: dict) -> int | None:
        cached = self._battery_cache.get(device["id"])
        now = time.monotonic()
        if cached and now - cached[1] < _BATTERY_TTL:
            return cached[0]
        address = device.get("address") or ""
        if address.count(":") != 5:
            return cached[0] if cached else None
        try:
            address_int = int(address.replace(":", ""), 16)
            le_device = await asyncio.wait_for(
                BluetoothLEDevice.from_bluetooth_address_async(address_int), timeout=4
            )
            if le_device is None:
                return cached[0] if cached else None
            services = await asyncio.wait_for(
                le_device.get_gatt_services_for_uuid_with_cache_mode_async(
                    _BATTERY_SERVICE, BluetoothCacheMode.UNCACHED
                ),
                timeout=5,
            )
            value: int | None = None
            for service in services.services:
                try:
                    chars = await asyncio.wait_for(
                        service.get_characteristics_with_cache_mode_async(BluetoothCacheMode.UNCACHED),
                        timeout=4,
                    )
                    for char in chars.characteristics:
                        if str(char.uuid).lower() != _BATTERY_CHAR:
                            continue
                        result = await asyncio.wait_for(char.read_value_async(), timeout=4)
                        data = bytes(result.value)
                        if data:
                            value = data[0]
                        break
                finally:
                    try:
                        service.close()
                    except Exception:
                        pass
                if value is not None:
                    break
            if value is not None:
                self._battery_cache[device["id"]] = (value, now)
            return value
        except Exception:
            return cached[0] if cached else None
