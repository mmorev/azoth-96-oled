"""The CLI (moved 1:1): argparse → the frozen Config view, the once/status
modes, the flag implications. The daemon dispatch (connect/reconnect/run)
lives in main.py (D6)."""
from __future__ import annotations

import argparse
import time

from .constants import DEFAULT_SLIDESHOW_S, SLOT_MONITOR, WIDGET_NAMES
from .log import log, setup_log_file
from .sources.router import build_router
from .widgets.base import apply_layout, enabled_slots, sync_clock
from .widgets.battery import resolve_battery
from .widgets.monitor import pair_label, parse_monitor_items, resolve_pairs


def _positive_float(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number: %r" % text)
    if v <= 0:
        raise argparse.ArgumentTypeError(
            "a positive number is required, got %s" % text)
    return v


class Config:
    """A read-only view of the parsed Namespace (the CLI boundary, D6):
    the widgets/loop see it instead of argparse internals. Built once in
    parse() — after the implications, so nothing mutates it afterwards."""

    def __init__(self, ns: argparse.Namespace) -> None:
        self._ns = ns

    def __getattr__(self, name: str):
        return getattr(self._ns, name)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="azoth-companion",
        description="Azoth Companion v0.3 — a GearLink replacement for the ASUS ROG Azoth 96 HE "
                    "(M901): a set of OLED widgets (banner/clock/battery/"
                    "monitoring/KPS), metrics/slideshow and the volume "
                    "rocker OSD. Close GearLink before running "
                    "(taskkill //IM GearLink* //F) — otherwise NAK ping-pong. "
                    "On shutdown (Ctrl+C, SIGTERM) the dynamic widgets "
                    "are turned off, the banner stays.")

    g = ap.add_argument_group("widgets (which slots to enable; without flags — the banner only)")
    g.add_argument("--banner", action="store_true",
                   help="slot 0: the banner/music mode/a custom bitmap — static, "
                        "the daemon is not needed")
    g.add_argument("--clock", action="store_true",
                   help="slot 1: the clock — a 0x63 sync at the boundary of every minute")
    g.add_argument("--battery", action="store_true",
                   help="slot 2: the PC battery — a 0x64 push; --bat enables it automatically")
    g.add_argument("--monitor", action="store_true",
                   help="slot 3: the double indicator --metrics / slideshow; "
                        "enabled automatically by the content flags "
                        "(--metrics/--slideshow/--monitor-items/--demo/--cpu/"
                        "--temp/--ram-val)")
    g.add_argument("--kps", action="store_true",
                   help="slot 4: the native KPS counter (keys/s) — the firmware "
                        "draws it itself, the host only enables the slot")

    g = ap.add_argument_group("the working loop")
    g.add_argument("--interval", type=_positive_float, default=2.0,
                   help="the host polling period in the normal mode, s (default 2)")
    g.add_argument("--metrics", choices=("cpu-temp", "cpu-ram", "cpu"),
                   default=None,
                   help="the double indicator tile set without a slideshow: "
                        "usage+temp (the default; without a sensor — usage only), "
                        "usage+RAM (\"DRAM0\"), usage only; "
                        "enables --monitor")
    g.add_argument("--start", type=int, choices=range(5), default=None,
                   help="which slot to show after the setup (by default — "
                        "the monitor if enabled, otherwise the first enabled; "
                        "the slot must be in the widget set)")
    g.add_argument("--brightness", type=int, metavar="0-100", default=None,
                   help="re-set the brightness (by default leave it untouched)")
    g.add_argument("--keep-awake", action="store_true",
                   help="wake the OLED with `65 FF` every 60 s")
    g.add_argument("--no-clock", action="store_true",
                   help="do not sync the time")

    g = ap.add_argument_group("slideshow (single 0x66 tiles with echo=0, like GearLink)")
    g.add_argument("--slideshow", type=_positive_float, metavar="SEC", default=None,
                   help="page the slides every SEC seconds (the set — "
                        "--monitor-items, the default cpu.usage,ram.usage,"
                        "cpu.freq); a swipe down pages manually and puts "
                        "the auto-paging on a 5 s pause; enables --monitor")
    g.add_argument("--monitor-items", default=None, metavar="SRC.METRIC[,…]",
                   help="comma-separated slides, the \"source.metric\" format — "
                        "the GearLink config grid: the sources cpu/gpu/ram, "
                        "the metrics usage/temp/freq/volt/fan, e.g. cpu.usage,"
                        "cpu.freq,ram.usage,gpu.temp; a pair \"a+b\" (two tiles "
                        "in one 0x66 push, the double tile) is accepted too, e.g. "
                        "cpu.usage+ram.usage; ram is drawn with the "
                        "\"DRAM0\" header (0x30), gpu — \"GPU0\" (0x10); the sensor "
                        "slides (all temp/freq/volt and gpu.usage) require a "
                        "running LibreHardwareMonitor — without a sensor the "
                        "slide is skipped with a warning; the short names "
                        "(cpu, ram, freq, temp, volt) are accepted too; "
                        "--monitor-items without --slideshow enables a "
                        "slideshow with a 2 s period")

    g = ap.add_argument_group("manual values (tests without sensors)")
    g.add_argument("--cpu", type=int, metavar="0-100", default=None,
                   help="a manual CPU %% value (the cpu.usage slide and --metrics; "
                        "enables --monitor)")
    g.add_argument("--temp", type=int, default=None,
                   help="a manual temperature °C (the cpu.temp slide and --metrics "
                        "cpu-temp; enables --monitor)")
    g.add_argument("--ram-val", type=int, metavar="0-100", default=None,
                   help="a manual RAM %% value (the ram.usage slide and --metrics "
                        "cpu-ram; enables --monitor)")
    g.add_argument("--bat", type=int, metavar="0-100", default=None,
                   help="a manual PC battery %% value (enables --battery)")

    g = ap.add_argument_group("volume (the rocker OSD)")
    g.add_argument("--vol-hz", type=_positive_float, default=12.0, metavar="HZ",
                   help="the OSD push rate inside the rocker series window, Hz "
                        "(the OSD updates continuously at an interval >=50 ms; "
                        "values above 20 Hz are capped; the default 12 has "
                        "a margin)")
    g.add_argument("--no-volume", action="store_true",
                   help="do not listen to the rocker and do not push the volume "
                        "OSD (e.g. if the audio device is unavailable)")

    g = ap.add_argument_group("one-shot modes and diagnostics")
    g.add_argument("--once", action="store_true",
                   help="set up and push the values once, no loop")
    g.add_argument("--status", action="store_true",
                   help="only show the device and host status (read-only)")
    g.add_argument("--demo", action="store_true",
                   help="a sensor test: the CPU tile runs 0→100→0 (~5.5 s each way), "
                        "the push interval 0.4 s; enables --monitor")
    g.add_argument("--events", action="store_true",
                   help="listen to iface2 (slot changes/widget status) and write to the log")
    g.add_argument("--evt-dump", action="store_true",
                   help="dump all the 0xFFC0 frames (03 71/93/95/96) and the "
                        "transaction log (TXLOG) into the log")

    g = ap.add_argument_group("log file")
    g.add_argument("--log-file", nargs="?", const="", default=None, metavar="PATH",
                   help="mirror the log into a file, UTF-8, a ~2 MB rotation × 3 files; "
                        "without PATH — logs/azoth-companion.log next to main.py. "
                        "The autostart lives in scripts/install.py (install|uninstall)")
    return ap


