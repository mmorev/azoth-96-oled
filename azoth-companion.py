#!/usr/bin/env python3
"""
Azoth Companion v0.3 — a GearLink replacement prototype for the ASUS ROG Azoth 96 HE (M901).

The protocol model was taken from a live GearLink capture of 2026-10-04
(C:/azoth-capture/16 and 18, the breakdown is in README.md):

  • startup:          `65 FF` (wake) → optionally the `6A` slot enables +
                      `68` brightness + `50 55` commit (ONLY on a mask change);
  • values (no commit): `66` CPU usage/temp → slot 3,
                      `64` the PC battery % → slot 2,
                      `63` date+time → slot 1 (the device draws the dial itself);
  • slots: 0 = the banner/music mode (leave alone), 1 = the clock, 2 = the battery,
                      3 = the double indicator, 4 = the monitoring carousel;
  • GearLink polling: `12 01`/`12 00` every ~30 s, the `27` ping — not critical in v0.2.

Running:
  python azoth-companion.py                         # the banner (slot 0) only + the rocker OSD
  python azoth-companion.py --clock --battery --monitor
                                            # the classic GearLink set
  python azoth-companion.py --monitor --slideshow 3 # a slideshow of cpu.usage → ram.usage → cpu.freq
  python azoth-companion.py --monitor-items cpu.usage,gpu.temp,freq
                                            # a custom set (enables the slideshow, 2 s)
  python azoth-companion.py --monitor-items cpu.usage,temp --slideshow 5 --log-file
                                            # the same + mirror the log into logs/azoth-companion.log
  python azoth-companion.py --install-autostart     # start at logon (pythonw, the scheduler)
  python azoth-companion.py --uninstall-autostart   # remove the autostart task
  python azoth-companion.py --once --cpu 42 --bat 77   # a one-shot check
  python azoth-companion.py --status                # read the status only, change nothing
  python azoth-companion.py --demo --bat 42         # a sensor test: 0→100→0 (~5.5 s each way)

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

import argparse
import ctypes                  # MacVolume: CoreAudio (WindowsVolume imports it lazily)
import datetime as dt
import json
from collections.abc import Callable
from contextlib import suppress
import logging
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "analysis"))

from m901_client import M901  # noqa: E402  (requires pip install hidapi)
import m901_client  # noqa: E402

# The widget slots (confirmed by the GearLink mask in the live session of 2026-10-04:
# the minimal clock+battery+CPU configuration gave the mask [0,1,1,1,0])
SLOT_BANNER = 0       # the banner / music mode / a custom bitmap — leave alone
SLOT_CLOCK = 1        # the clock: the content = a 0x63 push
SLOT_BATTERY = 2      # the PC battery: the content = a push of 0x64 <percent>
SLOT_MONITOR = 3      # the DOUBLE indicator (two tiles): the content = a 0x66 push
SLOT_KPS = 4          # the native KPS tile (keys/s): the firmware draws it itself,
                      # the host only enables the slot (formerly the "carousel" — the
                      # "single tile" hypothesis was not confirmed, see README.md)

# The widgets = the slots, the flags --banner/--clock/--battery/--monitor/--kps.
# Without flags only the banner is enabled: it is static and does not go stale
# without the daemon, unlike the clock/battery/metrics (see shutdown_widgets).
WIDGET_FLAGS = (       # an argparse flag name → slot
    ("banner", SLOT_BANNER),
    ("clock", SLOT_CLOCK),
    ("battery", SLOT_BATTERY),
    ("monitor", SLOT_MONITOR),
    ("kps", SLOT_KPS),
)
WIDGET_NAMES = {SLOT_BANNER: "banner", SLOT_CLOCK: "clock",
                SLOT_BATTERY: "battery", SLOT_MONITOR: "monitor",
                SLOT_KPS: "KPS"}
DEFAULT_SLOTS = (SLOT_BANNER,)   # the set when started without flags

CLOCK_SYNC_S = 60.0     # not used for a timer: the clock is synced
                        # at the boundary of every minute (see run)
HEARTBEAT_S = 10.0      # the display falls asleep after ~30 idle ticks: we push
                        # the values unconditionally every 10 s to keep the screen
                        # alive (otherwise a NAK + blinking after every wake-up)
SWIPE_PAUSE_S = 5.0     # after a manual swipe the auto-paging pauses
WAKE_EVERY_S = 60.0
STAT_EVERY_S = 30.0

# The slideshow slides (v0.3): the "source.metric" grid from the GearLink config —
# it is also the nibble layout of the 0x66 selector (PROTOCOL_OLED.md §10.5):
# the hi-nibble = the tile header {0=CPU, 1=GPU, 2=VRM, 3=DRAM, 4=CHA},
# the lo-nibble = the value label {0=Usage, 1=Temp., 2=Freq., 3/4=Fan, 5=Volt}.
# GearLink only configures cpu/gpu/ram × usage/temp/volt/freq —
# VRM/CHA exist in the firmware but are absent from its grid, we don't set them.
SLIDE_SOURCES = {"cpu": 0x0, "gpu": 0x1, "ram": 0x3}      # "ram" = the DRAM header
SLIDE_METRICS = {"usage": 0x0, "temp": 0x1, "freq": 0x2, "fan": 0x3, "volt": 0x5}
SLIDE_ALIASES = {   # the short first-draft v0.3 names → the canonical ones
    "cpu": "cpu.usage", "gpu": "gpu.usage", "ram": "ram.usage",
    "usage": "cpu.usage", "temp": "cpu.temp", "freq": "cpu.freq",
    "fan": "cpu.fan", "volt": "cpu.volt",
}
DEFAULT_SLIDES = ("cpu.usage", "ram.usage", "cpu.freq")   # the v0.2 set: CPU0 Usage /
                                                          # DRAM0 Usage (RAM) / CPU0 Freq
DEFAULT_SLIDESHOW_S = 2.0                 # the period when --monitor-items is given without --slideshow
AUTOSTART_TASK = "Azoth Companion"                # the scheduler task name (the current user)
LOG_MAX_BYTES = 2 * 1024 * 1024           # the log file rotation: ~2 MB
LOG_BACKUPS = 2                           # 3 files in total: azoth-companion.log, .1, .2

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


def slide_sel(spec: str) -> int:
    """The 0x66 tile selector from a "source.metric" slide name."""
    src, met = spec.split(".", 1)
    return (SLIDE_SOURCES[src] << 4) | SLIDE_METRICS[met]

# Registering the autostart task via XML without admin rights (see install_autostart):
# the LogonTrigger is limited to the current user — a regular user is allowed
# this, unlike `schtasks /sc onlogon` (the "any logon" trigger).
AUTOSTART_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Azoth Companion — the OLED daemon for the ROG Azoth 96 HE (a GearLink replacement): slideshow + volume rocker OSD.</Description>
    <URI>\\Azoth Companion</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%(user)s</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(cmd)s</Command>
      <Arguments>%(args)s</Arguments>
    </Exec>
  </Actions>
</Task>
"""

_FILE_LOG = None      # the file mirror of the log (a Logger, created by setup_log_file)


def log(msg: str) -> None:
    t = time.time()
    line = (time.strftime("[%H:%M:%S.", time.localtime(t))
            + "%03d] " % (int(t * 1000) % 1000) + msg)
    print(line, flush=True)   # under pythonw stdout=None — print silently skips
    if _FILE_LOG is not None:
        _FILE_LOG.info(line)


