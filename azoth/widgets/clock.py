"""The clock widget (slot 1): a 0x63 sync at the boundary of every minute."""
from __future__ import annotations

import datetime as dt

from ..constants import SLOT_CLOCK
from .base import Widget, robust_push, sync_clock


class ClockWidget(Widget):
    slot = SLOT_CLOCK

    def __init__(self) -> None:
        self.last_min = None        # the minute of the last clock sync

    def refresh(self, kbd, router, now, config) -> int:
        # The on-screen clock is static text from 0x63, it does not tick
        # by itself (confirmed 2026-10-04: the time hung over from the
        # previous run). Sync at the boundary of every minute.
        now_dt = dt.datetime.now()
        if self.last_min is None or (now_dt.second < 5 and now_dt.minute != self.last_min):
            robust_push(kbd, lambda: sync_clock(kbd))
            self.last_min = now_dt.minute
            return 1
        return 0
