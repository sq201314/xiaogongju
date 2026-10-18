"""Desktop monitor widget entry point."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import Config  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.window import MonitorWindow  # noqa: E402
from app.win32 import integrity_rid, process_integrity_rid  # noqa: E402

MUTEX_NAME = "Global\\DesktopPulseWidget"


def _watch_pid_from_argv(argv: list[str]) -> int | None:
    for index, arg in enumerate(argv):
        if arg == "--watch-pid" and index + 1 < len(argv):
            try:
                return int(argv[index + 1])
            except ValueError:
                return None
    return None


def _our_instances() -> list:
    """Processes that are actually running this script with a Python interpreter.

    Matching is deliberately strict: the executable must be a Python
    interpreter and some argument must be exactly this file's path (or the bare
    name `main.py` with this directory as cwd). Loose substring matching would
    also kill editors / tools that merely have `…\main.py` open.
    """
    import psutil

    interpreters = {
        "python.exe",
        "pythonw.exe",
        "python3.exe",
        "python",
        "py.exe",
        "pyw.exe",
    }
    main_path = os.path.normcase(str(ROOT / "main.py"))
    root_dir = os.path.normcase(str(ROOT))
    found = []
    for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
        if proc.info["pid"] == os.getpid():
            continue
        cmdline = [str(part) for part in (proc.info.get("cmdline") or [])]
        if len(cmdline) < 2:
            continue
        exe = os.path.basename(os.path.normcase(cmdline[0]))
        if exe not in interpreters:
            continue
        try:
            cwd = proc.info.get("cwd") or ""
        except Exception:
            cwd = ""
        runs_main = any(
            os.path.normcase(part) == main_path
            or (
                os.path.basename(os.path.normcase(part)) == "main.py"
                and os.path.normcase(cwd) == root_dir
            )
            for part in cmdline[1:]
        )
        if runs_main:
            found.append(proc)
    return found


def _ancestor_pids() -> set[int]:
    import psutil

    pids: set[int] = set()
    try:
        proc = psutil.Process(os.getpid()).parent()
        while proc is not None:
            pids.add(proc.pid)
            proc = proc.parent()
    except Exception:
        pass
    return pids


def _take_over_restricted_instances(force: bool = False) -> bool:
    """Replace an existing instance when this launch has enough rights.

    Windows refuses WLAN queries for low-integrity processes, so a restricted
    instance must not keep the mutex when a normal-permission launch starts.
    """
    mine = integrity_rid() or 0
    skip = _ancestor_pids() | {os.getpid()}
    instances = [proc for proc in _our_instances() if proc.pid not in skip]
    if not instances:
        return False
    targets = [
        proc
        for proc in instances
        if force or (process_integrity_rid(proc.pid) or 0) < mine
    ]
    if not targets:
        return False
    killed = 0
    for proc in targets:
        try:
            proc.terminate()
            proc.wait(timeout=3)
            killed += 1
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
                killed += 1
            except Exception:
                pass
    if not killed:
        return False
    # Only continue when nothing else is left holding the mutex.
    return not [p for p in _our_instances() if p.pid not in skip]


def already_running() -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main() -> int:
    force_replace = "--replace" in sys.argv
    if already_running() and not _take_over_restricted_instances(force_replace):
        return 0
    config = Config()
    try:
        scale = float(config.get("ui_scale", 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    theme.set_scale(scale)
    # Scaling is applied per-widget to the overlay only; ignore any global Qt
    # scale factor inherited from an older launch.
    os.environ.pop("QT_SCALE_FACTOR", None)
    app = QApplication(sys.argv)
    app.setApplicationName("性能监控")
    app.setApplicationDisplayName("性能监控")
    app.setQuitOnLastWindowClosed(False)
    app.setFont(theme.font(9, scale=1.0))

    window = MonitorWindow(config, watch_pid=_watch_pid_from_argv(sys.argv))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