def parse(argv: list[str] | None = None) -> Config:
    """argparse → implications → validation → Config (fail-fast on bad flags)."""
    args = build_parser().parse_args(argv)
    if args.log_file is not None:        # "" = --log-file without a value → the default path
        setup_log_file(args.log_file)

    # The content flags enable their widget themselves (as --slides used to
    # enable the slideshow): the metrics/slides/demo → the monitor, --bat →
    # the battery.
    if (args.metrics is not None or args.slideshow is not None
            or args.monitor_items is not None or args.demo
            or args.cpu is not None or args.temp is not None
            or args.ram_val is not None):
        args.monitor = True
    if args.bat is not None:
        args.battery = True
    if args.metrics is None:
        args.metrics = "cpu-temp"        # the --metrics default (already after the implication)
    items_given = args.monitor_items is not None   # parse_monitor_items(None)
    args.monitor_items = parse_monitor_items(args.monitor_items)  # returns the default
    if items_given and args.slideshow is None:
        args.slideshow = DEFAULT_SLIDESHOW_S   # --monitor-items enables the slideshow
    if args.start is not None and args.start not in enabled_slots(args):
        raise SystemExit("--start %d: the slot is not enabled (the set: %s) — add "
                         "the corresponding widget flag (--banner/--clock/"
                         "--battery/--monitor/--kps)"
                         % (args.start, ", ".join(
                             "%d %s" % (s, WIDGET_NAMES[s])
                             for s in enabled_slots(args))))
    return Config(args)


