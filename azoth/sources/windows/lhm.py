"""LibreHardwareMonitor/OpenHardwareMonitor values over WMI (Windows):
moved 1:1 from HostSensors._lhm_connect/_lhm_pick. Subsystem failures
(the wmi import, the namespace, a query) are logged once; a missing sensor
warns once per key — in the router (the warn-once per dead key)."""
from __future__ import annotations

import sys

from ..base import Source
from ...log import log


class LhmSource(Source):
    """key → (SensorType, the name patterns (the order = the priority), what,
    fallback_any, scale). fallback_any — if none of the pattern names exist at
    all, take the first sensor of the type (some boards call Vcore "Voltage #N")."""

    LHM_NS = ("root\\LibreHardwareMonitor", "root\\OpenHardwareMonitor")

    _PICKS = {
        "cpu.temp":  ("Temperature", ("cpu package", "package", "cpu"),
                      "the CPU temperature (Package)", False, 1.0),
        "cpu.volt":  ("Voltage", ("vcore", "cpu"),
                      "the CPU voltage (Vcore)", True, 1000.0),
        "gpu.usage": ("Load", ("gpu core", "gpu"),
                      "the GPU load", False, 1.0),
        "gpu.temp":  ("Temperature", ("gpu core", "hot spot", "gpu"),
                      "the GPU temperature", False, 1.0),
        "gpu.freq":  ("Clock", ("gpu core", "gpu"),
                      "the GPU frequency", False, 1.0),
        "cpu.fan":   ("Fan", ("cpu", "fan"),
                      "the CPU fan RPM", True, 1.0),
        "gpu.volt":  ("Voltage", ("gpu core", "gpu"),
                      "the GPU voltage", False, 1000.0),
        "ram.temp":  ("Temperature", ("sodimm", "dimm", "memory", "ram"),
                      "the RAM temperature (DIMM)", False, 1.0),
        "ram.freq":  ("Clock", ("memory clock", "memory", "dram"),
                      "the RAM frequency", False, 1.0),
        "ram.volt":  ("Voltage", ("dimm", "dram", "vddr", "memory"),
                      "the RAM voltage (DIMM)", False, 1000.0),
    }

    def __init__(self):
        self._wmi = None
        self._wmi_dead = False    # the LHM subsystem as a whole (import/namespace)
        self._dead = set()        # "sensor not found" — one warning per key (the router logs),
                                  # the set spares the repeated WMI queries

    def keys(self) -> tuple[str, ...]:
        if sys.platform != "win32" or self._wmi_dead:
            return ()             # LHM/WMI is Windows-only: on macOS — macmon
        return tuple(self._PICKS)

    def _connect(self, what: str):
        """self._wmi or None; the connection is lazy, a failure — a single warning."""
        if self._wmi is not None:
            return self._wmi
        if self._wmi_dead:
            return None
        try:
            import wmi
        except ImportError:
            self._wmi_dead = True
            log("WMI sensors unavailable: pip install wmi + run "
                "LibreHardwareMonitor.exe (%s will be missing)" % what)
            return None
        for ns in self.LHM_NS:
            try:
                self._wmi = wmi.WMI(namespace=ns)
                return self._wmi
            except Exception:
                continue
        self._wmi_dead = True
        log("the LibreHardwareMonitor WMI namespace not found — run "
            "LibreHardwareMonitor.exe (%s will be missing)" % what)
        return None

    def get(self, key: str) -> int | None:
        """The first LibreHardwareMonitor sensor of the key's type whose name
        (in lower case) contains at least one of the patterns; the value is
        scaled (V → mV). Subsystem/sensor unavailable → None (a missing sensor
        warns once per key — in the router)."""
        if self._wmi_dead:
            return None
        stype, patterns, what, fallback_any, scale = self._PICKS[key]
        if key in self._dead:
            return None
        w = self._connect(what)
        if w is None:
            self._dead.add(key)
            return None
        try:
            rows = w.query("SELECT Name, Value FROM Sensor WHERE SensorType='%s'"
                           % stype)
        except Exception:
            self._dead.add(key)
            log("the \"%s\" sensor query failed — the value is skipped" % what)
            return None
        vals = [(str(r.Name or "").lower(), float(r.Value))
                for r in rows if r.Value is not None]
        for pat in patterns:
            for name, v in vals:
                if pat in name:
                    return int(round(v * scale))
        if fallback_any and vals:
            return int(round(vals[0][1] * scale))
        self._dead.add(key)
        return None