def setup_log_file(path: str) -> None:
    """A file mirror of the log: the same lines as stdout (not a redirection!),
    UTF-8, size rotation (the standard RotatingFileHandler). An empty string
    = the --log-file flag without a value: logs/azoth-companion.log next to azoth-companion.py
    (the directory is created). A relative PATH is resolved from the CWD."""
    global _FILE_LOG
    if not path:
        path = str(Path(__file__).resolve().parent / "logs" / "azoth-companion.log")
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(p), maxBytes=LOG_MAX_BYTES,
                                      backupCount=LOG_BACKUPS, encoding="utf-8")
    except OSError as e:
        print("the log file %s is unavailable (%s) — writing to stdout only" % (p, e), flush=True)
        return
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("azoth-companion.file")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(handler)
    _FILE_LOG = lg
    log("log file: %s (rotation %.0f MB × %d files)"
        % (p, LOG_MAX_BYTES / 1048576, LOG_BACKUPS + 1))


def pair_label(sel: int, digit: int) -> str:
    hdr = {0: "CPU", 1: "GPU", 2: "VRM", 3: "DRAM", 4: "CHA"}.get((sel >> 4) & 0xF, "?")
    val = {0: "Usage", 1: "Temp", 2: "Freq", 3: "Fan", 4: "Fan", 5: "Volt"}.get(sel & 0xF, "?")
    return "%s%s %s" % (hdr, "" if digit == 0xFF else digit, val)


