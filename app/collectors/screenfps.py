"""Frame-rate fallback that does not need ETW.

PresentMon loses almost every ETW event on some systems, so the overlay keeps a
cheap screen-change meter around: it samples a small patch of the foreground
window ~200 times per second and counts how often the pixels actually change.
For an animating app that count equals its presented frames per second.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
import zlib
from collections import deque
from ctypes import wintypes

import psutil

USER32 = ctypes.WinDLL("user32", use_last_error=True)
GDI32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

_BLOCKED_EXE = {
    "python.exe",
    "pythonw.exe",
    "explorer.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "textinputhost.exe",
    "lockapp.exe",
}
_BLOCKED_CLASS = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Windows.UI.Core.CoreWindow",
}

# Grabbing a screen region costs the same ~6 ms whatever its size (the readback
# dominates), so cover as much of the window as stays fast enough to resolve
# 60 fps: 720x400 measured ~8 ms per sample.
_MAX_W = 720
_MAX_H = 400
_INTERVAL = 0.004
_WINDOW = 1.0


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.Error, OSError):
        return ""


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    USER32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


class ScreenFps:
    """Measures how often the foreground window repaints."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._changes: deque[float] = deque()
        self._hwnd = 0
        self._app: str | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._ready = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="screenfps", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ---------------------------------------------------------------- helpers
    def _pick_window(self) -> tuple[int, str]:
        hwnd = USER32.GetForegroundWindow()
        if not hwnd:
            return 0, ""
        pid = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        name = _process_name(pid.value)
        if pid.value == os.getpid() or name in _BLOCKED_EXE:
            return 0, name
        if _window_class(hwnd) in _BLOCKED_CLASS:
            return 0, name
        return hwnd, name

    def _setup(self, hwnd: int) -> None:
        rect = wintypes.RECT()
        USER32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        width, height = rect.right, rect.bottom
        if width < 8 or height < 8:
            self._ready = False
            return
        grab_w = min(_MAX_W, width)
        grab_h = min(_MAX_H, height)
        origin = wintypes.POINT(0, 0)
        USER32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin))
        self._x = origin.x + (width - grab_w) // 2
        self._y = origin.y + (height - grab_h) // 2
        self._w = grab_w
        self._h = grab_h
        self._ready = True

    def _loop(self) -> None:
        screen = USER32.GetDC(None)
        mem = GDI32.CreateCompatibleDC(screen)
        bmp = 0
        buf = None
        prev = None
        size = (0, 0)
        while self._running:
            cycle = time.monotonic()
            hwnd, name = self._pick_window()
            if not hwnd:
                if self._hwnd:
                    # Foreground is our own window or the shell: keep last app.
                    hwnd = self._hwnd
                else:
                    time.sleep(0.05)
                    continue
            if hwnd != self._hwnd or not self._ready:
                self._hwnd = hwnd
                self._app = name
                self._setup(hwnd)
                prev = None
                with self._lock:
                    self._changes.clear()
                if self._ready and (self._w, self._h) != size:
                    if bmp:
                        GDI32.DeleteObject(wintypes.HGDIOBJ(bmp))
                    bmp = GDI32.CreateCompatibleBitmap(screen, self._w, self._h)
                    GDI32.SelectObject(wintypes.HDC(mem), wintypes.HGDIOBJ(bmp))
                    buf = ctypes.create_string_buffer(self._w * self._h * 4)
                    size = (self._w, self._h)
            if not self._ready or not bmp or buf is None:
                time.sleep(0.05)
                continue

            ok = GDI32.BitBlt(
                wintypes.HDC(mem),
                0,
                0,
                self._w,
                self._h,
                wintypes.HDC(screen),
                self._x,
                self._y,
                SRCCOPY,
            )
            if ok and GDI32.GetBitmapBits(wintypes.HGDIOBJ(bmp), len(buf), buf):
                digest = zlib.crc32(buf.raw)
                now = time.monotonic()
                if prev is not None and digest != prev:
                    with self._lock:
                        self._changes.append(now)
                prev = digest
            spent = time.monotonic() - cycle
            if spent < _INTERVAL:
                time.sleep(_INTERVAL - spent)

        if bmp:
            GDI32.DeleteObject(wintypes.HGDIOBJ(bmp))
        GDI32.DeleteDC(wintypes.HDC(mem))
        USER32.ReleaseDC(None, wintypes.HDC(screen))

    # ------------------------------------------------------------------ query
    def read(self) -> dict:
        now = time.monotonic()
        with self._lock:
            while self._changes and now - self._changes[0] > _WINDOW * 3:
                self._changes.popleft()
            recent = [t for t in self._changes if now - t <= _WINDOW]
        app = self._app
        if not self._hwnd:
            return {"fps": None, "app": None, "source": "screen", "detail": "无前台窗口"}
        if not recent:
            return {"fps": 0.0, "app": app, "source": "screen", "detail": "无出帧"}
        return {"fps": float(len(recent)) / _WINDOW, "app": app, "source": "screen", "detail": None}
