"""The shared constants: the widget slots, the slideshow slides, the timings."""
from __future__ import annotations

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
