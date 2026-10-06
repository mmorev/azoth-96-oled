"""The monitor widget (slot 3, D3): the value machinery and the slideshow
automaton — the swipes, hold_until, the dead slides, the cpu.usage fallback
and the echo=1 double-tile push (moved 1:1 from run()). Since step 2 the
values come from the SourceRouter — the "key → provider" registry."""
from __future__ import annotations

import sys

from ..constants import (DEFAULT_SLIDES, HEARTBEAT_S, SLIDE_ALIASES,
                         SLIDE_METRICS, SLIDE_SOURCES, SLOT_MONITOR,
                         SWIPE_PAUSE_S)
from ..log import log
from .base import Widget, robust_push


def slide_sel(spec: str) -> int:
    """The 0x66 tile selector from a "source.metric" slide name."""
    src, met = spec.split(".", 1)
    return (SLIDE_SOURCES[src] << 4) | SLIDE_METRICS[met]


def pair_label(sel: int, digit: int) -> str:
    hdr = {0: "CPU", 1: "GPU", 2: "VRM", 3: "DRAM", 4: "CHA"}.get((sel >> 4) & 0xF, "?")
    val = {0: "Usage", 1: "Temp", 2: "Freq", 3: "Fan", 4: "Fan", 5: "Volt"}.get(sel & 0xF, "?")
    return "%s%s %s" % (hdr, "" if digit == 0xFF else digit, val)


def resolve_pairs(args, router) -> list[tuple[int, int, int]]:
    """The 0x66 pairs for the --metrics mode. The manual values (--cpu etc.)
    take priority via the router chain (the manual provider)."""
    if args.demo:
        pairs = [(0x00, 0, router.get("cpu.usage"))]
        if args.metrics == "cpu-ram" or (args.metrics == "cpu-temp"
                                         and args.temp is None):
            # The second tile — RAM as "DRAM0 Usage" (the live test of the
            # hypothesis): a neutral empty pair looks like "CPU Usage 0" on
            # slot 3.
            pairs.append((0x30, 0, router.get("ram.usage")))
        elif args.metrics == "cpu-temp":
            pairs.append((0x01, 0, router.get("cpu.temp")))
        return pairs
    cpu = router.get("cpu.usage")
    if cpu is None:
        raise SystemExit("no CPU data: pip install psutil or set --cpu")
    if args.metrics == "cpu-temp":
        temp = router.get("cpu.temp")
        if temp is not None:
            return [(0x00, 0, cpu), (0x01, 0, temp)]
        return [(0x00, 0, cpu)]          # a fallback: a single Usage (like GearLink)
    if args.metrics == "cpu-ram":
        ram = router.get("ram.usage")
        if ram is None:
            raise SystemExit("no RAM data: pip install psutil or set --ram-val")
        return [(0x00, 0, cpu), (0x30, 0, ram)]      # "DRAM0 Usage" — the hypothesis
    return [(0x00, 0, cpu)]


def pairs_differ(a, b) -> bool:
    if a is None or b is None or len(a) != len(b):
        return True
    return any(x[0] != y[0] or abs(x[2] - y[2]) >= 1 for x, y in zip(a, b))


def parse_monitor_items(spec: str | None) -> list[str]:
    """--monitor-items "cpu.usage,gpu.temp,…" → a validated list of canonical
    names. The "source.metric" format — the GearLink config grid
    (PROTOCOL_OLED.md §10.5): the sources cpu/gpu/ram, the metrics
    usage/temp/freq/volt. A "a+b" pair (exactly two tiles) is pushed as one
    0x66 with two pairs — the double tile. The short first-draft names
    (cpu, ram, temp, …) are accepted as aliases. An unknown name and an
    empty list are a startup error; duplicates collapse, the order is
    preserved; None (the flag not given) → the default set."""
    if spec is None:
        return list(DEFAULT_SLIDES)
    names, seen = [], set()
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        halves = [SLIDE_ALIASES.get(h, h) for h in token.split("+")]
        if len(halves) > 2:
            raise SystemExit(
                "--monitor-items: \"%s\": a pair is at most two tiles \"a+b\""
                % raw.strip())
        for h in halves:
            parts = h.split(".")
            if (len(parts) != 2 or parts[0] not in SLIDE_SOURCES
                    or parts[1] not in SLIDE_METRICS):
                raise SystemExit(
                    "--monitor-items: unknown name \"%s\"; the format is "
                    "\"source.metric\" or a pair \"a+b\": the sources %s, the "
                    "metrics %s; the short names (%s) are accepted too"
                    % (raw.strip(), "/".join(SLIDE_SOURCES),
                       "/".join(SLIDE_METRICS), ", ".join(SLIDE_ALIASES)))
        token = "+".join(halves)
        if token not in seen:
            seen.add(token)
            names.append(token)
    if not names:
        raise SystemExit("--monitor-items: an empty list — give at least one "
                         "name, e.g. cpu.usage,ram.usage,gpu.temp")
    return names


