"""The unit check of the MonitorWidget slideshow automaton (task 3.1): the
auto-paging, the swipe paging + the 5 s pause, the dead-slide fallback, the
echo=1 for the double tile, the 10 s heartbeat. Run:
python tests/test_monitor_widget.py"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azoth.constants import HEARTBEAT_S, SWIPE_PAUSE_S
from azoth.sources.router import UNSERVED_KEYS
from azoth.widgets.monitor import MonitorWidget


class FakeKbd:
    def __init__(self):
        self.pushes = []

    def push_metrics(self, pairs, echo=0):
        self.pushes.append((list(pairs), echo))
        return True


class FakeRouter:
    def __init__(self, **vals):
        self.vals = vals

    def get(self, key):
        if key in UNSERVED_KEYS:   # the real router's contract: the structural gap
            raise KeyError(key)
        return self.vals.get(key)


def make(slides, **vals):
    # the dotted keys: FakeRouter(**{"cpu.usage": 11, ...})
    config = SimpleNamespace(monitor_items=slides, slideshow=2.0,
                             metrics="cpu-temp", demo=False, bat=None,
                             cpu=None, temp=None, ram_val=None)
    kbd = FakeKbd()
    return MonitorWidget(config, FakeRouter(**vals)), config, kbd


def quiet(w, kbd, now, cfg):
    with contextlib.redirect_stdout(io.StringIO()):
        return w.refresh(kbd, w.router, now, cfg)


# 1. the auto-paging: the first refresh (hold_until=0.0) advances the phase —
#    the first push = the SECOND slide (as in the monolith loop)
w, cfg, kbd = make(["cpu.usage", "ram.usage"], **{"cpu.usage": 11, "ram.usage": 22})
assert quiet(w, kbd, 100.0, cfg) == 1
assert kbd.pushes == [([(0x30, 0, 22)], 0)], kbd.pushes
assert w.hold_until == 100.0 + 2.0

# 2. the heartbeat: in the metrics mode (no slideshow) the same pairs are
#    re-pushed unconditionally every HEARTBEAT_S
w1b, cfg1b, kbd1b = make(["cpu.usage"], **{"cpu.usage": 11})
cfg1b.slideshow = None              # the --metrics mode
cfg1b.metrics = "cpu"
assert quiet(w1b, kbd1b, 100.0, cfg1b) == 1
assert quiet(w1b, kbd1b, 105.0, cfg1b) == 0                    # fresh, no push
assert quiet(w1b, kbd1b, 100.0 + HEARTBEAT_S, cfg1b) == 1      # the heartbeat
assert len(kbd1b.pushes) == 2 and kbd1b.pushes[-1] == kbd1b.pushes[0]
# 2b. in the slideshow within hold_until: the same slide, no changes → no push
assert quiet(w, kbd, 101.0, cfg) == 0

# 3. the swipe: the page + the auto-paging paused for SWIPE_PAUSE_S
w2, cfg2, kbd2 = make(["cpu.usage", "ram.usage", "gpu.temp"],
                      **{"cpu.usage": 11, "ram.usage": 22, "gpu.temp": 55})
quiet(w2, kbd2, 100.0, cfg2)                 # → ram.usage, hold=102
w2.on_swipes(1)                              # the 03 96 routing (D5)
assert quiet(w2, kbd2, 101.0, cfg2) == 1     # the swipe processes instantly
assert kbd2.pushes[-1] == ([(0x11, 0, 55)], 0)          # gpu.temp
assert w2.hold_until == 101.0 + SWIPE_PAUSE_S           # the 5 s pause
# within the pause there is no auto-advance and no push (the same values)
assert quiet(w2, kbd2, 103.0, cfg2) == 0
# after the pause — the next slide (cpu.usage, the wraparound)
assert quiet(w2, kbd2, 106.0, cfg2) == 1
assert kbd2.pushes[-1] == ([(0x00, 0, 11)], 0)

# 4. the a+b double tile: one 0x66 with two pairs, echo=1
w3, cfg3, kbd3 = make(["cpu.usage+ram.usage"], **{"cpu.usage": 11, "ram.usage": 22})
assert quiet(w3, kbd3, 100.0, cfg3) == 1
assert kbd3.pushes == [([(0x00, 0, 11), (0x30, 0, 22)], 1)], kbd3.pushes

# 5. a sensor-absent slide → the warning path; within the same pass the
#    phase moves on to the next resolvable slide ("the auto-paging moves on")
w4, cfg4, kbd4 = make(["cpu.usage", "gpu.temp"], **{"cpu.usage": 11})   # gpu.temp — None
with contextlib.redirect_stdout(io.StringIO()) as buf:
    w4.refresh(kbd4, w4.router, 100.0, cfg4)   # directly: quiet() has its own redirect
assert "gpu.temp" in buf.getvalue() and "skipped" in buf.getvalue()
assert kbd4.pushes == [([(0x00, 0, 11)], 0)]   # the phase moved on (as in the monolith)

# 6. a structurally nonexistent slide (gpu.fan → the router KeyError) → dead,
#    all dead → the cpu.usage fallback (as slide_value's KeyError pre-split)
w5, cfg5, kbd5 = make(["gpu.fan", "cpu.usage"], **{"cpu.usage": 7})
with contextlib.redirect_stdout(io.StringIO()) as buf:
    quiet(w5, kbd5, 100.0, cfg5)   # gpu.fan → dead; cpu.usage resolves
assert kbd5.pushes == [([(0x00, 0, 7)], 0)]
w6, cfg6, kbd6 = make(["gpu.fan"], **{"cpu.usage": 7})
with contextlib.redirect_stdout(io.StringIO()) as buf:
    quiet(w6, kbd6, 100.0, cfg6)   # the only slide is dead → the fallback
assert w6.slides == ["cpu.usage"] and w6.n_slides == 1
assert quiet(w6, kbd6, 101.0, cfg6) == 1
assert kbd6.pushes == [([(0x00, 0, 7)], 0)]

print("monitor widget unit checks OK")
