"""CPU / GPU telemetry."""

from __future__ import annotations

import subprocess
import winreg

import psutil

from ..pdh import PdhCounter, close_quietly
from ..win32 import system32


def _cpu_name() -> str:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        )
        with key:
            name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        for noise in ("(R)", "(TM)", "CPU", "  "):
            name = name.replace(noise, " " if noise == "  " else "")
        return " ".join(name.split())
    except OSError:
        return "CPU"


class NvidiaGpu:
    """NVML based dGPU telemetry."""

    def __init__(self) -> None:
        import pynvml

        self._nvml = pynvml
        pynvml.nvmlInit()
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(self.handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", "ignore")
        self.name = str(name).replace("NVIDIA ", "")

    def read(self) -> dict:
        nv = self._nvml
        h = self.handle
        out: dict = {"name": self.name, "vendor": "NVIDIA"}
        try:
            out["usage"] = float(nv.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            out["usage"] = None
        try:
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            out["mem_used"] = mem.used / (1024**2)
            out["mem_total"] = mem.total / (1024**2)
        except Exception:
            out["mem_used"] = out["mem_total"] = None
        try:
            out["temp"] = float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU))
        except Exception:
            out["temp"] = None
        try:
            out["power"] = float(nv.nvmlDeviceGetPowerUsage(h)) / 1000.0
        except Exception:
            out["power"] = None
        try:
            out["clock"] = float(nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_SM))
        except Exception:
            out["clock"] = None
        try:
            out["fan"] = float(nv.nvmlDeviceGetFanSpeed(h))
        except Exception:
            out["fan"] = None
        return out

    def close(self) -> None:
        try:
            self._nvml.nvmlShutdown()
        except Exception:
            pass


class _OtherGpu:
    """Fallback for non-NVIDIA systems using GPU Engine perf counters."""

    def __init__(self) -> None:
        self.name = self._detect_name()
        self._util: PdhCounter | None = None
        try:
            self._util = PdhCounter(r"\GPU Engine(*)\Utilization Percentage")
        except Exception:
            self._util = None

    @staticmethod
    def _detect_name() -> str:
        try:
            out = subprocess.run(
                [
                    system32("WindowsPowerShell", "v1.0", "powershell.exe"),
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_VideoController | "
                    "Where-Object { $_.Name -notmatch 'Virtual|Basic' } | "
                    "Select-Object -First 1 -ExpandProperty Name)",
                ],
                capture_output=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            text = out.stdout.decode("utf-8", "ignore").strip() or out.stdout.decode("mbcs", "ignore").strip()
            if text:
                return text
        except Exception:
            pass
        return "GPU"

    def read(self) -> dict:
        out: dict = {
            "name": self.name,
            "vendor": "Other",
            "usage": None,
            "mem_used": None,
            "mem_total": None,
            "temp": None,
            "power": None,
            "clock": None,
            "fan": None,
        }
        if self._util is not None and self._util.collect():
            total = sum(v for k, v in self._util.array().items() if "engtype_3D" in k)
            out["usage"] = max(0.0, min(100.0, total))
        return out

    def close(self) -> None:
        if self._util is not None:
            close_quietly(self._util)


class SystemCollector:
    def __init__(self) -> None:
        self.cpu_name = _cpu_name()
        self.cores = psutil.cpu_count(logical=False) or 0
        self.threads = psutil.cpu_count(logical=True) or 0
        try:
            self._temp: PdhCounter | None = PdhCounter(
                r"\Thermal Zone Information(*)\High Precision Temperature"
            )
        except Exception:
            self._temp = None
        try:
            # Turbo-aware live clock: base frequency scaled by % processor performance.
            self._perf: PdhCounter | None = PdhCounter(
                r"\Processor Information(_Total)\% Processor Performance"
            )
            self._base: PdhCounter | None = PdhCounter(
                r"\Processor Information(_Total)\Processor Frequency"
            )
        except Exception:
            self._perf = self._base = None
        try:
            self._rated = psutil.cpu_freq().max or psutil.cpu_freq().current
        except Exception:
            self._rated = 0.0
        if self._perf is not None and self._base is not None:
            self._perf.collect()
            self._base.collect()
        try:
            self._gpu: NvidiaGpu | _OtherGpu = NvidiaGpu()
        except Exception:
            self._gpu = _OtherGpu()
        psutil.cpu_percent(interval=None)

    def _frequency(self) -> float | None:
        if self._perf is not None and self._base is not None:
            self._perf.collect()
            self._base.collect()
            perf = self._perf.value()
            base = self._base.value()
            if perf and base:
                return base * perf / 100.0
        try:
            return psutil.cpu_freq().current
        except Exception:
            return None

    def cpu(self) -> dict:
        usage = psutil.cpu_percent(interval=None)
        freq = self._frequency()
        temp = None
        if self._temp is not None and self._temp.collect():
            values = [v / 10.0 - 273.15 for v in self._temp.array().values()]
            values = [v for v in values if -20 < v < 130]
            if values:
                temp = max(values)
        return {
            "name": self.cpu_name,
            "usage": usage,
            "freq": freq,
            "rated": self._rated,
            "temp": temp,
            "cores": self.cores,
            "threads": self.threads,
        }

    def gpu(self) -> dict:
        try:
            return self._gpu.read()
        except Exception:
            return {"name": getattr(self._gpu, "name", "GPU"), "usage": None}

    def close(self) -> None:
        close_quietly(self._temp, self._perf, self._base)
        self._gpu.close()
