"""The scheduler (D5): the single reader of the 0xFFC0 queue, the widget
refresh cycle and the sleep until the nearest deadline. The frames are
classified in place — `03 96` (a swipe) → the monitor, the rocker
`03 72` → the volume overlay, `03 93/95` (the iface2 mirror) → the log —
the former on_tick hack from the reader thread is gone. Also: the stop
handlers, the device-event channels and the reconnect wait."""
from __future__ import annotations

import queue
import signal
import threading
import time
from contextlib import suppress

from .constants import (SLOT_BANNER, SLOT_BATTERY, SLOT_CLOCK, SLOT_KPS,
                        SLOT_MONITOR, STAT_EVERY_S, WAKE_EVERY_S, WIDGET_NAMES)
from .devices import m901 as m901_client
from .devices.m901 import M901
from .log import log
from .overlays.volume import VolumeWorker
from .sources.darwin.volume import MacVolume
from .sources.router import build_router
from .sources.windows.volume import WindowsVolume
from .widgets.banner import BannerWidget
from .widgets.base import Widget, enabled_slots
from .widgets.battery import BatteryWidget
from .widgets.clock import ClockWidget
from .widgets.kps import KpsWidget
from .widgets.monitor import MonitorWidget, slide_sel

# Graceful shutdown: the handled termination signals set STOP, the main
# loop exits, and before closing the device all the widgets except the
# banner are turned off (shutdown_widgets) — without the daemon the
# clock/battery/metrics show stale data. SIGINT is not touched: Ctrl+C
# arrives normally as KeyboardInterrupt. On Windows an external SIGTERM almost
# always becomes TerminateProcess (taskkill /F, schtasks End) — that cannot be
# intercepted; the really interceptable paths there are SIGBREAK (Ctrl+Break)
# and the console CTRL_CLOSE.
STOP = threading.Event()
_stop_signal = None          # the name of the signal we were stopped by (for the log)


def _on_stop_signal(signum, _frame) -> None:
    global _stop_signal
    _stop_signal = signal.Signals(signum).name
    STOP.set()


