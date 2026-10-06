"""The --demo mode as a provider: a test "running" sensor (a 0→100→0
triangle) with the highest router priority. The RAM tile runs with its own
phase — the values of the demo pairs of the --metrics mode are 1:1."""
from __future__ import annotations

import time

from ..base import Source


def demo_value(half: float = 5.5, phase: float = 0.0) -> int:
    """A 0→100→0 triangle, a half-period of half s — a test "running" sensor."""
    ph = ((time.monotonic() + phase) % (2 * half)) / half   # 0..2
    return int(round(100 * (ph if ph <= 1.0 else 2.0 - ph)))


class DemoSource(Source):
    def keys(self) -> tuple[str, ...]:
        return ("cpu.usage", "ram.usage")

    def get(self, key: str) -> int | None:
        if key == "ram.usage":
            return demo_value(half=7.0, phase=2.5)   # the second demo tile
        return demo_value()                          # cpu.usage