class HostSensors:
    """The host metrics: psutil is required when possible; WMI (LibreHardwareMonitor/
    OpenHardwareMonitor) — for the temperature/voltage/clock/load sensors of CPU/GPU/RAM."""

    LHM_NS = ("root\\LibreHardwareMonitor", "root\\OpenHardwareMonitor")

    def __init__(self):
        try:
            import psutil
            self._p = psutil
            psutil.cpu_percent(interval=None)  # prime: the next call gives the delta
        except ImportError:
            self._p = None
        self._wmi = None
        self._wmi_dead = False    # the LHM subsystem as a whole (import/namespace)
        self._dead = set()        # "sensor not found" — one warning per key
        self._mm = None           # the last macmon JSON (macOS, background reader)
        self._mm_reader = None   # the macmon pipe reader thread (started lazily)
        self._mm_dead = False     # macmon is not in PATH / does not run

    def cpu_load(self) -> int | None:
        return None if self._p is None else int(round(self._p.cpu_percent(interval=None)))

    def ram_load(self) -> int | None:
        return None if self._p is None else int(round(self._p.virtual_memory().percent))

    def battery(self):
        if self._p is None or not hasattr(self._p, "sensors_battery"):
            return None
        return self._p.sensors_battery()

    def _lhm_connect(self, what: str):
        """self._wmi or None; the connection is lazy, a failure — a single warning."""
        if sys.platform != "win32":
            self._wmi_dead = True     # LHM/WMI is Windows-only: on macOS — macmon
            return None
        if self._wmi is not None:
            return self._wmi
        if self._wmi_dead:
            return None
        try:
            import wmi
        except ImportError:
            self._wmi_dead = True
            log("WMI sensors unavailable: pip install wmi + run "
                "LibreHardwareMonitor.exe (%s will be missing)" % what)
            return None
        for ns in self.LHM_NS:
            try:
                self._wmi = wmi.WMI(namespace=ns)
                return self._wmi
            except Exception:
                continue
        self._wmi_dead = True
        log("the LibreHardwareMonitor WMI namespace not found — run "
            "LibreHardwareMonitor.exe (%s will be missing)" % what)
        return None

    def _lhm_pick(self, key: str, stype: str, patterns: tuple[str, ...],
                  what: str, fallback_any: bool = False,
                  scale: float = 1.0) -> int | None:
        """The first LibreHardwareMonitor sensor of type `stype` whose name (in
        lower case) contains at least one of `patterns` (the order = the name
        priority). Subsystem/sensor unavailable → None, one warning per `key`.
        fallback_any — if none of the pattern names exist at all, take the
        first sensor of the type (some boards call Vcore "Voltage #N")."""
        if key in self._dead:
            return None
        w = self._lhm_connect(what)
        if w is None:
            self._dead.add(key)
            return None
        try:
            rows = w.query("SELECT Name, Value FROM Sensor WHERE SensorType='%s'"
                           % stype)
        except Exception:
            self._dead.add(key)
            log("the \"%s\" sensor query failed — the value is skipped" % what)
            return None
        vals = [(str(r.Name or "").lower(), float(r.Value))
                for r in rows if r.Value is not None]
        for pat in patterns:
            for name, v in vals:
                if pat in name:
                    return int(round(v * scale))
        if fallback_any and vals:
            return int(round(vals[0][1] * scale))
        self._dead.add(key)
        log("the sensor \"%s\" not found (LibreHardwareMonitor: a Type='%s' sensor "
            "with a name containing \"%s\") — the slide will be skipped" % (what, stype, patterns[0]))
        return None

    def _macmon_reader(self) -> None:
        """The background `macmon pipe` reader (no -s = an infinite stream,
        ~5 Hz): we keep the last parsed frame. Spawn-per-query with -s 1
        (run+timeout=3) cost 0.9–1.6 s per cold call — the daemon loop period
        wandered 1–2.6 s: a ragged slideshow rhythm and a swipe processed
        with up to a full period delay. A broken stream (a brew update) —
        up to 3 empty attempts, then give up (the slides are skipped
        normally)."""
        tries = 0
        while tries < 3:
            got = False
            with suppress(Exception):
                proc = subprocess.Popen(["macmon", "pipe", "-i", "200"],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
                for line in proc.stdout or ():
                    got = True
                    try:
                        self._mm = json.loads(line)
                    except ValueError:
                        continue         # a partial/broken frame — wait for the next one
            tries = 0 if got else tries + 1
            if not got:
                time.sleep(1.0)          # the stream gave no frames — retry, then give up
        self._mm_dead = True
        if self._mm is None:
            log("macmon unavailable — the temp/freq/gpu slides on macOS will be "
                "skipped (brew install macmon)")

    def _macmon(self) -> dict | None:
        """macOS: the macmon JSON (brew install macmon, sudoless IOReport) —
        the CPU/GPU temperatures, the real cluster frequency, the gpu usage,
        the fans. Not in PATH / did not run → None (the slides are skipped
        normally) + a single warning. On other OSes — None (LHM/psutil
        there)."""
        if sys.platform != "darwin":
            return None
        if self._mm_dead:
            return None
        if self._mm_reader is None:
            self._mm_reader = threading.Thread(target=self._macmon_reader,
                                               daemon=True)
            self._mm_reader.start()
            time.sleep(1.2)              # let the first frame arrive (~0.9 s sample)
        return self._mm

    def _macmon_val(self, path: list, scale: float = 1.0) -> int | None:
        """A number from the macmon JSON by the key path, or None."""
        m = self._macmon()
        if m is None:
            return None
        v: object = m
        for k in path:
            if isinstance(v, dict):
                v = v.get(k)
            elif isinstance(v, list) and isinstance(k, int) and k < len(v):
                v = v[k]            # macmon: fans[0].rpm
            else:
                v = None
            if v is None:
                return None
        try:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(round(v * scale))
        except (TypeError, ValueError):
            pass
        return None

    # --- the canonical slide value providers ---
    def cpu_temp(self) -> int | None:
        """The CPU package °C: macOS — macmon (cpu_temp_avg); Windows — the LHM
        Temperature ("CPU Package"); ACPI thermal zones are not counted — the
        test machine has none (verified)."""
        t = self._macmon_val(["temp", "cpu_temp_avg"])
        if t is not None:
            return t
        return self._lhm_pick("cpu.temp", "Temperature",
                              ("cpu package", "package", "cpu"),
                              "the CPU temperature (Package)")

    def cpu_volt(self) -> int | None:
        """The CPU voltage in millivolts: the LHM Voltage ("Vcore"/"CPU").
        Volt = mV — confirmed by capture 12 (PROTOCOL_OLED.md §10.5)."""
        return self._lhm_pick("cpu.volt", "Voltage", ("vcore", "cpu"),
                              "the CPU voltage (Vcore)", fallback_any=True,
                              scale=1000)

    def cpu_freq_mhz(self) -> int:
        """The current CPU frequency in MHz: macOS — macmon (pcpu_freq_mhz, the
        real P-cluster frequencies); otherwise psutil (on AS it returns the
        base one). On error 0 (never None)."""
        v = self._macmon_val(["pcpu_freq_mhz"])
        if v:
            return v
        if self._p is None:                    # no psutil (macmon did not help either)
            return 0
        try:
            f = self._p.cpu_freq()
            return int(round(f.current)) if f and f.current else 0
        except Exception:
            return 0

    def gpu_usage(self) -> int | None:
        """The GPU load %: macOS — macmon (gpu_active_ratio); Windows — the LHM
        Load ("GPU Core", otherwise any GPU sensor)."""
        u = self._macmon_val(["gpu_active_ratio"], scale=100)
        if u is not None:
            return u
        return self._lhm_pick("gpu.usage", "Load", ("gpu core", "gpu"),
                              "the GPU load")

    def gpu_temp(self) -> int | None:
        """The GPU °C: macOS — macmon (gpu_temp_avg); Windows — the LHM
        Temperature ("GPU Core", "Hot Spot", any GPU)."""
        t = self._macmon_val(["temp", "gpu_temp_avg"])
        if t is not None:
            return t
        return self._lhm_pick("gpu.temp", "Temperature",
                              ("gpu core", "hot spot", "gpu"), "the GPU temperature")

    def gpu_freq(self) -> int | None:
        """The GPU MHz: macOS — macmon (gpu_freq_mhz); Windows — the LHM Clock
        ("GPU Core")."""
        f = self._macmon_val(["gpu_freq_mhz"])
        if f is not None:
            return f
        return self._lhm_pick("gpu.freq", "Clock", ("gpu core", "gpu"),
                              "the GPU frequency")

    def fan_rpm(self) -> int | None:
        """The main fan RPM (§10.3.1: the Fan tile, the 1000/3500 gauge):
        macOS — macmon (fans[0].rpm); Linux — psutil.sensors_fans; Windows —
        the LHM Type='Fan' ("CPU", otherwise any). 0 RPM (silent operation)
        is a valid value; fanless/no sensor → None, the slide will be
        skipped."""
        r = self._macmon_val(["fans", 0, "rpm"])
        if r is not None:
            return r
        if sys.platform == "darwin":
            return None             # macmon present, no fans (an Air)
        if self._p is not None and hasattr(self._p, "sensors_fans"):
            with suppress(Exception):      # no sensors/platform — a skip, not worth logging
                for fans in self._p.sensors_fans().values():
                    if fans:
                        return int(fans[0].current)
        return self._lhm_pick("cpu.fan", "Fan", ("cpu", "fan"),
                              "the CPU fan RPM", fallback_any=True)

    def gpu_volt(self) -> int | None:
        """The GPU mV: the LHM Voltage ("GPU Core"), V → mV."""
        return self._lhm_pick("gpu.volt", "Voltage", ("gpu core", "gpu"),
                              "the GPU voltage", scale=1000)

    def ram_temp(self) -> int | None:
        """The memory °C: the LHM Temperature (SODIMM/DIMM/Memory) — platform-dependent."""
        return self._lhm_pick("ram.temp", "Temperature",
                              ("sodimm", "dimm", "memory", "ram"),
                              "the RAM temperature (DIMM)")

    def ram_freq(self) -> int | None:
        """The memory MHz: the LHM Clock ("Memory Clock") — platform-dependent."""
        return self._lhm_pick("ram.freq", "Clock",
                              ("memory clock", "memory", "dram"), "the RAM frequency")

    def ram_volt(self) -> int | None:
        """The memory mV: the LHM Voltage (DIMM/DRAM/VDDCR) — platform-dependent."""
        return self._lhm_pick("ram.volt", "Voltage",
                              ("dimm", "dram", "vddr", "memory"),
                              "the RAM voltage (DIMM)")


def sync_clock(kbd: M901) -> None:
    t = dt.datetime.now()
    ok = kbd.set_clock(t.year, t.month, t.day, t.hour, t.minute)
    log("time %s → 0x63 %s" % (t.strftime("%Y-%m-%d %H:%M"),
                                "ok" if ok else "NO REPLY"))


def enabled_slots(args) -> list[int]:
    """The widget slots per the --banner/--clock/--battery/--monitor/--kps flags;
    without flags — the banner only (DEFAULT_SLOTS). The set is never empty:
    the OLED requires at least one enabled widget."""
    slots = [slot for flag, slot in WIDGET_FLAGS if getattr(args, flag)]
    return slots or list(DEFAULT_SLOTS)


def apply_layout(kbd: M901, args) -> None:
    """The startup batch exactly like GearLink's (capture 18):
    65 FF → if needed the 6A mask + 68 + 50 55 → a push of the values.
    The widget set = the flags (--banner/--clock/…): exactly it gets enabled,
    the slots outside the set are turned off — the daemon is the single owner
    of the layout. THE ORDER MATTERS: the firmware validates every 6A against
    the CURRENT mask ("at least one widget must remain"), so the enables go
    before the disables — otherwise banner-off in a set without a banner is
    silently ignored (there is an ACK, the bit does not change; verified
    2026-10-05)."""
    kbd.wake_display(0xFF)               # a GearLink-style wake (not mode 0!)
    enabled = enabled_slots(args)
    start = args.start if args.start is not None else (
        SLOT_MONITOR if SLOT_MONITOR in enabled else enabled[0])
    mask = kbd.get_status_flags()
    changed = False
    for slot in enabled:                 # the enables first
        if mask is None or not mask[slot]:
            if not kbd.set_widget(slot, True):           # 6A 00 <slot> 01
                log("warning: slot %d did not acknowledge enabling" % slot)
            changed = True
    for slot in range(5):                # …then disabling the extras
        if slot in enabled or not (mask is None or mask[slot]):
            continue
        if not kbd.set_widget(slot, False):              # 6A 00 <slot> 00
            log("warning: slot %d did not acknowledge disabling" % slot)
        changed = True
    if changed:
        log("widget mask: %s → set %s" % (mask, enabled))
    if args.brightness is not None:
        kbd.set_brightness(args.brightness)          # 68 00 00 00 <v>
        changed = True
    if changed:
        kbd.commit()                    # 50 55 — only after a mask/brightness change
    kbd.select_slot(start)              # 6A 01 <slot>
    kbd.commit()
    log("layout: %s; start=slot %d (%s), mask=%s"
        % ("+".join(WIDGET_NAMES[s] for s in enabled), start,
           WIDGET_NAMES[start], kbd.get_status_flags()))


def demo_value(half: float = 5.5, phase: float = 0.0) -> int:
    """A 0→100→0 triangle, a half-period of half s — a test "running" sensor."""
    ph = ((time.monotonic() + phase) % (2 * half)) / half   # 0..2
    return int(round(100 * (ph if ph <= 1.0 else 2.0 - ph)))


def resolve_pairs(args, sensors: HostSensors) -> list[tuple[int, int, int]]:
    """The 0x66 pairs for the --metrics mode. The manual values (--cpu etc.)
    take priority."""
    if args.demo:
        pairs = [(0x00, 0, demo_value())]
        if args.metrics == "cpu-ram" or (args.metrics == "cpu-temp"
                                         and args.temp is None):
            # The second tile — RAM as "DRAM0 Usage" (the live test of the
            # hypothesis): a neutral empty pair looks like "CPU Usage 0" on
            # slot 3.
            pairs.append((0x30, 0, demo_value(half=7.0, phase=2.5)))
        elif args.metrics == "cpu-temp":
            pairs.append((0x01, 0, args.temp))
        return pairs
    cpu = args.cpu if args.cpu is not None else sensors.cpu_load()
    if cpu is None:
        raise SystemExit("no CPU data: pip install psutil or set --cpu")
    if args.metrics == "cpu-temp":
        temp = args.temp if args.temp is not None else sensors.cpu_temp()
        if temp is not None:
            return [(0x00, 0, cpu), (0x01, 0, temp)]
        return [(0x00, 0, cpu)]          # a fallback: a single Usage (like GearLink)
    if args.metrics == "cpu-ram":
        ram = args.ram_val if args.ram_val is not None else sensors.ram_load()
        if ram is None:
            raise SystemExit("no RAM data: pip install psutil or set --ram-val")
        return [(0x00, 0, cpu), (0x30, 0, ram)]      # "DRAM0 Usage" — the hypothesis
    return [(0x00, 0, cpu)]


def resolve_battery(args, sensors: HostSensors) -> int | None:
    if args.bat is not None:
        return args.bat
    bat = sensors.battery()
    if bat is None:
        return None
    return int(round(bat.percent))


def pairs_differ(a, b) -> bool:
    if a is None or b is None or len(a) != len(b):
        return True
    return any(x[0] != y[0] or abs(x[2] - y[2]) >= 1 for x, y in zip(a, b))


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


def robust_push(kbd: M901, fn, *args) -> bool:
    """A self-healing push, by the failure mode:
    • a real NAK (`FF AA`) + the screen EXPLICITLY asleep (`0x23/02 == 0`) →
      wake with `65 FF` + `69` and retry (a short black frame — only when
      needed);
    • a NAK with the screen on (the rocker OSD/a gesture monopolized the
      path) or a timeout/silence → a quiet retry after 0.3 s, no wake-up —
      otherwise the first rocker burst blinked the whole series (live test
      2026-10-05)."""
    if fn(*args):
        return True
    if getattr(kbd, "last_nak", False) and not kbd.screen_is_on():
        kbd.wake_display(0xFF)
        kbd.screen_on(True)
        time.sleep(0.2)
        ok = fn(*args)
        if ok:
            log("the screen was asleep — woke it and retried")
        return ok
    time.sleep(0.3)
    return fn(*args)


def open_ffc0(kbd: M901):
    """The 0xFFC0 channel (Col03 of iface2): the 03 93/95/96 event mirror +
    the 03 71 touch. Each `03 96` = an accepted vertical swipe (empirical
    2026-10-04)."""
    try:
        return kbd.open_ffc0()
    except Exception as e:
        log("the 0xFFC0 channel unavailable (%s) — the swipes will not page" % e)
        return None


def start_ffc0_reader(kbd, on_tick=None):
    """A background thread: continuously reads 0xFFC0 (a blocking read) and
    puts the 03-xx frames into a queue. A drain from the main loop LOST the
    reports: Windows-HID does not accumulate input reports without a pending
    read, and with one it delivers to every open handle. The rocker ticks
    (03 72 01/04) are forwarded to `on_tick(up)` immediately — the OSD does
    not wait for the main loop."""
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
                d = bytes(data)
                q.put(d)
                # The volume ticks (01 = vol+, 04 = vol−) open the OSD window;
                # a press of 02 does NOT open the window — it goes to the
                # worker only as a mark that "the unmute came from the rocker"
                # (a press = the mute toggle; pushing the window on press gave
                # a level OSD while muted — a regression of 2026-10-05).
                # The release 00 is not needed by anyone.
                if on_tick and len(d) >= 3 and d[0] == 0x03 and d[1] == 0x72 \
                        and d[2] in (0x01, 0x02, 0x04):
                    on_tick(d[2])

    threading.Thread(target=reader, daemon=True).start()
    return q


def poll_ffc0(ffc0, dump: bool = False) -> tuple[int, int]:
    """Drain the accumulated frames from the queue. Returns (the 03 96 swipes,
    the 03 72 01/04 rocker ticks)."""
    swipes = ticks = 0
    while True:
        try:
            d = ffc0.get_nowait()
        except queue.Empty:
            break
        if dump:
            log("ffc0: %s" % d[:20].hex(" "))
        if d[0] == 0x03 and len(d) >= 3:
            if d[1] == 0x96:
                swipes += 1
            elif d[1] == 0x72 and d[2] in (0x01, 0x04):
                # the rocker: 01 = vol+, 04 = vol− (00 = a release, not a tick)
                ticks += 1
            elif d[1] == 0x94:
                # in capture 20 GearLink saw these twice; if they appear —
                # we want to know about it (a possible swipe "neighbor")
                log("event 0394: %s" % d[:10].hex(" "))
    return swipes, ticks


def parse_monitor_items(spec: str | None) -> list[str]:
    """--monitor-items "cpu.usage,gpu.temp,…" → a validated list of canonical
    names. The "source.metric" format — the GearLink config grid
    (PROTOCOL_OLED.md §10.5): the sources cpu/gpu/ram, the metrics
    usage/temp/freq/volt. The short first-draft names (cpu, ram, temp, …)
    are accepted as aliases. An unknown name and an empty list are a startup
    error; duplicates collapse, the order is preserved; None (the flag not
    given) → the default set."""
    if spec is None:
        return list(DEFAULT_SLIDES)
    names, seen = [], set()
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        token = SLIDE_ALIASES.get(token, token)
        parts = token.split(".")
        if (len(parts) != 2 or parts[0] not in SLIDE_SOURCES
                or parts[1] not in SLIDE_METRICS):
            raise SystemExit(
                "--monitor-items: unknown name \"%s\"; the format is "
                "\"source.metric\": the sources %s, the metrics %s; the short "
                "names (%s) are accepted too"
                % (raw.strip(), "/".join(SLIDE_SOURCES),
                   "/".join(SLIDE_METRICS), ", ".join(SLIDE_ALIASES)))
        if token not in seen:
            seen.add(token)
            names.append(token)
    if not names:
        raise SystemExit("--monitor-items: an empty list — give at least one "
                         "name, e.g. cpu.usage,ram.usage,gpu.temp")
    return names


def slide_value(spec: str, args, sensors: HostSensors) -> int | None:
    """The value of a "source.metric" slide in the 0x66 tile units
    (Usage=%, Temp=°C, Freq=MHz, Fan=RPM, Volt=mV — PROTOCOL_OLED.md §10.5);
    None = the slide is skipped (the sensor unavailable — a single warning,
    see HostSensors._lhm_pick). The manual --cpu/--temp/--ram-val take
    priority. cpu.usage/ram.usage without psutil — a stop; everything else
    sensor-based — LibreHardwareMonitor, "ram" on screen = the "DRAM0"
    header."""
    src, met = spec.split(".", 1)
    if met == "usage":
        if src == "gpu":
            return sensors.gpu_usage()
        if src == "ram":
            v = args.ram_val if args.ram_val is not None else sensors.ram_load()
            if v is None:
                log("no RAM data (the ram.usage slide): pip install psutil "
                    "or set --ram-val")
                raise SystemExit(1)
            return v
        v = args.cpu if args.cpu is not None else sensors.cpu_load()
        if v is None:
            log("no CPU data (the cpu.usage slide): pip install psutil "
                "or set --cpu")
            raise SystemExit(1)
        return v
    if src == "cpu":
        if met == "temp":
            return args.temp if args.temp is not None else sensors.cpu_temp()
        if met == "volt":
            return sensors.cpu_volt()
        if met == "fan":
            return sensors.fan_rpm()
        return sensors.cpu_freq_mhz()                  # cpu.freq
    if src == "gpu":
        return {"temp": sensors.gpu_temp, "freq": sensors.gpu_freq,
                "volt": sensors.gpu_volt}[met]()
    return {"temp": sensors.ram_temp, "freq": sensors.ram_freq,
            "volt": sensors.ram_volt}[met]()           # ram


def _positive_float(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number: %r" % text)
    if v <= 0:
        raise argparse.ArgumentTypeError(
            "a positive number is required, got %s" % text)
    return v


class WindowsVolume:
    """The Windows master output volume via IAudioEndpointVolume
    (pure ctypes, no dependencies). The rocker sends consumer events —
    the OS changes the volume itself; all we have to do is READ the new
    value and mirror it onto the keyboard OSD (`51 0C`, like GearLink,
    PROTOCOL_VOLUME.md §0-§1)."""

    def __init__(self):
        self._ep = None
        self._dead = False
        self._cache = None      # the value from the background poller
        self._cache_t = 0.0
        self._muted = None      # None = not known yet
        self.on_unmute: Callable[[], None] | None = None   # the "mute cleared" callback (a level push to the OSD)
        self._stop = threading.Event()
        self._init_com()

    def _init_com(self):
        import ctypes
        from ctypes import byref, cast, c_float, c_long, c_uint32, c_void_p, POINTER

        self._ct = ctypes
        self._byref, self._cast = byref, cast
        self._c_float, self._c_void_p = c_float, c_void_p
        self._POINTER = POINTER

        class _GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        def guid(s):
            h = s.strip("{}").replace("-", "")
            return _GUID(int(h[:8], 16), int(h[8:12], 16), int(h[12:16], 16),
                         (ctypes.c_ubyte * 8)(
                             *[int(h[16 + 2 * i:18 + 2 * i], 16) for i in range(8)]))

        self._CLSID_enum = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        self._IID_enum = guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        self._IID_epvol = guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        # the COM method prototypes (the vtable indexes are in the calls below)
        self._P_EP = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_long,
                                        c_long, POINTER(c_void_p))
        self._P_ACT = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(_GUID),
                                         c_uint32, c_void_p, POINTER(c_void_p))
        self._P_F = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(c_float))
        self._P_M = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(ctypes.c_int))
        ctypes.oledll.ole32.CoInitializeEx(None, 0)
        self._GUID_T = _GUID

    def _open(self) -> bool:
        ct, byref, c_void_p = self._ct, self._byref, self._c_void_p
        try:
            enum, dev = c_void_p(), c_void_p()
            ct.oledll.ole32.CoCreateInstance(byref(self._CLSID_enum), None, 1,
                                             byref(self._IID_enum), byref(enum))
            self._call(enum, 4, self._P_EP, 0, 1, byref(dev))   # GetDefaultAudioEndpoint
            ep = c_void_p()
            self._call(dev, 3, self._P_ACT, byref(self._IID_epvol), 1, None,
                       byref(ep))                               # Activate
            self._ep = ep
            return True
        except Exception:
            return False

    def _call(self, obj, idx, proto, *args):
        """Call a COM object method by the vtable index."""
        vt = self._cast(self._cast(obj, self._POINTER(self._c_void_p)).contents,
                        self._POINTER(self._c_void_p))
        fn = self._ct.cast(vt[idx], proto)
        return fn(obj, *args)

    def percent(self) -> int | None:
        """0-100 or None (no audio device). A fresh cache from the background
        poller is returned instantly, otherwise a direct COM call (~2 ms)."""
        if self._cache is not None and time.monotonic() - self._cache_t < 0.5:
            return self._cache
        if self._dead:
            return None
        for attempt in (1, 2):
            if self._ep is None and not self._open():
                break
            v = self._c_float()
            hr = self._call(self._ep, 9, self._P_F, self._byref(v))
            if hr == 0:
                self._cache = max(0, min(100, int(round(v.value * 100))))
                self._cache_t = time.monotonic()
                return self._cache
            self._ep = None        # the device may have been recreated — reopen
        return None

    def fresh(self) -> int | None:
        """The exact value straight from the OS, bypassing the cache (for the
        "settle" push)."""
        if self._dead:
            return None
        for _ in (1, 2):
            if self._ep is None and not self._open():
                break
            v = self._c_float()
            hr = self._call(self._ep, 9, self._P_F, self._byref(v))
            if hr == 0:
                val = max(0, min(100, int(round(v.value * 100))))
                self._cache, self._cache_t = val, time.monotonic()
                return val
            self._ep = None
        return None

    def nudge(self, v: int) -> None:
        """The cache is given a predicted value (after a push on a rocker tick)."""
        self._cache = max(0, min(100, int(v)))
        self._cache_t = time.monotonic()

    def muted(self) -> bool | None:
        """The mute state (GetMute, vtable 15) or None."""
        if self._dead or self._ep is None:
            return None
        m = self._ct.c_int()
        hr = self._call(self._ep, 15, self._P_M, self._byref(m))
        return None if hr != 0 else bool(m.value)

    def start_poller(self, interval: float = 0.1) -> None:
        """Background: keep the volume "at hand" (a ~10 Hz cache) so the OSD
        push does not wait for either a COM call or the main loop; along the
        way catch the mute→unmute transition and call on_unmute (a level OSD
        after Unmute)."""
        self._cache_t = 0.0
        self._cache = None

        def poll():
            while not self._stop.is_set():
                try:
                    self.percent()
                    m = self.muted()
                    if m is not None:
                        if self._muted and not m and self.on_unmute:
                            try:
                                self.on_unmute()
                            except Exception:
                                pass
                        self._muted = m
                except Exception:
                    pass
                self._stop.wait(interval)

        self._stop.clear()
        threading.Thread(target=poll, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


class MacVolume:
    """The macOS master output volume via CoreAudio (pure ctypes, no
    dependencies). The interface is 1:1 with WindowsVolume:
    percent/fresh/nudge/muted/start_poller/stop/on_unmute. Every value is
    read directly — a full cycle (default device + volume + mute) is
    ~0.07 ms, the cache and the poller are like on Windows. macOS 26 changed
    the fourcc selectors ('defa'→'dOut', volume → 'volm'), the old ones
    return 'who?', so every selector is a chain of new + legacy, the working
    one is cached on the first success."""

    _GLOB = 0x676C6F62          # 'glob'
    _OUTP = 0x6F757470          # 'outp'

    def __init__(self):
        self._ca = None
        self._sel = {}          # a property key → the working fourcc (the 1st success)
        self._cache = None
        self._cache_t = 0.0
        self._muted_live = None
        self._muted = None      # the previous state — the unmute detector in the poller
        self.on_unmute: Callable[[], None] | None = None
        self._stop = threading.Event()
        try:
            import ctypes
            self._ct = ctypes
            self._ca = ctypes.CDLL(
                "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            self._ca.AudioObjectGetPropertyData.restype = ctypes.c_uint32
            self._ca.AudioObjectGetPropertyData.argtypes = [
                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p]
        except Exception as e:
            log("CoreAudio unavailable (%s) — the volume OSD will not work" % e)

    class _AOPA(ctypes.Structure):
        _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                    ("elem", ctypes.c_uint32)]

    def _get(self, key: str, fourccs: tuple[int, ...], oid: int, scope: int,
             typ) -> int | float | None:
        """An object property over a chain of selectors (macOS 26+ / legacy).
        The working fourcc is cached — then a single CA call."""
        if self._ca is None:
            return None
        ct = self._ct
        sel = self._sel.get(key)
        for f in ([sel] if sel is not None else fourccs):
            addr = self._AOPA(f, scope, 0)
            v = typ()
            sz = ct.c_uint32(ct.sizeof(typ))
            st = self._ca.AudioObjectGetPropertyData(
                oid, ctypes.byref(addr), 0, None, ctypes.byref(sz),
                ctypes.byref(v))
            if st == 0:
                self._sel[key] = f
                return v.value
        return None

    def _read(self):
        """(vol 0..1, muted) or None. The default device — on every request:
        on an output switch the id changes, the lookup is dirt cheap."""
        dev = self._get("dev", (0x644F7574, 0x64656661),   # 'dOut' / 'defa'
                        1, self._GLOB, self._ct.c_uint32)
        if dev is None:
            return None
        vol = self._get("vol", (0x766F6C6D, 0x766F6C75),   # 'volm' / 'volu'
                        int(dev), self._OUTP, self._ct.c_float)
        if vol is None:
            return None
        mute = self._get("mute", (0x6D757465,),            # 'mute'
                         int(dev), self._OUTP, self._ct.c_uint32)
        return vol, bool(mute)

    def fresh(self) -> int | None:
        r = self._read()
        if r is None:
            return None
        self._cache = max(0, min(100, int(round(r[0] * 100))))
        self._cache_t = time.monotonic()
        self._muted_live = r[1]
        return self._cache

    def percent(self) -> int | None:
        if self._cache is not None and time.monotonic() - self._cache_t < 0.5:
            return self._cache
        return self.fresh()

    def nudge(self, v: int) -> None:
        self._cache = max(0, min(100, int(v)))
        self._cache_t = time.monotonic()

    def muted(self) -> bool | None:
        return self._muted_live

    def start_poller(self, interval: float = 0.1) -> None:
        self._cache_t = 0.0
        self._cache = None

        def poll():
            while not self._stop.is_set():
                try:
                    # fresh(), not percent(): the volume cache lives 0.5 s, while
                    # _muted_live is updated only by fresh — with percent() the
                    # mute transition was caught once per 0.5+ s (the main
                    # unmute-push lag; a full read cycle is ~0.07 ms — no cache
                    # needed here).
                    self.fresh()
                    m = self.muted()
                    if m is not None:
                        if self._muted and not m and self.on_unmute:
                            try:
                                self.on_unmute()
                            except Exception:
                                pass
                        self._muted = m
                except Exception:
                    pass
                self._stop.wait(interval)

        self._stop.clear()
        threading.Thread(target=poll, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


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
            log("--vol-hz %g exceeds the OSD threshold (%g Hz) — capped "
                "(live calibration 2026-10-05: <50 ms = a freeze)" % (hz, self.MAX_HZ))
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
    sensors = HostSensors()
    enabled = enabled_slots(args)
    do_monitor = SLOT_MONITOR in enabled
    if args.evt_dump:
        # a transaction log: catch which exact exchange NAKs in the blink window
        m901_client.TXLOG = lambda cmd, sub, e, r, nak, ms: log(
            "tx %02X.%02X echo=%04X → %s (%.0f ms)" % (
                cmd, sub, e,
                "NAK" if nak else (r[:8].hex(" ") if r else "no-reply"), ms))
        start_gate_watcher(kbd)
    cons = open_events(kbd) if args.events else None
    volume = MacVolume() if sys.platform == "darwin" else WindowsVolume()
    # 50 ms: the unmute is caught by polling the state, the poll quantum = the push delay
    volume.start_poller(0.05)
    vol_worker = VolumeWorker(kbd, volume, hz=args.vol_hz)
    if not args.no_volume:
        vol_worker.start()
        volume.on_unmute = vol_worker.unmute_gate   # a push — only on a rocker unmute
    ffc0 = start_ffc0_reader(kbd, on_tick=vol_worker.tick if not args.no_volume else None)
    slides = args.monitor_items   # a validated non-empty list (parse_monitor_items in main)
    n_slides = len(slides)
    slide_warned = set()   # the temp/volt slides already warned about
    dead_slides = set()    # structurally nonexistent slides (gpu.fan etc.)
    last_pairs = None
    last_bat = None
    warn_bat = True
    slide_phase = 0
    pending_swipes = 0    # the swipes pulled out of the queue by the loop tail
                          # wakeup (get() removes the frame — without this
                          # counter they were lost and the swipe "did not work")
    hold_until = 0.0       # the moment of the next auto-page advance
                           # (monotonic; a swipe sets now + SWIPE_PAUSE_S —
                           # a swipe and an auto tick cannot coincide, no
                           # double jumps)
    last_min = None        # the minute of the last clock sync
    last_push_t = 0.0      # the heartbeat: an unconditional push every HEARTBEAT_S
    t_wake = t_stat = time.monotonic()
    if do_monitor:
        if args.slideshow:
            mode_desc = "a slideshow %.1f s [%s]" % (args.slideshow, ",".join(slides))
            log("slides: %s" % " → ".join("%s(0x%02X)" % (n, slide_sel(n))
                                          for n in slides))
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
    try:
        while not STOP.is_set():
            now = time.monotonic()
            # The 0xFFC0 drain is always needed (the queue must not grow); the
            # rocker ticks go to the VolumeWorker straight from the reader thread.
            swipes, _ticks = poll_ffc0(ffc0, dump=args.evt_dump) if ffc0 else (0, 0)
            swipes += pending_swipes
            pending_swipes = 0
            if do_monitor:
                if args.slideshow:
                    # The slideshow like GearLink's (§10.6): the tile is
                    # overwritten by a single push with the next selector.
                    # The auto-scroll is timer-driven; a swipe down (03 96)
                    # pages immediately and puts the auto-paging on pause.
                    # A swipe UP is not reported to the host by the firmware
                    # at all (a clean session of 2026-10-05: >10 up — zero
                    # events on both iface2 channels), so "back" is not
                    # implementable by the host — forward only, like GearLink.
                    if swipes:
                        slide_phase = (slide_phase + swipes) % n_slides
                        last_pairs = None
                        hold_until = now + SWIPE_PAUSE_S
                        log("swipe down → slide %d/%d \"%s\" (the auto-paging paused for %g s)"
                            % (slide_phase + 1, n_slides, slides[slide_phase],
                               SWIPE_PAUSE_S))
                    elif now >= hold_until:
                        slide_phase = (slide_phase + 1) % n_slides
                        last_pairs = None
                        hold_until = now + args.slideshow
                    # The value of the next slide; temp/volt without a sensor
                    # are skipped (a one-time warning), the auto-paging moves
                    # on. A structurally nonexistent slide (gpu.fan etc.) —
                    # one log message, then the slide is "dead"; if ALL are
                    # dead — the cpu.usage fallback (always available).
                    pairs = None
                    for _ in range(n_slides):
                        name = slides[slide_phase]
                        try:
                            val = slide_value(name, args, sensors)
                        except Exception as e:
                            val = None
                            if name not in dead_slides:
                                dead_slides.add(name)
                                log("the slide \"%s\": no such sensor (%s)"
                                    % (name, e))
                                if len(dead_slides) >= n_slides:
                                    log("all the slides are without sensors — the cpu.usage fallback")
                                    slides = ["cpu.usage"]
                                    n_slides = 1
                                    slide_phase = 0
                                    slide_warned.clear()
                                    last_pairs = None
                                    break
                            slide_phase = (slide_phase + 1) % n_slides
                            continue
                        if val is not None:
                            pairs = [(slide_sel(name), 0, val)]
                            break
                        if name not in slide_warned:
                            slide_warned.add(name)
                            need = ("macmon (brew install macmon)"
                                    if sys.platform == "darwin" else
                                    "a running LibreHardwareMonitor (pip install wmi)")
                            log("the slide \"%s\" is skipped: the sensor is unavailable — %s is needed"
                                % (name, need))
                        slide_phase = (slide_phase + 1) % n_slides
                else:
                    pairs = resolve_pairs(args, sensors)
                if pairs is not None and (
                        pairs_differ(pairs, last_pairs)
                        or now - last_push_t >= HEARTBEAT_S):
                    if robust_push(kbd, kbd.push_metrics, pairs):
                        log("the 0x66 push: " + ", ".join(
                            "%s=%d" % (pair_label(sel, dig), val)
                            for sel, dig, val in pairs))
                        last_pairs = pairs
                        last_push_t = now
                    else:
                        log("the 0x66 push not acknowledged (NAK/no reply)")
            if args.battery:
                pct = resolve_battery(args, sensors)
                if pct is None:
                    if warn_bat and args.bat is None:
                        log("the host battery not found — the battery widget is not pushed")
                        warn_bat = False
                elif last_bat is None or abs(pct - last_bat) >= 1:
                    if robust_push(kbd, kbd.set_slot2_value, pct, 0):
                        log("the PC battery → slot %d: %d%%" % (SLOT_BATTERY, pct))
                        last_bat = pct
            # The on-screen clock is static text from 0x63, it does not tick
            # by itself (confirmed 2026-10-04: the time hung over from the
            # previous run). Sync at the boundary of every minute.
            if args.clock and not args.no_clock:
                now_dt = dt.datetime.now()
                if last_min is None or (now_dt.second < 5 and now_dt.minute != last_min):
                    robust_push(kbd, lambda: sync_clock(kbd))
                    last_min = now_dt.minute
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
            # wait, not sleep: STOP wakes up immediately, without waiting out
            # the timeout. In the slideshow mode we sleep on the 0xFFC0 queue:
            # a swipe wakes the loop instantly (STOP.wait slept out the whole
            # period — the swipe was processed with a delay and landed right
            # next to the auto tick = a double jump). The timeout — until the
            # next auto-page advance; the 0395 heartbeat ~1 Hz keeps the
            # usual iteration rate. IMPORTANT: get() REMOVES the frame — a
            # swipe is counted into pending_swipes, otherwise it was lost.
            if args.slideshow and ffc0:
                delay = min(1.0, max(0.05, hold_until - time.monotonic()))
                with suppress(queue.Empty):
                    d = ffc0.get(timeout=delay)
                    if args.evt_dump:
                        log("ffc0: %s" % d[:20].hex(" "))
                    if len(d) >= 3 and d[0] == 0x03 and d[1] == 0x96:
                        pending_swipes += 1
            else:
                STOP.wait(1.0 if args.slideshow else (0.4 if args.demo else args.interval))
    finally:
        vol_worker.stop()
        volume.stop()
        if cons:
            cons.close()


def once(kbd: M901, args) -> None:
    """A one-shot check: the layout + a push of the values, like the GearLink
    startup batch. Only what is enabled gets pushed (the content flags enable
    their widget themselves); after the exit the layout STAYS on screen — it
    is the visual verification mode, graceful shutdown does not turn it
    off."""
    apply_layout(kbd, args)
    sensors = HostSensors()
    if SLOT_MONITOR in enabled_slots(args):
        pairs = resolve_pairs(args, sensors)
        for i in range(2):
            ok = kbd.push_metrics(pairs)
            log("the 0x66 push #%d: %s → %s" % (i + 1, ", ".join(
                "%s=%d" % (pair_label(sel, dig), val) for sel, dig, val in pairs),
                "ok" if ok else "NO REPLY"))
            if ok and i == 0:
                time.sleep(0.3)
    if args.battery:
        pct = resolve_battery(args, sensors)
        if pct is not None:
            log("the PC battery %d%% → 0x64 %s" % (pct, kbd.set_slot2_value(pct, 0)))
    if args.clock and not args.no_clock:
        sync_clock(kbd)
    log("done; the screen is set up now — check the captions on the OLED")


def status(kbd: M901, args) -> None:
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
    s = HostSensors()
    print(" CPU %s%%, RAM %s%%, temp %s, battery %s" % (
        s.cpu_load(), s.ram_load(),
        ("%d°C" % s.cpu_temp()) if s.cpu_temp() is not None else "n/a",
        ("%d%%" % round(s.battery().percent)) if s.battery() else "n/a"))


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


def shutdown_widgets(kbd: M901) -> None:
    """Graceful shutdown: turn off all the enabled widgets EXCEPT the banner.
    The clock will freeze at the last sync, the battery/metrics will go
    stale — without the daemon the dynamic widgets show wrong data; offline
    (without host pushes) only the banner and KPS live, the default
    remainder is the banner. The OLED requires at least one enabled widget
    (an empty mask is invalid), so a disabled banner is enabled FIRST and
    immediately becomes the current slot — and only then is the dynamics
    turned off (the order is in the body). Idempotent: only the enabled
    bits 1..4 of the mask are turned off; transport errors (the device
    already gone) do not block the shutdown."""
    try:
        mask = kbd.get_status_flags()
    except Exception as e:
        log("shutdown: the slot mask unreadable (%s) — turning 1..4 off blindly" % e)
        mask = None
    off = [s for s in range(5) if s != SLOT_BANNER and (mask is None or mask[s])]
    # An empty mask is invalid: the banner is off (or the mask unreadable) —
    # after turning the dynamics off, bring the banner back.
    need_banner = mask is None or not mask[SLOT_BANNER]
    if not off and not need_banner:
        log("shutdown: the dynamic widgets are already off (the mask %s)" % (mask,))
        return
    # THE ORDER MATTERS (as in apply_layout): the firmware validates every 6A
    # against the current mask ("leaving zero widgets is not allowed"), so
    # the fallback banner is enabled and made the current slot BEFORE the
    # shutdown — otherwise the disables may be silently ignored (verified
    # 2026-10-05).
    try:
        if need_banner:
            if not kbd.set_widget(SLOT_BANNER, True):    # 6A 00 00 01
                log("shutdown: the banner did not acknowledge enabling")
        kbd.select_slot(SLOT_BANNER)     # 6A 01 00 — the banner becomes the current slot
    except Exception as e:
        log("shutdown: cannot raise the banner (%s) — turning the dynamics off anyway" % e)
    for slot in off:
        try:
            if not kbd.set_widget(slot, False):          # 6A 00 <slot> 00
                log("shutdown: slot %d did not acknowledge disabling" % slot)
        except Exception as e:
            log("shutdown: slot %d did not turn off (%s) — is the transport dead?" % (slot, e))
            return
    try:
        kbd.commit()                        # 50 55 — the mask changed
    except Exception as e:
        log("shutdown: commit failed (%s)" % e)
        return
    log("shutdown: turned off the slots %s, the banner %s"
        % (", ".join("%d %s" % (s, WIDGET_NAMES[s]) for s in off) or "—",
           "re-enabled (the mask cannot be empty)" if need_banner
           else "active"))


# ---------- autostart (v0.3): a current-user scheduler task ----------

def _pythonw() -> str:
    """The pythonw.exe of the same interpreter (running WITHOUT a console window)."""
    cand = Path(sys.executable).with_name("pythonw.exe")
    if cand.is_file():
        return str(cand)
    return shutil.which("pythonw.exe") or "pythonw.exe"


def autostart_command() -> list[str]:
    """The daemon command in the autostart task: pythonw + this script + the
    working flag set. The widgets are given explicitly (without flags the
    daemon would enable the banner only): the classic clock+battery+monitor
    set, a 2 s slideshow. --log-file without a value = logs/azoth-companion.log
    next to azoth-companion.py — the task CWD at logon is not guaranteed, so
    the path must not be relative."""
    return [_pythonw(), str(Path(__file__).resolve()),
            "--clock", "--battery", "--monitor",
            "--slideshow", "2", "--log-file"]


def _schtasks(*argv: str) -> tuple[int, str]:
    r = subprocess.run(["schtasks", *argv], capture_output=True)
    out = b"\n".join(x for x in (r.stdout, r.stderr) if x)
    for enc in ("utf-8", "cp866"):    # schtasks writes in the console OEM encoding
        try:
            return r.returncode, out.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.returncode, out.decode("utf-8", "replace")


def query_autostart() -> None:
    code, out = _schtasks("/query", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: the task \"%s\" not found" % AUTOSTART_TASK)
        return
    rows = [ln.strip() for ln in out.splitlines() if ln.strip()]
    log("autostart: %s" % (rows[-1] if rows else "the task is in place"))


def install_autostart() -> None:
    """The "Azoth Companion" task: starts at the current user's logon, no
    administrator rights needed. Method 1 — the `schtasks /create /sc
    onlogon` command (its trigger is "ANY logon" — on most machines it
    requires admin); on refusal, method 2 — an XML registration: a
    LogonTrigger for the current user only + InteractiveToken +
    LeastPrivilege, i.e. exactly what the scheduler GUI lets a regular user
    create. ExecutionTimeLimit PT0S — no 72 h limit (the daemon lives
    forever). The daemon is not started at install time."""
    if sys.platform != "win32":
        raise SystemExit("autostart via schtasks is supported on Windows only")
    cmd = autostart_command()
    tr = subprocess.list2cmdline(cmd)
    code, out = _schtasks("/create", "/f", "/sc", "onlogon",
                          "/tn", AUTOSTART_TASK, "/tr", tr)
    if code == 0:
        log("autostart: the task \"%s\" created: %s" % (AUTOSTART_TASK, tr))
        query_autostart()
        return
    log("autostart: /sc onlogon rejected (code %d: %s) — registering via XML: "
        "the current user's logon only" % (code, out.strip()))
    import os
    from xml.sax.saxutils import escape
    user = r"%s\%s" % (os.environ.get("USERDOMAIN", "."),
                       os.environ.get("USERNAME", ""))
    xml = AUTOSTART_XML % {"user": escape(user),
                           "cmd": escape(cmd[0]),
                           "args": escape(subprocess.list2cmdline(cmd[1:]))}
    xml_path = Path(__file__).resolve().parent / "logs" / "azoth-companion-task.xml"
    try:
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml, encoding="utf-16")   # schtasks expects UTF-16
    except OSError as e:
        raise SystemExit("autostart: cannot write the task XML (%s): %s" % (xml_path, e))
    code, out = _schtasks("/create", "/f", "/tn", AUTOSTART_TASK,
                          "/xml", str(xml_path))
    if code != 0:
        log("autostart: FAILED to create the task \"%s\" (code %d): %s"
            % (AUTOSTART_TASK, code, out.strip()))
        raise SystemExit(1)
    log("autostart: the task \"%s\" created (the logon of %s): %s"
        % (AUTOSTART_TASK, user, tr))
    query_autostart()


def uninstall_autostart() -> None:
    if sys.platform != "win32":
        raise SystemExit("autostart via schtasks is supported on Windows only")
    code, _ = _schtasks("/query", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: the task \"%s\" does not exist — nothing to remove" % AUTOSTART_TASK)
        return
    code, out = _schtasks("/delete", "/f", "/tn", AUTOSTART_TASK)
    if code != 0:
        log("autostart: FAILED to remove the task (code %d): %s"
            % (code, out.strip()))
        raise SystemExit(1)
    log("autostart: the task \"%s\" removed" % AUTOSTART_TASK)


def main() -> None:
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
                        "usage+RAM (the \"DRAM0\" experiment), usage only; "
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
                        "cpu.freq,ram.usage,gpu.temp; ram is drawn with the "
                        "\"DRAM0\" header (0x30), gpu — \"GPU0\" (0x10); the sensor "
                        "slides (all temp/freq/volt and gpu.usage) require a "
                        "running LibreHardwareMonitor — without a sensor the "
                        "slide is skipped with a warning; the short names "
                        "(cpu, ram, freq, temp, volt) are accepted too; "
                        "--monitor-items without --slideshow enables a "
                        "slideshow with a 2 s period (formerly --slides)")

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
                        "(live calibration 2026-10-05: the OSD updates continuously "
                        "at an interval >=50 ms; 40 ms and below — a freeze/blink, "
                        "so the values above 20 Hz are capped; the default 12 has "
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

    g = ap.add_argument_group("log file and autostart")
    g.add_argument("--log-file", nargs="?", const="", default=None, metavar="PATH",
                   help="mirror the log into a file, UTF-8, a ~2 MB rotation × 3 files; "
                        "without PATH — logs/azoth-companion.log next to azoth-companion.py")
    g.add_argument("--install-autostart", action="store_true",
                   help="create the \"Azoth Companion\" scheduler task (a start at the "
                        "logon of the current user via pythonw — without a console "
                        "window; the daemon flags: --clock --battery --monitor "
                        "--slideshow 2 --log-file); no administrator rights "
                        "needed; the daemon is not started now")
    g.add_argument("--uninstall-autostart", action="store_true",
                   help="remove the \"Azoth Companion\" scheduler task")
    args = ap.parse_args()

    if args.log_file is not None:        # "" = --log-file without a value → the default path
        setup_log_file(args.log_file)
    if args.install_autostart:
        install_autostart()
        return
    if args.uninstall_autostart:
        uninstall_autostart()
        return

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

    install_stop_handlers()
    try:
        kbd = M901()
    except Exception as e:
        if args.status or args.once:
            raise SystemExit("keyboard not found: %s\n"
                             "(is the USB plugged in? is GearLink closed? pip install hidapi)" % e)
        log("keyboard not found (%s)" % e)
        kbd = wait_reconnect()
        if kbd is None:                  # stopped by a signal while waiting
            return
    log("connected: %s" % kbd.dev.get_product_string())
    daemon = not (args.status or args.once)
    try:
        if args.status:
            status(kbd, args)
        elif args.once:
            once(kbd, args)
        else:
            try:
                while not STOP.is_set():
                    apply_layout(kbd, args)
                    try:
                        run(kbd, args)
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
            if _stop_signal:
                log("stopped by the signal %s" % _stop_signal)
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
