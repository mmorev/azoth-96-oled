"""SourceRouter (D1) — the "key → provider" registry instead of a method-API.

The key space legalizes the existing slide grid (one grid for the CLI
validation — parse_monitor_items — and the router):

    cpu|gpu|ram × usage|temp|freq|volt|fan   battery.percent   volume.percent   volume.muted

- get(key) -> int | None: the first non-None value along the priority chain;
  None is the protocol language — the provider does not serve the key →
  the slide is skipped with a single warning (the warn-once per dead key
  is here, from HostSensors._dead).
- The priorities are data, not code (D1): the provider list is assembled in
  the order demo > manual > platform chain > psutil (+ volume) — the reserve
  for a future "--sensor key=provider".
- The keys are validated at build time (fail-fast); a typo in a string key
  kills the daemon at startup, not in the middle of a slideshow.
"""
from __future__ import annotations

import sys

from ..constants import SLIDE_METRICS, SLIDE_SOURCES
from ..log import log
from .base import Source
from .common.demo import DemoSource
from .common.manual import ManualSource
from .common.psutil_src import PsutilSource
from .darwin.macmon import MacmonSource
from .darwin.volume import MacVolume
from .linux.hwmon import LinuxHwmonSource
from .windows.lhm import LhmSource
from .windows.volume import WindowsVolume

# The key grid — one for the CLI validation (parse_monitor_items) and the router.
VALID_KEYS = frozenset(
    f"{src}.{met}" for src in SLIDE_SOURCES for met in SLIDE_METRICS
) | {"battery.percent", "volume.percent", "volume.muted"}

# The grid keys no provider ever serves (the former slide_value raised a
# KeyError for them — the same "structurally nonexistent slide" the slideshow
# marks dead, with the cpu.usage fallback). get() raises KeyError for them —
# 1:1 with the pre-split behavior; the sensor-absent keys keep returning None
# (the slide is skipped with a single warning and retried).
UNSERVED_KEYS = frozenset(
    f"{src}.fan" for src in SLIDE_SOURCES if f"{src}.fan" not in
    {"cpu.fan"}   # cpu.fan is served (macmon/psutil/LHM); gpu/ram fans do not exist
)


class SourceRouter:
    def __init__(self, providers: list[Source]) -> None:
        self._providers = providers
        self._warned = set()   # "sensor not found" — one warning per key
        for p in providers:    # the key validation at build (fail-fast)
            for k in p.keys():
                if k not in VALID_KEYS:
                    raise ValueError("the provider %s claims an unknown key "
                                     "\"%s\" (the valid grid: %s)"
                                     % (type(p).__name__, k, ", ".join(sorted(VALID_KEYS))))

    def get(self, key: str) -> int | None:
        """The value of a "source.metric" key in the 0x66 tile units
        (Usage=%, Temp=°C, Freq=MHz, Fan=RPM, Volt=mV — PROTOCOL_OLED.md
        §10.5); None = the slide is skipped (the sensor unavailable — a
        single warning, see _warned). "ram" on screen = the "DRAM0" header."""
        if key not in VALID_KEYS:
            raise ValueError("unknown source key \"%s\"" % key)
        if key in UNSERVED_KEYS:
            # the former slide_value dict-lookup KeyError (the structurally
            # nonexistent slide — gpu.fan/ram.fan)
            raise KeyError(key)
        claimed = False
        for p in self._providers:
            if key not in p.keys():
                continue
            claimed = True
            v = p.get(key)
            if v is not None:
                return v
        if claimed and key not in self._warned:
            self._warned.add(key)
            log("the sensor \"%s\" not found — the slide will be skipped" % key)
        return None

    def provider(self, key: str) -> Source | None:
        """The first provider claiming the key — for the extended interface
        beyond keys()/get() (the volume overlay uses fresh/nudge/on_unmute
        directly, D2)."""
        for p in self._providers:
            if key in p.keys():
                return p
        return None

    def start(self) -> None:
        """The lazy resource hooks of all the providers (idempotent)."""
        for p in self._providers:
            p.start()

    def stop(self) -> None:
        for p in self._providers:
            p.stop()


def build_router(config, manual: bool = True,
                 demo: bool | None = None) -> SourceRouter:
    """The provider chain assembled per the platform (the priority = the
    list order, D1). config — the CLI Config (the manual values, --demo);
    manual=False strips the manual provider (--status ignores the manual
    flags — as before the split)."""
    providers: list[Source] = []
    if config.demo if demo is None else demo:
        providers.append(DemoSource())
    if manual:
        providers.append(ManualSource(config.cpu, config.temp,
                                      config.ram_val, config.bat))
    if sys.platform == "darwin":
        providers.append(MacmonSource())
    elif sys.platform == "win32":
        providers.append(LhmSource())
    else:
        providers.append(LinuxHwmonSource())
    providers.append(MacVolume() if sys.platform == "darwin" else WindowsVolume())
    providers.append(PsutilSource())
    return SourceRouter(providers)
