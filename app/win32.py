"""Thin ctypes layer for the Win32 bits Qt does not expose."""

from __future__ import annotations

import ctypes
import hashlib
import os
import socket
import struct
import sys
import winreg
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi.dll")

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

WM_HOTKEY = 0x0312

user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def system32(*parts: str) -> str:
    """Absolute path to a system executable, so lookup never falls back to the CWD."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root, "System32", *parts))


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WinTrustFileInfo(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pcwszFilePath", wintypes.LPCWSTR),
        ("hFile", wintypes.HANDLE),
        ("pgKnownSubject", ctypes.POINTER(_GUID)),
    ]


class _WinTrustData(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD),
        ("dwUnionChoice", wintypes.DWORD),
        ("pInfo", ctypes.c_void_p),
        ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPWSTR),
        ("dwProvFlags", wintypes.DWORD),
        ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_CACHE_ONLY_URL_RETRIEVAL = 0x1000
_WINTRUST_ACTION_GENERIC_VERIFY_V2 = _GUID(
    0x00AAC56B,
    0xCD44,
    0x11D0,
    (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
)


def verify_authenticode(path: str | Path) -> bool:
    """True when Windows accepts the file's embedded Authenticode signature."""
    try:
        wintrust = ctypes.WinDLL("wintrust.dll")
        wintrust.WinVerifyTrust.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
        ]
        wintrust.WinVerifyTrust.restype = ctypes.c_long
        file_info = _WinTrustFileInfo(
            cbStruct=ctypes.sizeof(_WinTrustFileInfo),
            pcwszFilePath=str(path),
            hFile=None,
            pgKnownSubject=None,
        )
        data = _WinTrustData()
        data.cbStruct = ctypes.sizeof(_WinTrustData)
        data.dwUIChoice = _WTD_UI_NONE
        data.fdwRevocationChecks = _WTD_REVOKE_NONE
        data.dwUnionChoice = _WTD_CHOICE_FILE
        data.pInfo = ctypes.cast(ctypes.byref(file_info), ctypes.c_void_p)
        data.dwStateAction = _WTD_STATEACTION_VERIFY
        data.dwProvFlags = _WTD_CACHE_ONLY_URL_RETRIEVAL
        action = _WINTRUST_ACTION_GENERIC_VERIFY_V2
        status = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
        data.dwStateAction = _WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
        return status == 0
    except Exception:
        return False


def set_click_through(hwnd: int, enabled: bool) -> None:
    """Make the window ignore mouse input so clicks reach the app behind it."""
    hwnd = wintypes.HWND(hwnd)
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    # Never activate: a desktop widget must not take focus from the active app.
    new_style = style | WS_EX_LAYERED | WS_EX_TOOLWINDOW
    if enabled:
        new_style |= WS_EX_TRANSPARENT
    else:
        new_style &= ~WS_EX_TRANSPARENT
    if new_style != style:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)


def set_no_activate(hwnd: int) -> None:
    hwnd = wintypes.HWND(hwnd)
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    new_style = style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if new_style != style:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "DesktopPulseWidget"


def _autostart_command() -> str:
    """Command Windows runs at logon, using the same interpreter as this app."""
    if getattr(sys, "frozen", False):
        # Packaged exe: launch the exe directly, no interpreter involved.
        return f'"{Path(sys.executable)}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if exe.name.lower() != "pythonw.exe" and pythonw.exists():
        exe = pythonw
    main = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{exe}" "{main}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
    except OSError:
        return False
    return bool(value)


def set_autostart(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
            return
        try:
            winreg.DeleteValue(key, AUTOSTART_NAME)
        except FileNotFoundError:
            pass


def set_topmost(hwnd: int, topmost: bool) -> None:
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(HWND_TOPMOST if topmost else HWND_NOTOPMOST),
        0,
        0,
        0,
        0,
        0x0001 | 0x0002,
    )


