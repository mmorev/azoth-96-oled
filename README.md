# Azoth Companion

Open-source companion for the **ASUS ROG Azoth 96 HE** touchscreen OLED — a free
replacement for the proprietary ASUS GearLink utility. Runs on Windows, macOS and
Linux.

*Русская версия: [README.ru.md](README.ru.md)*

<!-- TODO: a photo/GIF of the display with the widgets would go here -->

## What it is

The ROG Azoth 96 HE keyboard has a small touchscreen that normally shows clock,
battery and CPU stats, driven by ASUS GearLink — a Windows-only companion app.
This project is a lightweight open-source daemon that feeds the display from any
OS: host clock, battery, CPU/GPU/RAM metrics, a slideshow of metric tiles and
even the volume indicator on the rocker. The USB protocol was reverse-engineered
and is documented separately (see [Contributing](#contributing)).

## Features

- **Clock** — synced from the host every minute (the dial ticks on its own).
- **Battery** — notebook battery percentage.
- **Monitor tile** — CPU/GPU/RAM usage, temperature, frequency, voltage, fan RPM;
  a double tile or a timed slideshow of single tiles.
- **Manual paging** — swipe down on the display to switch slides; auto-paging
  pauses for a few seconds.
- **Volume OSD** — the volume rocker shows the level on the display; even the
  mute→unmute transition gets a level pop-up (something GearLink does not do).
- **KPS counter** — the native keys-per-second tile.
- **Log file** — optional, with rotation (handy when running without a console).

## Why not GearLink

| | Azoth Companion | GearLink |
| --- | --- | --- |
| OS | Windows, macOS, Linux | Windows only |
| License | open source, GPL-3.0 | proprietary |
| Autostart | per-user task, no admin rights | (part of the suite) |
| Unmute → level OSD | yes | no |
| Sensors | via standard tools (psutil, LibreHardwareMonitor, macmon) | own stack |

## How it works

A small daemon polls the host values and pushes them to the keyboard over a
vendor USB HID channel; the keyboard's second microcontroller (system controller)
renders everything on the touchscreen. The clock ticks on-device, the display
goes to sleep only if nothing is pushed, and rocker/touch events are read back
over the same channel.

```text
this daemon                          keyboard
+----------------------+   USB HID   +----------------------+
| CPU / GPU / RAM /    |  vendor     | system controller    |
| battery, volume      |------------>| renders the widgets  |
| polls, pushes        |   0xFFC0    | on the touchscreen   |
+----------------------+             +----------------------+
```

The protocol is reverse-engineered from USB captures and firmware analysis and
documented in full in the
[azoth-96-oled-proto](https://github.com/mmorev/azoth-96-oled-proto) repository.

## Sensor support

| Metric | Windows | macOS | Linux |
| --- | --- | --- | --- |
| CPU usage | ✅ | ✅ | ✅ |
| CPU temperature | ✅ | ✅ | ✅ (best effort) |
| CPU frequency | ✅ | ✅ | ✅ |
| CPU voltage | ✅ | — | — |
| CPU fan RPM | ✅ | ✅ | ✅ |
| GPU usage | ✅ | ✅ | — |
| GPU temperature | ✅ | ✅ | — |
| GPU frequency | ✅ | — | — |
| GPU voltage | ✅ | — | — |
| RAM usage | ✅ | ✅ | ✅ |
| RAM temperature | ✅ | — | — |
| RAM frequency | ✅ | — | — |
| RAM voltage | ✅ | — | — |
| Battery | ✅ | ✅ | ✅ |
| Volume OSD | ✅ | ✅ | — |

Backends: Windows reads sensors from a running
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
(via WMI; `pip install wmi`); macOS uses
[macmon](https://github.com/vladkens/macmon) (Apple Silicon, `brew install
macmon`); Linux is psutil-based only (core temps via hwmon). Load, RAM, battery
and CPU frequency come from [psutil](https://github.com/giampaolo/psutil) on
every OS. Metrics without a working backend are skipped with a single warning —
the slideshow simply runs over what is alive.

## Quick start

Python 3.10+ and the keyboard connected via **USB** are required.

```bash
pip install -r requirements.txt        # hidapi + psutil
```

**Close GearLink first** — two hosts fighting over the display cause a NAK
ping-pong. On Windows: `taskkill //IM GearLink* //F`.

Try it read-only, then play:

```bash
python main.py --status                # device + host status, writes nothing
python main.py --demo --bat 42         # sensor test: the CPU tile runs 0→100→0
python main.py --once --cpu 42 --temp 55 --bat 77   # push manual values once
```

The working loop — clock, battery and the monitor tile:

```bash
python main.py --clock --battery --monitor              # CPU temp/usage tile
python main.py --clock --battery --monitor --metrics cpu-ram   # both cells live
```

Slideshow of metric tiles (names follow the `source.metric` scheme):

```bash
python main.py --clock --battery --monitor --slideshow 3
python main.py --monitor --monitor-items cpu.usage,cpu.freq,ram.usage
python main.py --monitor --monitor-items cpu.usage+ram.usage,cpu.temp  # a pair = both cells
```

Valid names: `cpu.usage cpu.temp cpu.freq cpu.volt cpu.fan gpu.usage gpu.temp
gpu.freq gpu.volt ram.usage ram.temp ram.freq ram.volt` (short aliases like
`cpu` work too). Without `--slideshow`, `--monitor-items` pages every 2 s.

Useful extras: `--brightness 0-100`, `--start 0-4` (initial slide), `--no-volume`,
`--events` (log the rocker/touch events), `--log-file`. On shutdown (Ctrl+C) the
dynamic widgets are switched off and the banner is restored.

## Autostart (Windows)

```bash
python scripts/install.py install      # create the "Azoth Companion" logon task
python scripts/install.py uninstall    # remove it
```

Creates a current-user scheduled task (starts via `pythonw.exe` at logon with
`--clock --battery --monitor --slideshow 2 --log-file`). No administrator rights
required. On macOS/Linux run the same command line from your session autostart
mechanism; a built-in installer is not implemented yet.

## Contributing

- Internal notes — package layout, daemon behavior, tests, live-test status —
  live in [DEVELOPMENT.md](DEVELOPMENT.md). Tests: `python tests/test_*.py`.
- The device protocol (transport, widget slots, gestures, volume OSD, OLED
  internals) is documented in the separate
  [azoth-96-oled-proto](https://github.com/mmorev/azoth-96-oled-proto)
  repository — the right place for protocol-level contributions and issues.

## License

[GPL-3.0](LICENSE). Not affiliated with or endorsed by ASUS. Use at your own risk.
