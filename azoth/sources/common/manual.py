"""The manual values --cpu/--temp/--ram-val/--bat as a provider (D1): the
"manual has priority" branches leave the widgets, the priority is set by
the router chain order."""
from __future__ import annotations

from ..base import Source


class ManualSource(Source):
    def __init__(self, cpu, temp, ram_val, bat):
        self._vals = {"cpu.usage": cpu, "cpu.temp": temp,
                      "ram.usage": ram_val, "battery.percent": bat}

    def keys(self) -> tuple[str, ...]:
        return tuple(k for k, v in self._vals.items() if v is not None)

    def get(self, key: str) -> int | None:
        return self._vals.get(key)
