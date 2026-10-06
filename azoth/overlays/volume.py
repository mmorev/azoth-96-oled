"""The volume OSD overlay: VolumeWorker — the `51 0C` pusher with the strict
series-window cadence. The OS volume readers live in sources/<os>/volume.py
(D2: the volume keys are in the common registry; the overlay takes the
provider from the router and uses the extended interface directly)."""
from __future__ import annotations

import threading
import time

from ..devices.m901 import M901
from ..log import log
from ..sources.darwin.volume import MacVolume
from ..sources.windows.volume import WindowsVolume


class VolumeWorker(threading.Thread):
    """Firing `51 0C` with a STRICT cadence within the series window (live
    calibration 2026-10-05: intervals <50 ms break the OSD layer — a
    freeze/blink/resets; 50 ms (20 Hz) is the top working pace, the cadence
    is capped by it).

    A rocker tick opens the WINDOW_S window (every tick extends it); inside
    the window the worker once per 1/hz takes the FRESH value from the OS
    (a direct COM call, ~2 ms) and pushes it on change; the first tick of
    the window always pushes — a tick without a value change (hitting the
    0/100% stop) must also show the overlay. Firing by a timer, not by
    events: the rocker auto-repeats are not in the mirror (one tick per
    press, the dump of 2026-10-05), and the push pace is capped by the OSD
    layer threshold, see above.
    (The early variant slept a fixed 20 ms after every push and during a
    Windows volume ramp produced ~40 Hz — a guaranteed freeze mode.)"""

    WINDOW_S = 0.6
    MAX_HZ = 20.0            # the live threshold: 50 ms ok, 40 ms — blink/reset
    UNMUTE_GRACE_S = 1.0     # a press → the OS applies the mute within tens of ms (a margin for the poller)

    def __init__(self, kbd: M901, volume: WindowsVolume | MacVolume,
                 hz: float = 12.0):
        super().__init__(daemon=True)
        self._kbd = kbd
        self._vol = volume
        if hz > self.MAX_HZ:
            log("--vol-hz %g exceeds the OSD threshold (%g Hz) — capped" % (hz, self.MAX_HZ))
            hz = self.MAX_HZ
        self._cadence = 1.0 / max(1.0, float(hz))
        self._until = 0.0
        self._last = None
        self._was_open = False
        self._stop_evt = False
        self._press_t = 0.0     # the last rocker press (0 = long ago) — the unmute gate

    def tick(self, code: int) -> None:
        if code == 0x02:    # a rocker press: the window is NOT opened — only a mark
            self._press_t = time.monotonic()   # of origin for the unmute gate
            return
        self._until = time.monotonic() + self.WINDOW_S

    def unmute_gate(self) -> None:
        """Push the level on unmute only if it came from a rocker press:
        without the gate an unmute done via macOS (F10/a menu item) also
        fired the OSD (the request of 2026-10-05: a push — only in response
        to an unmute from the keyboard)."""
        if time.monotonic() - self._press_t <= self.UNMUTE_GRACE_S:
            self.kick()

    def stop(self) -> None:
        self._stop_evt = True

    def kick(self) -> None:
        """Open the window with a GUARANTEED push (the reaction to Unmute — a
        request of 2026-10-05: "the volume level in response to Unmute").
        _last=None → the first cadence tick always pushes; a NAK (the path
        is busy right after a rocker press) is retried on the next tick —
        the former single push from the poller thread without a retry was
        getting lost ("does not always push"). Called from the poller
        thread, the worker does the rest."""
        self._until = time.monotonic() + self.WINDOW_S
        self._last = None

    def run(self) -> None:
        next_push = 0.0
        while not self._stop_evt:
            now = time.monotonic()
            if now >= self._until:
                self._was_open = False
                time.sleep(0.005)     # a short idle: a window start without latency
                continue
            if now < next_push:       # the cadence: not earlier than the next tick
                time.sleep(min(0.005, next_push - now))
                continue
            v = self._vol.fresh()
            first = not self._was_open or self._last is None
            if v is not None and (first or v != self._last):
                if self._kbd.push_volume_osd(v):
                    self._last = v
                    log("volume OSD: %d%%" % v)
                    # While the value keeps changing during an active series —
                    # extend the window. Key for holding: the rocker vendor
                    # mirror auto-repeats once per press, and Windows ramps
                    # the volume itself in 2% steps while held — without the
                    # extension the window would close 0.6 s after the last
                    # tick and the OSD would lag the ramp (the 18:35–18:36
                    # log of 2026-10-05).
                    if self._until > now:
                        self._until = now + self.WINDOW_S
                # it did not make it — a retry on the next cadence tick
            self._was_open = True
            next_push = max(next_push + self._cadence, time.monotonic())
