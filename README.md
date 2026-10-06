# Azoth Companion

Open-source companion for the OLED display of the **ASUS ROG Azoth 96 HE**
keyboard — a replacement for the proprietary ASUS GearLink Companion utility.
Works on Windows, macOS and Linux.

*Русская версия: [README.ru.md](README.ru.md)*

## What it is

The ROG Azoth 96 HE keyboard has a small touchscreen that can be used to
configure the keyboard itself; it can also display several widgets: a clock,
the notebook battery charge and system health indicators such as CPU/RAM load
or fan speed.

The widgets on this keyboard are controlled only via ASUS GearLink Companion —
a background application that runs on Windows only and is configured through
the GearLink web interface.

This project is a lightweight Python application that can run in the
background, is easy to configure and feeds the display with the data its
widgets need.

The USB protocol for talking to the keyboard was reverse-engineered and
documented separately (see [Contributing](#contributing)).

## Features

- **Cross-platform** — Windows/macOS/Linux. GearLink is Windows-only.
- **Clock** — synced with the host every minute (the keyboard has no RTC of
  its own; the clock is updated only from the host).
- **Battery** — notebook battery percentage.
- **Indicators** — load/temperature/frequency/voltage for CPU, GPU, RAM and
  VRM, plus the speed of the corresponding fans.
  - Indicators are shown as tiles with 1 or 2 cells.
  - Tiles page automatically or by swiping down (on GearLink the swipe is
    unreliable).
  - A swipe delays the current tile for a few seconds (timeout is
    configurable).
- **Volume** — when the volume is changed from the keyboard, the host system
  volume is shown on the OLED in real time (on GearLink — delayed, with gaps).
  - Unmute also pops the volume level on the display (GearLink cannot).
- **KPS counter** — a native tile with a keys-per-second counter.
- **Graceful shutdown** — when the app exits (Ctrl+C, SIGTERM), widgets that
  cannot run offline are switched off.

## Why not GearLink

|                       | Azoth Companion                           | GearLink           |
|-----------------------|-------------------------------------------|--------------------|
| Platform              | Windows, macOS, Linux                     | Windows only       |
| Sensors               | CPU only (as of October 2026)             | CPU, GPU, RAM, Fan |
| Volume display        | real time                                 | delayed            |
| Volume on unmute      | yes                                       | no                 |
| Graceful shutdown     | yes                                       | no                 |

## How it works

- The app polls the monitoring values and pushes them to the keyboard over USB HID.
- The app listens to keyboard events:
  - Volume changes (rocker up/down presses) and Mute/Unmute.
  - Swipes on the display (currently **down only** — left/right swipes are
    handled natively by the keyboard, up blends into the heartbeat noise).
  - On volume events it pushes the current volume value to the keyboard to be
    shown on the overlay.
- **The app has no access to keystrokes — it is not, and cannot be used as, a
  keylogger.**
- It talks **only** to the keyboard's secondary microcontroller (the system
  controller), which drives the display and the touchscreen.

The protocol for talking to the OLED display controller was researched via USB
traffic capture (USBPcap) and controller firmware analysis. The protocol is
fully documented in the companion repository
[azoth-96-oled-proto](https://github.com/mmorev/azoth-96-oled-proto).

## Sensor support

| Metric          | Windows | macOS | Linux            |
|-----------------|---------|-------|------------------|
| CPU usage       | ✅      | ✅    | ✅               |
| CPU temperature | ✅      | ✅    | ✅ (best effort) |
| CPU frequency   | ✅      | ✅    | ✅               |
| CPU voltage     | ✅      | —     | —                |
| CPU fan         | ✅      | ✅    | ✅               |
| GPU usage       | ✅      | ✅    | —                |
| GPU temperature | ✅      | ✅    | —                |
| GPU frequency   | ✅      | —     | —                |
| GPU voltage     | ✅      | —     | —                |
| RAM usage       | ✅      | ✅    | ✅               |
| RAM temperature | ✅      | —     | —                |
| RAM frequency   | ✅      | —     | —                |
| RAM voltage     | ✅      | —     | —                |
| Battery         | ✅      | ✅    | ✅               |
| Volume OSD      | ✅      | ✅    | —                |

### Backends:

- Windows reads the sensors from a running
  [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  (via WMI; `pip install wmi`);
- macOS uses [macmon](https://github.com/vladkens/macmon) (Apple Silicon,
  `brew install macmon`);
- Linux — [psutil](https://github.com/giampaolo/psutil) only.

Load, RAM, battery and CPU frequency come from psutil on every OS.

Metrics without a working backend are skipped with a single log warning — the
slideshow runs over the live slides only.

## Quick start

Python 3.10+ and the keyboard connected via **USB** are required.

```bash
pip install -r requirements.txt        # hidapi + psutil
```

**Close GearLink first** — otherwise the applications conflict and freeze the
display and sometimes the whole controller.

In any case, using the application is completely safe for the keyboard. If
anything hangs or misbehaves, just reconnect the cable.

On Windows: `taskkill //IM GearLink* //F`.

Sanity checks:

```bash
python main.py --status                  # device and host status, writes nothing
python main.py --demo                    # indicator test: the CPU tile sweeps 0→100→0
python main.py --once --cpu 42 --bat 77  # push manual values once, no updates
```

The working loop — the clock, the battery and two monitoring tiles, a double
one (CPU+RAM usage) and a single one (CPU temperature):

```bash
python main.py --clock --battery --monitor --monitor-items cpu.usage+ram.usage,cpu.temp
```

Setting the slideshow delay for the metric tiles:

```bash
python main.py --clock --battery --monitor --monitor-items cpu.usage+ram.usage,cpu.temp --slideshow 3
```

## Command-line parameters

The full list — `python main.py --help`. The essentials:

**Widgets** (which slots to enable; without flags only the banner remains):

- `--banner` — slot 0: the banner/music mode/your own bitmap, static
- `--clock` — slot 1: the clock, synced at the boundary of every minute
- `--battery` — slot 2: the PC battery (`--bat` enables it automatically)
- `--monitor` — slot 3: the double indicator/slideshow; enabled automatically
  by the `--metrics`, `--slideshow`, `--monitor-items` flags
- `--kps` — slot 4: the keys-per-second counter, driven natively by the
  controller

**Loop and slideshow:**

- `--interval SEC` — the host polling period in the normal mode, s (default 2)
- `--metrics cpu-temp|cpu-ram|cpu` — the double indicator tile set without a
  slideshow: usage+temp (the default; without a sensor — usage only),
  usage+RAM ("DRAM0"), usage only
- `--slideshow SEC` — page the slides every SEC seconds; a swipe down pages
  manually and puts auto-paging on a 5 s pause
- `--monitor-items SRC.METRIC[,…]` — slides in the "source.metric" format:
  the sources `cpu/gpu/ram`, the metrics `usage/temp/freq/volt/fan`, `a+b`
  pairs are a double tile (e.g. `cpu.usage+ram.usage`); short names like
  `cpu` work too; the sensor slides (all temp/freq/volt and `gpu.usage`)
  require LibreHardwareMonitor, without it the slide is skipped with a
  warning; without `--slideshow` the list pages every 2 s
- `--start 0-4` — which widget to show after the setup, the slot number
  (must be in the set of the enabled widgets)
- `--brightness 0-100` — re-set the brightness (by default it is left
  untouched)
- `--keep-awake` — wake the OLED with `65 FF` every 60 s
- `--no-clock` — do not sync the time

**Manual values** (tests without sensors):

- `--cpu 0-100`, `--temp °C`, `--ram-val 0-100`, `--bat 0-100`.

**Volume updates:**

- `--vol-hz HZ` — the OSD push rate inside the rocker series window
  (default 12, values above 20 Hz are capped)
- `--no-volume` — do not listen to the rocker and do not push the volume OSD.

**Diagnostics:**

`--once` — set up and push the values once, no loop (the picture stays on the screen)
`--status` — device and host status only, writes nothing
`--demo` — indicator test (the CPU tile sweeps smoothly 0→100→0)
`--events` — log the rocker/touch events
`--evt-dump` — dump all the 0xFFC0 frames and the TXLOG

**Log:**

`--log-file [PATH]` — mirror the log into a file (UTF-8, a ~2 MB rotation × 3);
without PATH — `logs/azoth-companion.log` next to `main.py`.

## Autostart (Windows)

```bash
python scripts/install.py install      # create the "Azoth Companion" logon task
python scripts/install.py uninstall    # remove it
```

Creates a current-user scheduled task (starts via `pythonw.exe` at logon with
the flags `--clock --battery --monitor --slideshow 2 --log-file`). No
administrator rights required. On macOS/Linux add the same command line to
your session autostart; a built-in installer is not implemented yet.

## Contributing

- Internal notes — the package layout, the daemon behavior, the tests, the
  live-test status — live in [DEVELOPMENT.md](DEVELOPMENT.md). Tests:
  `python tests/test_*.py`.
- The device protocol (the transport, the widget slots, the gestures, the
  volume OSD, the OLED internals) is documented in a separate repository,
  [azoth-96-oled-proto](https://github.com/mmorev/azoth-96-oled-proto) —
  protocol-level edits and questions go there.

## License

[GPL-3.0](LICENSE). Not affiliated with or endorsed by ASUS. Use at your own risk.
