# Development notes (Azoth Companion)

Internal notes for contributors: package layout, runtime behavior of the daemon,
tests, live-test status and known limitations.

The reverse-engineered device protocol is **not** documented here — see the
companion repository [azoth-96-oled-proto](https://github.com/mmorev/azoth-96-oled-proto)
(`docs/`: transport, widget slots, gestures, volume OSD, OLED internals).

## Package layout

Since the 2026-10-06 restructure (before that — the `azoth-companion.py` monolith):

```text
main.py               — the entry point (the connect/reconnect cycle)
azoth/
  cli.py              — argparse → Config; the --once/--status modes
  loop.py             — the scheduler: the single 0xFFC0 queue owner
  constants.py        — the slots, the slides, the timings
  log.py              — the stdout log + the file mirror
  devices/m901.py     — the HID transport + the vendor protocol
  sources/            — the value providers: router.py (the "key → provider"
                        registry) + common/ (psutil, manual, demo),
                        windows/ (LHM, volume), darwin/ (macmon, volume),
                        linux/ (hwmon)
  widgets/            — the slot widgets: base.py (the contract), banner.py,
                        clock.py, battery.py, monitor.py, kps.py
  overlays/volume.py  — the volume OSD (the rocker)
scripts/install.py    — the autostart (schtasks): install|uninstall
tests/                — the assert-based unit checks
```

## Runtime behavior notes

- **Robust pushes.** A heartbeat (`HEARTBEAT_S=10`, an unconditional push) keeps
  the screen out of its idle sleep; `robust_push` distinguishes failure modes:
  a real NAK `FF AA` (sleep gate) → wake (`65 FF` + `69`) + retry, with a short
  blink, only when the screen is explicitly off (`screen_is_on()`); a timeout
  (the device is busy rendering) → a quiet retry after 0.3 s. The failure-mode
  flag is `M901.last_nak`. See proto `PROTOCOL_STATUSBAR.md` §4 for the wake
  mechanics.
- **Slideshow.** A slide = a single `0x66` push (echo=0; pair slides `a+b` use
  echo=1, see proto `PROTOCOL_STATUSBAR.md` §4.1). Auto-paging by timer; a swipe
  down (`03 96`) pages manually and pauses auto-paging for `SWIPE_PAUSE_S = 5` s
  (`0396` routing in `azoth/loop.py`); swipe up is not reported by the firmware.
- **Volume worker.** A tick is `03 72 01/04` on 0xFFC0; the worker pushes
  `51 0C` on every tick with a PREDICTED "cache ± 2%" value (no round-trip), a
  background OS poller (~10 Hz) reconciles the cache, and 0.3 s after the last
  tick the EXACT value is pushed (`WindowsVolume.fresh()`, a direct COM call).
  The mute→unmute transition is detected by polling the OS (`on_unmute`) — the
  mute key does not produce an iface2 event; the unmute push draws the level
  OSD. See proto `PROTOCOL_VOLUME.md` §1–§2.6.
- **Sensor routing.** `azoth/sources/router.py` assembles the provider chain per
  platform (demo > manual > platform chain > psutil); a dead subsystem shrinks
  its `keys()` to `()` — one warning, the affected slides are skipped. Without
  psutil `cpu.usage`/`ram.usage` fail fast (SystemExit), not silent zeros.
- **Log file.** `--log-file [PATH]` mirrors stdout lines into a UTF-8 file with
  size-based rotation ~2 MB × 3 files (`RotatingFileHandler`); default
  `logs/azoth-companion.log` next to `main.py`. Important under `pythonw`:
  `sys.stdout` is absent there and `print()` stays silent — the log lives only
  in the file.
- **Autostart (Windows only).** `scripts/install.py install|uninstall` manages a
  current-user scheduler task (`pythonw.exe`, `--clock --battery --monitor
  --slideshow 2 --log-file`). Two-stage: plain `schtasks /sc onlogon` first, then
  an XML fallback (`LogonTrigger` for the current user, `InteractiveToken`,
  `RunLevel=LeastPrivilege`, no 72 h limit, `IgnoreNew`) — no administrator
  rights needed. The generated XML is left in `logs/azoth-companion-task.xml`.

## Tests

```bash
python tests/test_loop_route.py
python tests/test_monitor_widget.py
python tests/test_router.py
```

Assert-based, no framework. Run them after touching `loop.py`, the widgets or
the source router.

## Live test status

### v0.2 (2026-10-04, session 2)

- ✅ Transport (the report-ID prefix), status, mask, slot, time `0x63` — the
  on-screen clock updated upon a push.
- ✅ Battery `0x64` — confirmed visually (42% in the demo).
- ✅ `0x66` is pushed and acknowledged; "DRAM0 Usage" confirmed on screen (a
  running triangle, the second pair of the double tile).
- ✅ The slideshow mechanics (alternating selectors within one push) work on the
  top tile of the double indicator.
- ❌ Slot 4 as a "single tile" — not confirmed (the double layout + KPS).
- ⏳ Temperature — waiting for LibreHardwareMonitor + `pip install wmi`.

### v0.3 (2026-10-05, GearLink killed, `ZEPHYRUS`, Python 3.14)

- ✅ The slides `cpu,ram,freq` page by timer every 2 s; one NAK in 40 s absorbed
  by a quiet retry (robust_push without a wake-up), the protocol unchanged.
- ✅ `--slides` moved to the GearLink "source.metric" grid
  (`cpu.usage,ram.usage,cpu.freq` — selectors 0x00/0x30/0x02 confirmed in the
  log); short first-draft names work as aliases; `gpu.usage` without a sensor is
  skipped with a single warning and the slideshow runs over the live slides;
  `--once` after the `HostSensors` refactor works as before.
- ✅ `--slides cpu,temp,volt,freq` without wmi/LHM: both sensor slides skipped
  with one-time warnings, the slideshow ran over the live slides `cpu → freq`.
- ✅ Validation: `--slides bogus` / `--slides ","` — startup errors listing the
  allowed names; `--slideshow 0` — a clean argparse error.
- ✅ `--log-file`: the file is created, mirrors stdout, valid UTF-8; rotation
  verified on a real `setup_log_file()` with a reduced limit — exactly 3 files,
  no losses.
- ✅ Autostart: `schtasks /sc onlogon` without admin → "Access is denied" → the
  XML fallback created the task; `schtasks /run` brought up the pythonw daemon
  WITHOUT a console window; idempotent reinstall, clean uninstall.
- ✅ The `--once` regression (the old `--metrics` path) — unchanged.
- ⏳ Swipe down (manual paging + the pause) and the rocker hold (the final value
  after the release) need hands on the device; the code of these paths was not
  touched in v0.3. Verification command: `python main.py --slideshow 2 --log-file`.
- ⏳ The labels/units of the gpu.*/ram.* tiles on screen and the LHM GPU/RAM
  sensors themselves — to be checked on a machine with a running
  LibreHardwareMonitor.

## Known limitations

- The host polling is fixed (2 s / 0.4 s in demo / 1 s in slideshow, a push on
  change) — not strictly event-driven like GearLink.
- Without LibreHardwareMonitor (Windows) only Usage is pushed; the second tile
  of the double indicator stays at "CPU0 Usage 0" — visible to the user.
- Slideshow: right after a slide change the first value may flash from the
  previous metric (the shift of the "type/value" pairs).
- The residual rare blinks coincide with the per-minute `0x63` push (a full
  re-render of the clock text) — the price of an accurate clock.
- The first rocker series blink is a firmware fallback, not fixable host-side
  (proto `PROTOCOL_VOLUME.md` §2.4).
