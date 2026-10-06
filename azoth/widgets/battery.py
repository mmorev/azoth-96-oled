"""The battery widget (slot 2): a 0x64 push on a ≥1% change (D3); the
manual --bat wins over psutil via the router chain (the manual provider)."""
from __future__ import annotations

from ..constants import SLOT_BATTERY
from ..log import log
from .base import Widget, robust_push


def resolve_battery(router) -> int | None:
    return router.get("battery.percent")


class BatteryWidget(Widget):
    slot = SLOT_BATTERY

    def __init__(self) -> None:
        self.last_bat = None
        self.warn_bat = True

    def refresh(self, kbd, router, now, config) -> int:
        pct = resolve_battery(router)
        if pct is None:
            if self.warn_bat and config.bat is None:
                log("the host battery not found — the battery widget is not pushed")
                self.warn_bat = False
            return 0
        if self.last_bat is None or abs(pct - self.last_bat) >= 1:
            if robust_push(kbd, kbd.set_slot2_value, pct, 0):
                log("the PC battery → slot %d: %d%%" % (SLOT_BATTERY, pct))
                self.last_bat = pct
                return 1
        return 0
