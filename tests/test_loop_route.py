"""The unit check of the scheduler's 0xFFC0 frame routing (task 3.2, D5):
03 96 → the monitor (a swipe), the rocker 03 72 (01/02/04) → the volume
overlay, the release 00/93/95 → nothing on this channel. Run:
python tests/test_loop_route.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azoth.loop import _route


class FakeMonitor:
    def __init__(self):
        self.swipes = 0

    def on_swipes(self, n):
        self.swipes += n


class FakeOverlay:
    def __init__(self):
        self.ticks = []

    def tick(self, code):
        self.ticks.append(code)


mon, ovl = FakeMonitor(), FakeOverlay()

_route(bytes([0x03, 0x96, 0x00, 0x00, 0x30]), mon, ovl)
assert mon.swipes == 1 and ovl.ticks == []

_route(bytes([0x03, 0x72, 0x01]), mon, ovl)   # vol+
_route(bytes([0x03, 0x72, 0x04]), mon, ovl)   # vol−
_route(bytes([0x03, 0x72, 0x02]), mon, ovl)   # a press (the unmute-gate mark)
assert mon.swipes == 1 and ovl.ticks == [0x01, 0x04, 0x02]

_route(bytes([0x03, 0x72, 0x00]), mon, ovl)   # the release — not needed by anyone
_route(bytes([0x01, 0x00, 0x04]), mon, ovl)   # a consumer bitmap — a foreign frame
assert mon.swipes == 1 and ovl.ticks == [0x01, 0x04, 0x02]

print("loop routing unit checks OK")
