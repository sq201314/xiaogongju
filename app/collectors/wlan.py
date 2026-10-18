"""WLAN link details through the native WlanAPI.

`netsh` only reports a percentage; WlanAPI exposes the real RSSI, negotiated
link rates and channel. Windows requires location permission for these
queries, so callers must handle the access-denied case.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, wintypes
from dataclasses import dataclass

_wlan = ctypes.WinDLL("wlanapi.dll")

_wlan.WlanOpenHandle.argtypes = [
    wintypes.DWORD,
    ctypes.c_void_p,
    POINTER(wintypes.DWORD),
    POINTER(wintypes.HANDLE),
]
_wlan.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_wlan.WlanEnumInterfaces.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    POINTER(ctypes.c_void_p),
]
_wlan.WlanQueryInterface.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_void_p,
    POINTER(wintypes.DWORD),
    POINTER(ctypes.c_void_p),
    POINTER(ctypes.c_int),
]
_wlan.WlanFreeMemory.argtypes = [ctypes.c_void_p]

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
WLAN_INTERFACE_STATE_CONNECTED = 1
OPCODE_CURRENT_CONNECTION = 7
OPCODE_CHANNEL_NUMBER = 8
OPCODE_RSSI = 0x10000101  # wlan_intf_opcode_rssi (MSM range)

_PHY_NAMES = {
    0: "",
    1: "FHSS",
    2: "DSSS",
    3: "IR",
    4: "OFDM",
    5: "HR-DSSS",
    6: "ERP",
    7: "802.11n",
    8: "802.11ac",
    9: "802.11ad",
    10: "802.11ax",
    11: "802.11be",
}


@dataclass
class WlanLink:
    available: bool = False
    permission: bool = False
    connected: bool = False
    ssid: str = ""
    signal_pct: int | None = None
    rssi: int | None = None
    rx_mbps: float | None = None
    tx_mbps: float | None = None
    channel: int | None = None
    phy: str = ""
    description: str = ""


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WlanInterfaceInfo(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", _GUID),
        ("strInterfaceDescription", wintypes.WCHAR * 256),
        ("isState", ctypes.c_uint),
    ]


class _WlanInterfaceInfoList(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("InterfaceInfo", _WlanInterfaceInfo * 1),
    ]


class _Dot11Ssid(ctypes.Structure):
    _fields_ = [("uSSIDLength", ctypes.c_ulong), ("ucSSID", ctypes.c_ubyte * 32)]


class _WlanAssociationAttributes(ctypes.Structure):
    _fields_ = [
        ("dot11Ssid", _Dot11Ssid),
        ("dot11BssType", ctypes.c_int),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11PhyType", ctypes.c_int),
        ("uDot11PhyIndex", ctypes.c_ulong),
        ("wlanSignalQuality", ctypes.c_ulong),
        ("ulRxRate", ctypes.c_ulong),
        ("ulTxRate", ctypes.c_ulong),
    ]


class _WlanSecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("dot11AuthAlgorithm", ctypes.c_int),
        ("dot11CipherAlgorithm", ctypes.c_int),
        ("bSecurityEnabled", wintypes.BOOL),
    ]


class _WlanConnectionAttributes(ctypes.Structure):
    _fields_ = [
        ("isState", ctypes.c_int),
        ("wlanConnectionMode", ctypes.c_int),
        ("strProfileName", wintypes.WCHAR * 256),
        ("wlanAssociationAttributes", _WlanAssociationAttributes),
        ("wlanSecurityAttributes", _WlanSecurityAttributes),
    ]


def _open_handle() -> tuple[int, int]:
    handle = wintypes.HANDLE()
    negotiated = wintypes.DWORD()
    rc = _wlan.WlanOpenHandle(2, None, byref(negotiated), byref(handle))
    return rc, handle.value


def _query(handle: int, guid_ptr, opcode: int) -> tuple[int, ctypes.c_void_p, int]:
    data = ctypes.c_void_p()
    size = wintypes.DWORD()
    kind = ctypes.c_int()
    rc = _wlan.WlanQueryInterface(
        wintypes.HANDLE(handle),
        guid_ptr,
        opcode,
        None,
        byref(size),
        byref(data),
        byref(kind),
    )
    return rc, data, size.value


def read_link(netsh_ok: bool = False) -> WlanLink:
    """WlanAPI link state; when netsh already has the details, only channel is read."""
    link = WlanLink()
    rc, handle = _open_handle()
    if rc != ERROR_SUCCESS or not handle:
        return link
    link.available = True
    plist = ctypes.c_void_p()
    try:
        if _wlan.WlanEnumInterfaces(wintypes.HANDLE(handle), None, byref(plist)) != ERROR_SUCCESS:
            return link
        info_list = ctypes.cast(plist, POINTER(_WlanInterfaceInfoList)).contents
        for index in range(info_list.dwNumberOfItems):
            info = info_list.InterfaceInfo[index]
            link.description = info.strInterfaceDescription
            guid_ptr = ctypes.cast(ctypes.pointer(info.InterfaceGuid), ctypes.c_void_p)

            # Channel is readable even without location permission.
            rc, data, size = _query(handle, guid_ptr, OPCODE_CHANNEL_NUMBER)
            if rc == ERROR_SUCCESS and data and size >= 4:
                link.channel = ctypes.cast(data, POINTER(ctypes.c_ulong)).contents.value
                _wlan.WlanFreeMemory(data)

            if netsh_ok:
                link.permission = True
                return link

            rc, data, size = _query(handle, guid_ptr, OPCODE_CURRENT_CONNECTION)
            if rc == ERROR_ACCESS_DENIED:
                return link
            if rc != ERROR_SUCCESS or not data:
                continue
            try:
                if size < ctypes.sizeof(_WlanConnectionAttributes):
                    continue
                link.permission = True
                attrs = ctypes.cast(data, POINTER(_WlanConnectionAttributes)).contents
                assoc = attrs.wlanAssociationAttributes
                quality = int(assoc.wlanSignalQuality)
                if not 0 <= quality <= 100:
                    continue
                ssid_len = min(int(assoc.dot11Ssid.uSSIDLength), 32)
                ssid = bytes(assoc.dot11Ssid.ucSSID[:ssid_len]).decode("utf-8", "ignore")
                link.connected = attrs.isState == 1 and bool(ssid)
                link.ssid = ssid
                link.signal_pct = quality
                if assoc.ulRxRate:
                    link.rx_mbps = assoc.ulRxRate / 1000.0
                if assoc.ulTxRate:
                    link.tx_mbps = assoc.ulTxRate / 1000.0
                link.phy = _PHY_NAMES.get(int(assoc.dot11PhyType), "")
            finally:
                _wlan.WlanFreeMemory(data)
            if not link.connected:
                continue
            rc, data, size = _query(handle, guid_ptr, OPCODE_RSSI)
            if rc == ERROR_SUCCESS and data and size == 4:
                value = ctypes.cast(data, POINTER(ctypes.c_long)).contents.value
                if -120 < value < 0:
                    link.rssi = value
                _wlan.WlanFreeMemory(data)
            return link
        return link
    finally:
        if plist:
            _wlan.WlanFreeMemory(plist)
        _wlan.WlanCloseHandle(wintypes.HANDLE(handle), None)
