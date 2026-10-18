"""Minimal PDH (Performance Data Helper) wrapper.

Used for data that psutil cannot provide on Windows, e.g. ACPI thermal zone
temperatures and GPU engine utilization perf counters.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, wintypes

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2


class _FmtValue(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [
            ("longValue", wintypes.LONG),
            ("doubleValue", ctypes.c_double),
            ("largeValue", ctypes.c_longlong),
            ("AnsiStringValue", wintypes.LPCSTR),
            ("WideStringValue", wintypes.LPCWSTR),
        ]

    _anonymous_ = ("u",)
    _fields_ = [("CStatus", wintypes.DWORD), ("u", _U)]


class _FmtItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _FmtValue)]


_pdh = ctypes.WinDLL("pdh.dll")
_pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, POINTER(wintypes.HANDLE)]
_pdh.PdhOpenQueryW.restype = wintypes.DWORD
_pdh.PdhAddEnglishCounterW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCWSTR,
    ctypes.c_size_t,
    POINTER(wintypes.HANDLE),
]
_pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
_pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
_pdh.PdhCollectQueryData.restype = wintypes.DWORD
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
_pdh.PdhGetFormattedCounterValue.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_pdh.PdhGetFormattedCounterValue.restype = wintypes.DWORD
_pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]


class PdhCounter:
    """A single PDH counter, added by its English path."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._query = wintypes.HANDLE()
        self._counter = wintypes.HANDLE()
        if _pdh.PdhOpenQueryW(None, 0, byref(self._query)) != 0:
            raise OSError(f"PdhOpenQuery failed for {path}")
        if _pdh.PdhAddEnglishCounterW(self._query, path, 0, byref(self._counter)) != 0:
            _pdh.PdhCloseQuery(self._query)
            self._query = wintypes.HANDLE()
            raise OSError(f"PdhAddEnglishCounter failed for {path}")

    def collect(self) -> bool:
        return _pdh.PdhCollectQueryData(self._query) == 0

    def array(self) -> dict[str, float]:
        """All instances of a wildcard counter: {instance_name: value}."""
        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        rc = _pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE, byref(size), byref(count), None
        )
        if rc not in (0, PDH_MORE_DATA) or size.value == 0:
            return {}
        for _ in range(2):
            buf = ctypes.create_string_buffer(size.value)
            rc = _pdh.PdhGetFormattedCounterArrayW(
                self._counter, PDH_FMT_DOUBLE, byref(size), byref(count), buf
            )
            if rc == PDH_MORE_DATA:
                continue
            if rc != 0:
                return {}
            items = ctypes.cast(buf, POINTER(_FmtItem))
            result: dict[str, float] = {}
            for i in range(count.value):
                item = items[i]
                if item.FmtValue.CStatus == 0:
                    result[item.szName or ""] = item.FmtValue.doubleValue
            return result
        return {}

    def value(self) -> float | None:
        """Formatted value of a single-instance counter."""
        raw = ctypes.create_string_buffer(ctypes.sizeof(_FmtValue))
        rc = _pdh.PdhGetFormattedCounterValue(self._counter, PDH_FMT_DOUBLE, None, raw)
        if rc != 0:
            return None
        val = ctypes.cast(raw, POINTER(_FmtValue)).contents
        return val.doubleValue if val.CStatus == 0 else None

    def close(self) -> None:
        if self._query:
            _pdh.PdhCloseQuery(self._query)
            self._query = wintypes.HANDLE()


def close_quietly(*counters: PdhCounter) -> None:
    for counter in counters:
        try:
            counter.close()
        except Exception:
            pass
