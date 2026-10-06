"""A light Linux provider on psutil: the CPU temperature (sensors_temperatures)
and the fans (sensors_fans). gpu/ram stay unclaimed — None, the slides are
skipped normally. (The deep hwmon dive is deliberately deferred to a session
on a Linux machine.)"""
from __future__ import annotations

from ..base import Source
from ..common.psutil_src import psutil

_TEMP_PAT = ("cpu", "package", "core", "tctl", "k10temp")


class LinuxHwmonSource(Source):
    def keys(self) -> tuple[str, ...]:
        if psutil is None:
            return ()
        ks = []
        if hasattr(psutil, "sensors_temperatures"):
            ks.append("cpu.temp")
        if hasattr(psutil, "sensors_fans"):
            ks.append("cpu.fan")
        return tuple(ks)

    def get(self, key: str) -> int | None:
        if key == "cpu.temp":
            try:
                temps = psutil.sensors_temperatures()
            except Exception:
                return None
            for label, entries in temps.items():
                if entries and any(p in label.lower() for p in _TEMP_PAT):
                    return int(round(entries[0].current))
            return None
        # cpu.fan
        try:
            fans = psutil.sensors_fans()
        except Exception:
            return None
        for _label, entries in fans.items():
            if entries:
                return int(entries[0].current)
        return None