def once(kbd, config: Config) -> None:
    """A one-shot check: the layout + a push of the values, like the GearLink
    startup batch. Only what is enabled gets pushed (the content flags enable
    their widget themselves); after the exit the layout STAYS on screen — it
    is the visual verification mode, graceful shutdown does not turn it
    off."""
    apply_layout(kbd, config)
    router = build_router(config)
    if SLOT_MONITOR in enabled_slots(config):
        pairs = resolve_pairs(config, router)
        for i in range(2):
            ok = kbd.push_metrics(pairs)
            log("the 0x66 push #%d: %s → %s" % (i + 1, ", ".join(
                "%s=%d" % (pair_label(sel, dig), val) for sel, dig, val in pairs),
                "ok" if ok else "NO REPLY"))
            if ok and i == 0:
                time.sleep(0.3)
    if config.battery:
        pct = resolve_battery(router)
        if pct is not None:
            log("the PC battery %d%% → 0x64 %s" % (pct, kbd.set_slot2_value(pct, 0)))
    if config.clock and not config.no_clock:
        sync_clock(kbd)
    log("done; the screen is set up now — check the captions on the OLED")


def status(kbd, config: Config) -> None:
    """Read-only: writes nothing to the device."""
    print("== device ==")
    flags = kbd.get_status_flags()
    if flags is not None:
        on = ["%d %s" % (s, WIDGET_NAMES[s]) for s, f in enumerate(flags) if f]
        print(" the slot mask (0x24/02): %s → enabled: %s" % (flags, ", ".join(on) or "none"))
    print(" the current slot (0x24/01):", kbd.get_current_slot())
    print(" the current page (0x21):", kbd.get_current_page())
    print(" the screen on (0x23/02):", kbd.get_screen_state())
    print(" the keyboard battery (0x12/01):", kbd.get_battery(), "%")
    st = kbd.get_status_struct()
    if st:
        print(" the last metrics (0x12/00): temp=%d usage=%d metric3=%d" % (
            int.from_bytes(st[0:2], "little"), int.from_bytes(st[2:4], "little"),
            int.from_bytes(st[4:6], "little")))
    print("== host ==")
    router = build_router(config, manual=False, demo=False)
    cpu = router.get("cpu.usage")
    ram = router.get("ram.usage")
    temp = router.get("cpu.temp")
    bat = router.get("battery.percent")
    print(" CPU %s%%, RAM %s%%, temp %s, battery %s" % (
        cpu, ram,
        ("%d°C" % temp) if temp is not None else "n/a",
        ("%d%%" % bat) if bat is not None else "n/a"))
