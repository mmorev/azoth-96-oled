"""psutil-based values (common on all OSes): the CPU/RAM load, the CPU
frequency (the fallback after macmon) and the host battery."""
from __future__ import annotations

from ..base import Source

try:
    import psutil
except ImportError:       # the fail-fast is preserved: get() raises SystemExit,
    psutil = None         # the missing dependency must not become quiet Nones


class PsutilSource(Source):
    def keys(self) -> tuple[str, ...]:
        return ("cpu.usage", "ram.usage", "cpu.freq", "battery.percent")

    def get(self, key: str) -> int | None:
        if key == "cpu.usage":
            if psutil is None:
                # the fail-fast invariant from the live runs: without psutil
                # the cpu.usage/ram.usage slides are a stop, not a skip
                raise SystemExit("no CPU data: pip install psutil or set --cpu")
            return int(round(psutil.cpu_percent(interval=None)))
        if key == "ram.usage":
            if psutil is None:
                raise SystemExit("no RAM data: pip install psutil or set --ram-val")
            return int(round(psutil.virtual_memory().percent))
        if key == "cpu.freq":
            # The current CPU frequency in MHz: on Apple Silicon psutil returns
            # the base one (macmon goes first in the router chain). On error
            # 0 (never None) — like cpu_freq_mhz before the split.
            if psutil is None:
                return 0
            try:
                f = psutil.cpu_freq()
                return int(round(f.current)) if f and f.current else 0
            except Exception:
                return 0
        # battery.percent
        if psutil is None or not hasattr(psutil, "sensors_battery"):
            return None
        bat = psutil.sensors_battery()
        if bat is None:
            return None
        return int(round(bat.percent))