def install_stop_handlers() -> None:
    """Attach _on_stop_signal to every termination signal available on the
    platform; the missing ones (SIGHUP/SIGQUIT on Windows) are skipped."""
    for name in ("SIGTERM", "SIGHUP", "SIGQUIT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_stop_signal)
        except (OSError, ValueError):    # not the main thread / the platform
            pass


def open_events(kbd: M901):
    """The iface2 event channel (0x93 a slot change, 0x95/0x96 the widget state)."""
    try:
        return kbd.open_consumer()
    except Exception as e:
        log("the iface2 event channel unavailable (%s) — continuing without it" % e)
        return None


def drain_events(cons) -> None:
    for _ in range(8):
        # timeout_ms=1, not 0: on Windows-hidapi 0 = infinite blocking
        data = cons.read(64, timeout_ms=1)
        if not data:
            return
        if data[0] == 0x03 and len(data) >= 5 and data[1] in (0x93, 0x95, 0x96):
            name = {0x93: "slot change", 0x95: "widget status",
                    0x96: "local change"}.get(data[1], "?")
            log("event 0x%02X %s: payload=%s"
                % (data[1], name, bytes(data[4:9]).hex()))


def open_ffc0(kbd: M901):
    """The 0xFFC0 channel (Col03 of iface2): the 03 93/95/96 event mirror +
    the 03 71 touch. Each `03 96` = an accepted vertical swipe (empirical
    2026-10-04)."""
    try:
        return kbd.open_ffc0()
    except Exception as e:
        log("the 0xFFC0 channel unavailable (%s) — the swipes will not page" % e)
        return None


def start_ffc0_reader(kbd: M901):
    """A background thread: continuously reads 0xFFC0 (a blocking read) and
    puts the 03-xx frames into a queue. A drain from the main loop LOST the
    reports: Windows-HID does not accumulate input reports without a pending
    read, and with one it delivers to every open handle. The thread does NOT
    dispatch anything itself — the queue has the single owner (the scheduler,
    D5): the frames are classified and routed in the main loop."""
    try:
        dev = kbd.open_ffc0()
    except Exception as e:
        log("the 0xFFC0 channel unavailable (%s) — the swipes will not page" % e)
        return None
    q = queue.Queue()

    def reader():
        while True:
            try:
                # an EXPLICIT timeout: read(64) without it = timeout_ms=0 =
                # a NON-blocking read (the cython-hidapi trap, see recv)
                data = dev.read(64, timeout_ms=1000)
            except Exception:
                break
            if data:
                q.put(bytes(data))

    threading.Thread(target=reader, daemon=True).start()
    return q


def _route(d, monitor, overlay, dump: bool = False) -> None:
    """The frame classification in place (D5): `03 96` = an accepted vertical
    swipe → the monitor (the slideshow paging); the rocker `03 72` → the
    volume overlay; `03 94` → the log. The 03 93/95 mirror is logged by
    drain_events (the iface2 channel)."""
    if dump:
        log("ffc0: %s" % d[:20].hex(" "))
    if d[0] == 0x03 and len(d) >= 3:
        if d[1] == 0x96:
            if monitor:
                monitor.on_swipes(1)
        elif d[1] == 0x72 and d[2] in (0x01, 0x02, 0x04):
            # The volume ticks (01 = vol+, 04 = vol−) open the OSD window;
            # a press of 02 does NOT open the window — it goes to the
            # worker only as a mark that "the unmute came from the rocker"
            # (a press = the mute toggle; pushing the window on press gave
            # a level OSD while muted — a regression of 2026-10-05).
            # The release 00 is not needed by anyone.
            if overlay:
                overlay.tick(d[2])
        elif d[1] == 0x94:
            # in capture 20 GearLink saw these twice; if they appear —
            # we want to know about it (a possible swipe "neighbor")
            log("event 0394: %s" % d[:10].hex(" "))


def drain_ffc0(ffc0, monitor, overlay, dump: bool = False) -> None:
    """Drain the accumulated frames (the queue must not grow)."""
    while True:
        try:
            d = ffc0.get_nowait()
        except queue.Empty:
            return
        _route(d, monitor, overlay, dump)


def build_widgets(args, router) -> list[Widget]:
    """The widgets of the enabled slots (D3). The banner/KPS are passive:
    the firmware draws them itself, the host only enables the slot. The
    clock is skipped by --no-clock (the mask keeps the slot — as before)."""
    widgets: list[Widget] = []
    for slot in enabled_slots(args):
        if slot == SLOT_BANNER:
            widgets.append(BannerWidget())
        elif slot == SLOT_CLOCK and not args.no_clock:
            widgets.append(ClockWidget())
        elif slot == SLOT_BATTERY:
            widgets.append(BatteryWidget())
        elif slot == SLOT_MONITOR:
            widgets.append(MonitorWidget(args, router))
        elif slot == SLOT_KPS:
            widgets.append(KpsWidget())
    return widgets


def start_gate_watcher(kbd: M901) -> None:
    """A log of the OSD gate [0x23000CA0] (12 00 payload[8], 10 Hz): 51 0C
    renders only at 0 — tracing the gate behavior in the press-hold-release
    rocker scenario (the value freeze on the OLED, 2026-10-05).
    IMPORTANT: 12 00 replies only on the fixed echo=0000."""
    def w():
        prev = None
        while True:
            r = kbd.transact(0x12, 0x00, b"", timeout_ms=150, echo=0)
            if r and r[0] == 0x12 and len(r) > 12:
                g = r[12]
                if g != prev:
                    log("the OSD gate [0x23000CA0] = %d" % g)
                    prev = g
            time.sleep(0.1)

    threading.Thread(target=w, daemon=True).start()


def run(kbd: M901, args) -> None:
    router = build_router(args)
    router.start()   # the volume poller (a 50 ms quantum) — the macmon reader stays lazy
    widgets = build_widgets(args, router)
    monitor = next((w for w in widgets if isinstance(w, MonitorWidget)), None)
    volume = router.provider("volume.percent")
    # the volume slot of the chain is exactly the volume provider (the extended
    # fresh/nudge/on_unmute interface is used directly, D2)
    assert isinstance(volume, (MacVolume, WindowsVolume))
    overlay = None
    if not args.no_volume:
        overlay = VolumeWorker(kbd, volume, hz=args.vol_hz)
        overlay.start()
        volume.on_unmute = overlay.unmute_gate   # a push — only on a rocker unmute
    if args.evt_dump:
        # a transaction log: catch which exact exchange NAKs in the blink window
        m901_client.TXLOG = lambda cmd, sub, e, r, nak, ms: log(
            "tx %02X.%02X echo=%04X → %s (%.0f ms)" % (
                cmd, sub, e,
                "NAK" if nak else (r[:8].hex(" ") if r else "no-reply"), ms))
        start_gate_watcher(kbd)
    cons = open_events(kbd) if args.events else None
    ffc0 = start_ffc0_reader(kbd)
    enabled = enabled_slots(args)
    do_monitor = monitor is not None
    if do_monitor:
        if args.slideshow:
            mode_desc = "a slideshow %.1f s [%s]" % (args.slideshow, ",".join(monitor.slides))
            log("slides: %s" % " → ".join(
                "%s(%s)" % (n, "+".join("0x%02X" % slide_sel(h)
                                        for h in n.split("+")))
                for n in monitor.slides))
        else:
            mode_desc = args.metrics
    else:
        mode_desc = "the monitor is off (the widgets: %s)" % ", ".join(
            WIDGET_NAMES[s] for s in enabled)
    log("the loop is running: interval %.1f s, %s (Ctrl+C/SIGTERM to exit)"
        % (args.interval, mode_desc))
    if not do_monitor and not args.keep_awake:
        log("no dynamic widgets — the display will fall asleep on its own "
            "timeout (nothing to push, see --keep-awake)")
    t_wake = t_stat = time.monotonic()
    try:
        while not STOP.is_set():
            now = time.monotonic()
            # The 0xFFC0 drain is always needed (the queue must not grow); the
            # frames are classified in place (_route): the 03 96 swipes go to
            # the monitor, the rocker 03 72 — to the volume overlay.
            if ffc0:
                drain_ffc0(ffc0, monitor, overlay, dump=args.evt_dump)
            for w in widgets:
                w.refresh(kbd, router, now, args)
            if args.keep_awake and now - t_wake >= WAKE_EVERY_S:
                kbd.wake_display(0xFF)
                t_wake = now
            if cons:
                drain_events(cons)
            if now - t_stat >= STAT_EVERY_S:
                log("kbd: battery %s%%, slot %s, page %s"
                    % (kbd.get_battery(), kbd.get_current_slot(),
                       kbd.get_current_page()))
                t_stat = now
            # wait, not sleep: STOP is checked at the top of every iteration.
            # We sleep on the 0xFFC0 queue: a swipe or a rocker tick wakes the
            # loop instantly (STOP.wait slept out the whole period — the swipe
            # was processed with a delay and landed right next to the auto
            # tick = a double jump). In the slideshow the timeout — until the
            # next auto-page advance; otherwise — the polling period.
            # IMPORTANT: get() REMOVES the frame — it is routed right away.
            if ffc0:
                if args.slideshow:
                    delay = monitor.wait_delay(time.monotonic()) if monitor else 1.0
                else:
                    delay = 0.4 if args.demo else args.interval
                with suppress(queue.Empty):
                    _route(ffc0.get(timeout=delay),
                           monitor, overlay, dump=args.evt_dump)
            else:
                STOP.wait(1.0 if args.slideshow else (0.4 if args.demo else args.interval))
    finally:
        if overlay:
            overlay.stop()
        router.stop()
        if cons:
            cons.close()


def wait_reconnect() -> M901 | None:
    """Wait for the keyboard to return to the bus, return a fresh M901
    (or None — stopped by a signal while waiting)."""
    warned = False
    while not STOP.is_set():
        try:
            k = M901()
            log("the keyboard is back — restarting the layout")
            return k
        except Exception:
            if not warned:
                log("no keyboard on the bus — waiting for a reconnect (Ctrl+C to exit)")
                warned = True
            STOP.wait(2.0)
    return None