class MIB_IPFORWARDROW(ctypes.Structure):
    _fields_ = [
        ("dwForwardDest", wintypes.DWORD),
        ("dwForwardMask", wintypes.DWORD),
        ("dwForwardPolicy", wintypes.DWORD),
        ("dwForwardNextHop", wintypes.DWORD),
        ("dwForwardIfIndex", wintypes.DWORD),
        ("dwForwardType", wintypes.DWORD),
        ("dwForwardProto", wintypes.DWORD),
        ("dwForwardAge", wintypes.DWORD),
        ("dwForwardNextHopAS", wintypes.DWORD),
        ("dwForwardMetric1", wintypes.DWORD),
        ("dwForwardMetric2", wintypes.DWORD),
        ("dwForwardMetric3", wintypes.DWORD),
        ("dwForwardMetric4", wintypes.DWORD),
        ("dwForwardMetric5", wintypes.DWORD),
    ]


def default_gateway() -> str | None:
    """IPv4 next hop for the default route, without spawning a process."""
    row = MIB_IPFORWARDROW()
    dest = struct.unpack("<I", socket.inet_aton("8.8.8.8"))[0]
    try:
        rc = iphlpapi.GetBestRoute(dest, 0, ctypes.byref(row))
    except Exception:
        return None
    if rc != 0 or not row.dwForwardNextHop:
        return None
    return socket.inet_ntoa(struct.pack("<I", row.dwForwardNextHop))


def foreground_process_id() -> int | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", ctypes.c_long),
        ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


def display_refresh_rate() -> float | None:
    """True refresh rate of the current display, from the display mode."""
    mode = DEVMODEW()
    mode.dmSize = ctypes.sizeof(DEVMODEW)
    try:
        if not user32.EnumDisplaySettingsW(None, -1, ctypes.byref(mode)):
            return None
    except Exception:
        return None
    rate = float(mode.dmDisplayFrequency)
    return rate if 1 < rate < 1000 else None


def integrity_rid() -> int | None:
    """Process integrity RID: 0x1000 low, 0x2000 medium, 0x3000 high."""
    import os

    return process_integrity_rid(os.getpid())


def process_integrity_rid(pid: int) -> int | None:
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    token_query = 0x0008
    process_query_limited = 0x1000
    token_integrity_level = 25
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    process = kernel32.OpenProcess(process_query_limited, False, pid)
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(
            wintypes.HANDLE(process), token_query, ctypes.byref(token)
        ):
            return None
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_integrity_level, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, token_integrity_level, buffer, size.value, ctypes.byref(size)
        ):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not sid:
            return None
        count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
        if count < 1:
            return None
        sub = ctypes.cast(sid + 8, ctypes.POINTER(ctypes.c_uint))[count - 1]
        return int(sub)
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


# Only these schemes may reach ShellExecute; anything else (file:, javascript:,
# a bare path, an .exe, …) is refused before it can be handed to the shell.
_ALLOWED_URI_SCHEMES = ("ms-settings:", "http:", "https:")


def open_uri(uri: str) -> bool:
    if not isinstance(uri, str) or not uri:
        return False
    if any(ch in uri for ch in ('"', "\n", "\r", "\t")):
        return False
    scheme = uri.split(":", 1)[0].lower() + ":" if ":" in uri else ""
    if scheme not in _ALLOWED_URI_SCHEMES:
        return False
    try:
        se = ctypes.WinDLL("shell32.dll")
        se.ShellExecuteW.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        se.ShellExecuteW.restype = ctypes.c_void_p
        if (se.ShellExecuteW(None, "open", uri, None, None, 1) or 0) > 32:
            return True
    except Exception:
        pass
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def register_hotkey(hwnd: int, hotkey_id: int = 1) -> bool:
    MOD_CONTROL, MOD_ALT = 0x2, 0x1
    try:
        return bool(
            user32.RegisterHotKey(wintypes.HWND(hwnd), hotkey_id, MOD_CONTROL | MOD_ALT, ord("L"))
        )
    except Exception:
        return False
