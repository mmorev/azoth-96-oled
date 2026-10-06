#!/usr/bin/env python3
"""
Azoth Companion v0.3 — a GearLink replacement for the ASUS ROG Azoth 96 HE (M901).

The protocol model (see README.md):

  • startup:          `65 FF` (wake) → optionally the `6A` slot enables +
                      `68` brightness + `50 55` commit (ONLY on a mask change);
  • values (no commit): `66` CPU usage/temp → slot 3,
                      `64` the PC battery % → slot 2,
                      `63` date+time → slot 1 (the device draws the dial itself);
  • slots: 0 = the banner/music mode (leave alone), 1 = the clock, 2 = the battery,
                      3 = the double indicator, 4 = the monitoring carousel;
  • GearLink polling: `12 01`/`12 00` every ~30 s, the `27` ping — not critical in v0.2.

Running:
  python main.py                         # the banner (slot 0) only + the rocker OSD
  python main.py --clock --battery --monitor
                                            # the classic GearLink set
  python main.py --monitor --slideshow 3 # a slideshow of cpu.usage → ram.usage → cpu.freq
  python main.py --monitor-items cpu.usage,gpu.temp,freq
                                            # a custom set (enables the slideshow, 2 s)
  python main.py --monitor-items cpu.usage,temp --slideshow 5 --log-file
                                            # the same + mirror the log into logs/azoth-companion.log
  python main.py --once --cpu 42 --bat 77   # a one-shot check
  python main.py --status                # read the status only, change nothing
  python main.py --demo --bat 42         # a sensor test: 0→100→0 (~5.5 s each way)

The widgets = the slots: --banner (0), --clock (1), --battery (2), --monitor (3),
--kps (4); without flags only the banner is enabled. The content flags enable
their widget automatically: --metrics/--slideshow/--monitor-items/--demo/--cpu/
--temp/--ram-val → --monitor, --bat → --battery.

Graceful shutdown: Ctrl+C and the handled termination signals (SIGTERM,
SIGBREAK, SIGHUP, SIGQUIT) turn off all the widgets except the banner — without
the daemon the clock/battery/metrics show stale data. Only the banner and KPS
live offline; an empty mask is invalid for the OLED, so if the daemon was started
without a banner, it gets re-enabled on shutdown. --once/--status do not turn
the layout off (--once leaves it on screen for visual verification).

The --monitor-items slides (the "source.metric" format, the GearLink config grid):
  the sources cpu / gpu / ram, the metrics usage / temp / freq / fan / volt — e.g.
  cpu.usage, cpu.temp, cpu.freq, cpu.volt, ram.usage, gpu.temp. The ram source is
  drawn with the "DRAM0" header (the selector 0x30), gpu — "GPU0" (0x10). The short
  first-draft names (cpu, gpu, ram, usage, temp, freq, volt) are accepted as
  aliases. The sensor slides (all temp/freq/volt and gpu.usage) require
  a running LibreHardwareMonitor — without a sensor the slide is skipped with a
  single warning. A swipe down pages the slides manually (a 5 s auto-paging
  pause); up is not reported by the firmware.

Autostart (Windows): python scripts/install.py install   # the task at logon
                    python scripts/install.py uninstall  # remove it

Dependencies: pip install hidapi psutil
The CPU temperature/voltage: run LibreHardwareMonitor.exe and pip install wmi
  (we read the WMI root\\LibreHardwareMonitor, the "CPU Package" and Voltage sensors).
  Without them the temp/volt slides are skipped, and --metrics cpu-temp pushes a
  single usage pair (a fallback, like GearLink with a single widget). There is no
  ACPI thermal zone on this machine (verified).
macOS (Apple Silicon): brew install macmon — the CPU/GPU temperatures, the real
  frequency and the GPU load without sudo (sudoless IOReport). Without it the
  temp/freq/gpu slides are skipped.

Close GearLink before running — two owners of the vendor channel are not welcome.
"""
from __future__ import annotations

from azoth import loop as loop_mod
from azoth.cli import once, parse, status
from azoth.devices.m901 import M901
from azoth.log import log
from azoth.loop import STOP, install_stop_handlers, wait_reconnect
from azoth.widgets.base import apply_layout, shutdown_widgets


def main() -> None:
    config = parse()
    install_stop_handlers()
    try:
        kbd = M901()
    except Exception as e:
        if config.status or config.once:
            raise SystemExit("keyboard not found: %s\n"
                             "(is the USB plugged in? is GearLink closed? pip install hidapi)" % e)
        log("keyboard not found (%s)" % e)
        kbd = wait_reconnect()
        if kbd is None:                  # stopped by a signal while waiting
            return
    log("connected: %s" % kbd.dev.get_product_string())
    daemon = not (config.status or config.once)
    try:
        if config.status:
            status(kbd, config)
        elif config.once:
            once(kbd, config)
        else:
            try:
                while not STOP.is_set():
                    apply_layout(kbd, config)
                    try:
                        loop_mod.run(kbd, config)
                        break
                    except OSError as e:
                        # the USB link died (re-enumeration, a driver glitch, unplugging)
                        log("the device disappeared (%s)" % e)
                        try:
                            kbd.close()
                        except Exception:
                            pass
                        kbd = wait_reconnect()
                        if kbd is None:  # stopped by a signal while waiting
                            break
            except KeyboardInterrupt:    # Ctrl+C: SIGINT is not overridden by a handler
                log("stopped by the user (Ctrl+C)")
    finally:
        if daemon:
            if loop_mod._stop_signal:
                log("stopped by the signal %s" % loop_mod._stop_signal)
            try:
                shutdown_widgets(kbd)    # turn off everything except the banner
            except Exception:
                pass
        try:
            kbd.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