class MonitorWidget(Widget):
    """The DOUBLE indicator (slot 3): the --metrics pairs or the slideshow
    automaton — the swipes, hold_until, the dead slides and the cpu.usage
    fallback, the echo=1 for the double tile, the 10 s heartbeat."""

    slot = SLOT_MONITOR

    def __init__(self, config, router) -> None:
        self.router = router
        self.slides = config.monitor_items   # a validated non-empty list (parse_monitor_items)
        self.n_slides = len(self.slides)
        self.slide_phase = 0
        self.pending_swipes = 0    # the swipes pulled out of the queue by the loop tail
                                   # wakeup (get() removes the frame — without this
                                   # counter they were lost and the swipe "did not work")
        self.hold_until = 0.0      # the moment of the next auto-page advance
                                   # (monotonic; a swipe sets now + SWIPE_PAUSE_S —
                                   # a swipe and an auto tick cannot coincide, no
                                   # double jumps)
        self.last_pairs = None
        self.last_push_t = 0.0     # the heartbeat: an unconditional push every HEARTBEAT_S
        self.slide_warned = set()  # the temp/volt slides already warned about
        self.dead_slides = set()   # structurally nonexistent slides (gpu.fan etc.)

    def on_swipes(self, n: int) -> None:
        self.pending_swipes += n

    def wait_delay(self, now: float) -> float:
        """The tail sleep timeout in the slideshow mode: until the next
        auto-page advance (capped 0.05..1 s)."""
        return min(1.0, max(0.05, self.hold_until - now))

    def refresh(self, kbd, router, now, config) -> int:
        swipes = self.pending_swipes
        self.pending_swipes = 0
        if config.slideshow:
            pairs = self._slideshow_pairs(now, config, swipes)
        else:
            pairs = resolve_pairs(config, self.router)
        if pairs is not None and (pairs_differ(pairs, self.last_pairs)
                                  or now - self.last_push_t >= HEARTBEAT_S):
            # A "a+b" pair is pushed with echo=1: the on-screen layout
            # follows the echo of the last push — echo=0 renders the top
            # tile only, echo=1 — both cells (live test 2026-10-06); the
            # 0396 swipes still reach the host normally (20 swipes
            # received during echo=1 pushes every 2 s).
            if robust_push(kbd, kbd.push_metrics, pairs,
                           1 if len(pairs) == 2 else 0):
                log("the 0x66 push: " + ", ".join(
                    "%s=%d" % (pair_label(sel, dig), val)
                    for sel, dig, val in pairs))
                self.last_pairs = pairs
                self.last_push_t = now
                return 1
            log("the 0x66 push not acknowledged (NAK/no reply)")
        return 0

    def _slideshow_pairs(self, now: float, config, swipes: int):
        """The slideshow like GearLink's (§10.6): the tile is overwritten by
        a single push with the next selector. The auto-scroll is timer-driven;
        a swipe down (03 96) pages immediately and puts the auto-paging on
        pause. A swipe UP is not reported to the host by the firmware at all
        (a clean session of 2026-10-05: >10 up — zero events on both iface2
        channels), so "back" is not implementable by the host — forward only,
        like GearLink."""
        if swipes:
            self.slide_phase = (self.slide_phase + swipes) % self.n_slides
            self.last_pairs = None
            self.hold_until = now + SWIPE_PAUSE_S
            log("swipe down → slide %d/%d \"%s\" (the auto-paging paused for %g s)"
                % (self.slide_phase + 1, self.n_slides, self.slides[self.slide_phase],
                   SWIPE_PAUSE_S))
        elif now >= self.hold_until:
            self.slide_phase = (self.slide_phase + 1) % self.n_slides
            self.last_pairs = None
            self.hold_until = now + config.slideshow
        # The value of the next slide; temp/volt without a sensor
        # are skipped (a one-time warning), the auto-paging moves
        # on. A structurally nonexistent slide (gpu.fan etc.) —
        # one log message, then the slide is "dead"; if ALL are
        # dead — the cpu.usage fallback (always available).
        pairs = None
        for _ in range(self.n_slides):
            name = self.slides[self.slide_phase]
            halves = name.split("+")
            try:
                vals = [self.router.get(h) for h in halves]
            except Exception as e:
                if name not in self.dead_slides:
                    self.dead_slides.add(name)
                    log("the slide \"%s\": no such sensor (%s)"
                        % (name, e))
                    if len(self.dead_slides) >= self.n_slides:
                        log("all the slides are without sensors — the cpu.usage fallback")
                        self.slides = ["cpu.usage"]
                        self.n_slides = 1
                        self.slide_phase = 0
                        self.slide_warned.clear()
                        self.last_pairs = None
                        break
                self.slide_phase = (self.slide_phase + 1) % self.n_slides
                continue
            if all(v is not None for v in vals):
                # a "a+b" pair — one 0x66 with two pairs (the
                # double tile); a single — one pair
                pairs = [(slide_sel(h), 0, v)
                         for h, v in zip(halves, vals)]
                break
            for h, v in zip(halves, vals):
                if v is None and h not in self.slide_warned:
                    self.slide_warned.add(h)
                    need = ("macmon (brew install macmon)"
                            if sys.platform == "darwin" else
                            "a running LibreHardwareMonitor (pip install wmi)")
                    log("the slide \"%s\" is skipped: the sensor is unavailable — %s is needed"
                        % (h, need))
            self.slide_phase = (self.slide_phase + 1) % self.n_slides
        return pairs
