"""The widget-slot helpers shared by the widgets (D3): the Widget contract,
the self-healing push, the clock sync, the layout (mask) application and the
graceful shutdown."""
from __future__ import annotations

import datetime as dt
import time

from ..constants import (DEFAULT_SLOTS, SLOT_BANNER, SLOT_MONITOR, WIDGET_FLAGS,
                         WIDGET_NAMES)
from ..devices.m901 import M901
from ..log import log


class Widget:
    """The widget contract (D3): exactly five widgets are known in advance —
    no plugin system.

    slot     — the OLED slot (0..4);
    refresh  — the periodic push of the values (returns their number); all
               the widget state lives on the instance;
    shutdown — the per-widget exit cleanup (the slot mask teardown itself is
               centralized — shutdown_widgets; the banner is never turned off)."""

    slot: int

    def refresh(self, kbd, router, now, config) -> int:
        return 0

    def shutdown(self, kbd) -> None:
        pass


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
