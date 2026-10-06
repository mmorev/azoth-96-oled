"""Azoth Companion — the OLED daemon package for the ASUS ROG Azoth 96 HE (M901).

The package layout (a mechanical split of the former azoth-companion.py):
  azoth/log.py         — the stdout log + the optional file mirror;
  azoth/constants.py   — the slots, the slideshow slides, the timings;
  azoth/devices/m901.py — the M901 HID transport + the vendor protocol;
  azoth/sources/       — the host value providers (sensors, manual, demo);
  azoth/widgets/       — the slot widgets (banner/clock/battery/monitor/kps helpers);
  azoth/overlays/      — the transient overlays over the slots (the volume OSD);
  azoth/loop.py        — the main loop (the device events + the pushes);
  azoth/cli.py         — argparse + Config; main.py (the project root) is the entry.
"""
