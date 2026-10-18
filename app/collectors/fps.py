"""Frame rate measurement.

Real per-application frame rates come from Intel's PresentMon CLI, which needs
an elevated ETW session. Without elevation the widget falls back to the display
refresh rate and says so explicitly.
"""

from __future__ import annotations

import collections
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import byref, wintypes
from pathlib import Path

import psutil

from ..win32 import file_sha256, foreground_process_id, is_admin, verify_authenticode
from .screenfps import ScreenFps

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kill_on_close_job():
    """Job object that kills its children when this process exits."""
    try:
        kernel32 = ctypes.WinDLL("kernel32.dll")
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            wintypes.HANDLE(handle),
            JobObjectExtendedLimitInformation,
            byref(info),
            ctypes.sizeof(info),
        )
        return handle
    except Exception:
        return None

_BLOCKLIST = {
    "dwm.exe",
    "explorer.exe",
    "python.exe",
    "pythonw.exe",
    "textinputhost.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "searchhost.exe",
    "applicationframehost.exe",
    "presentmon.exe",
    "lockapp.exe",
}

# Windows shell processes: not "the app the user is looking at".
_SHELL = {"explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe"}

# Frames are averaged over this window; PresentMon flushes stdout in bursts.
_FPS_WINDOW_MS = 2000.0
_KEEP_MS = 6000.0
# PresentMon can take a while to attach its ETW consumer on this platform.
_STARTUP_GRACE = 45.0
_STALL_SECONDS = 25.0
_RESTART_INTERVAL = 60.0
# After this many fruitless restarts PresentMon is given up on.
_MAX_FAILURES = 2

# SHA-256 of the bundled Intel-signed PresentMon.exe; update it when replacing the tool.
_PRESENTMON_SHA256 = "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"


def _foreground_process_name() -> str | None:
    pid = foreground_process_id()
    if not pid:
        return None
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.Error, OSError):
        return None


class FpsCollector:
    def __init__(self, exe: Path, session_name: str = "DesktopPulseFps") -> None:
        self.exe = exe
        self.session_name = session_name
        self.frames: collections.deque[tuple[float, str]] = collections.deque()
        self.running = False
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._job = None
        self._lock = threading.Lock()
        self._own = {os.path.basename(sys.executable).lower()}
        self._gen = 0
        self._last_line = 0.0
        self._started_at = 0.0
        self._last_restart = 0.0
        self._failures = 0
        self._disabled = False
        self._screen = ScreenFps()

    def start(self) -> None:
        self._screen.start()
        if not self.exe.exists():
            self.error = "未找到 PresentMon"
            return
        if not self._trusted_binary():
            self.error = "PresentMon 校验失败"
            return
        if not is_admin():
            self.error = "需要管理员权限"
            return
        self._gen += 1
        self._started_at = time.monotonic()
        self._last_line = self._started_at
        try:
            self._proc = subprocess.Popen(
                [
                    str(self.exe),
                    "--output_stdout",
                    "--no_console_stats",
                    "--session_name",
                    self.session_name,
                    "--stop_existing_session",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                cwd=str(self.exe.parent),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.error = f"启动失败: {exc}"
            return
        # Make PresentMon die with this process even if we are killed hard.
        self._job = _kill_on_close_job()
        if self._job:
            try:
                ctypes.windll.kernel32.AssignProcessToJobObject(
                    wintypes.HANDLE(self._job), wintypes.HANDLE(int(self._proc._handle))
                )
            except Exception:
                pass
        self.running = True
        threading.Thread(
            target=self._reader, args=(self._gen,), name="presentmon", daemon=True
        ).start()

    def _trusted_binary(self) -> bool:
        try:
            return (
                file_sha256(self.exe) == _PRESENTMON_SHA256
                and verify_authenticode(self.exe)
            )
        except OSError:
            return False

    def _reader(self, gen: int) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        i_app, i_t = 0, 8
        while True:
            raw = stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            if line.startswith("Application"):
                cols = [c.strip() for c in line.split(",")]
                try:
                    i_app = cols.index("Application")
                    i_t = cols.index("TimeInMs")
                except ValueError:
                    pass
                continue
            if line.startswith(("warning", "error")):
                continue
            fields = line.split(",")
            if len(fields) <= max(i_app, i_t):
                continue
            app = fields[i_app].strip().strip('"').lower()
            try:
                # PresentMon's own presentation timestamp (ms since boot) gives
                # real frame spacing; pipe read times arrive in bursts.
                t = float(fields[i_t])
            except ValueError:
                continue
            if not app or app == "<unknown>" or t <= 0 or t > 1e12:
                continue
            now = time.monotonic()
            with self._lock:
                self.frames.append((t, app))
            self._last_line = now
            self._failures = 0
        if gen != self._gen:
            return
        self.running = False
        if self.error is None:
            self.error = "PresentMon 已退出"

    def _maybe_restart(self, now: float) -> None:
        """Restart PresentMon when it goes silent (ETW session can stall)."""
        if self._disabled or not self.running:
            return
        if now - self._started_at < _STARTUP_GRACE:
            return
        if now - self._last_line < _STALL_SECONDS:
            return
        if now - self._last_restart < _RESTART_INTERVAL:
            return
        self._last_restart = now
        self._stop_proc()
        self._failures += 1
        if self._failures >= _MAX_FAILURES:
            # ETW is unusable here; the screen meter already covers FPS.
            self._disabled = True
            self.error = "PresentMon 无数据"
            return
        self.error = None
        self.start()

    def _from_presentmon(self, now: float) -> dict | None:
        with self._lock:
            latest = max((t for t, _ in self.frames), default=0.0)
            while self.frames and latest - self.frames[0][0] > _KEEP_MS:
                self.frames.popleft()
            window = [(t, app) for t, app in self.frames if latest - t <= _FPS_WINDOW_MS]
        if not self.running or not window:
            return None

        counts = collections.Counter(
            app for _, app in window if app not in _BLOCKLIST and app not in self._own
        )
        span = (latest - min(t for t, _ in window)) / 1000.0
        if span < 0.5:
            span = _FPS_WINDOW_MS / 1000.0
        foreground = _foreground_process_name()
        if foreground in _SHELL or foreground in _BLOCKLIST or foreground in self._own:
            # Never report the overlay itself or the shell as the active app.
            foreground = None
        if foreground and foreground in counts:
            return {
                "fps": counts[foreground] / span,
                "app": foreground,
                "source": "presentmon",
                "detail": None,
            }
        if foreground:
            # A real app is in front but produced no frames.
            return {
                "fps": 0.0,
                "app": foreground,
                "source": "presentmon",
                "detail": "无出帧",
            }
        if counts:
            app = counts.most_common(1)[0][0]
            return {
                "fps": counts[app] / span,
                "app": app,
                "source": "presentmon",
                "detail": None,
            }
        return None

    def read(self) -> dict:
        now = time.monotonic()
        self._maybe_restart(now)
        result = self._from_presentmon(now)
        if result is not None:
            return result

        fallback = self._screen.read()
        if fallback.get("fps") is not None:
            return fallback
        if self.running and now - self._started_at < _STARTUP_GRACE:
            return {
                "fps": None,
                "app": None,
                "source": "starting",
                "detail": "PresentMon 启动中",
            }
        if not self.running and self.error:
            fallback["detail"] = self.error
        return fallback

    def _stop_proc(self) -> None:
        self.running = False
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None

    def stop(self) -> None:
        self._gen += 1
        self._screen.stop()
        self._stop_proc()
