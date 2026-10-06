#!/usr/bin/env python3
"""
M901 (ASUS ROG Azoth 96 HE) - Python client of the vendor protocol.

Transport: HID, usage page 0xFF00 (USB) or 0xFF01, 64-byte reports without a report ID.
Dependency: pip install hidapi  (import hid)

For the command status see analysis/PROTOCOL.md.
"""
from __future__ import annotations
import struct
import sys
import threading
import time
from contextlib import suppress

# Transaction logging hook: (cmd, sub, echo, response|None, nak, ms).
# The prototype enables it in --evt-dump mode to correlate NAKs with events.
TXLOG = None

try:
    import hid
except ImportError:
    raise SystemExit("pip install hidapi")

if sys.platform == "darwin":
    # macOS: a recent hidapi opens devices in exclusive mode (seize) —
    # our handle kicks the system HID stack off the shared EP, and the OS
    # stops seeing the consumer events of the volume rocker (iface2: the
    # volume 0x0C and the 0xFFC0 mirror sit on the same USB interface; the
    # volume stops changing). Switch to non-exclusive like on Windows: the
    # reports are delivered to all clients. The call is global, BEFORE the
    # first hid.device().open; on an old hidapi (no such symbol) it is
    # non-exclusive by default anyway.
    import ctypes
    with suppress(OSError, AttributeError):
        ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive(0)

VID = 0x0B05
PID = 0x1C10           # APP mode (the bootloader is a different PID, not shipped)
USAGE_PAGES = (0xFF00, 0xFF01)

# Windows-hidapi requires the first buffer byte to be the report ID (the
# device has none → 0x00); Linux/hidraw sends the report as is. Verified by
# a live transaction on 2026-10-04: without the prefix on Windows the
# device answers NAK FF AA 00 00 (the packet "shifted" by a byte).
WRITE_PREFIX = b"\x00" if sys.platform == "win32" else b""

MAGIC_UNLOCK = bytes([0x7B, 0xAA, 0x41, 0x53, 0x55, 0x53, 0xAA]) + b"\x00" * 57  # "{AA ASUSS AA"

# Response codes
R_STATUS = 0x12
R_MATRIX = 0x24
R_LED    = 0x25
R_BOOL   = 0x27
R_CFG1   = 0x41
R_CFG2   = 0x43
R_SETACK = 0x51
R_NAK_HDR = b"\xff\xaa"


class M901:
    def __init__(self, path: bytes | None = None):
        self.dev = hid.device()
        if path:
            self.dev.open_path(path)
        else:
            opened = False
            for d in hid.enumerate(VID, PID):
                if d["usage_page"] in USAGE_PAGES:
                    self.dev.open_path(d["path"])
                    opened = True
                    break
            if not opened:
                raise IOError("vendor interface (usage page 0xFF00/0xFF01) not found")
        self.dev.set_nonblocking(False)
        self._echo = 0
        self._tlock = threading.Lock()

    def close(self):
        self.dev.close()

    # ---------- transport ----------
    def _next_echo(self) -> int:
        self._echo = (self._echo + 1) & 0xFFFF
        return self._echo

    def send(self, cmd: int, sub: int, args: bytes = b"", echo: int | None = None) -> None:
        """CMD [SUB] [ECHO u16] [ARGS...] + padding to 64."""
        e = self._next_echo() if echo is None else echo
        pkt = struct.pack("<BBH", cmd, sub, e) + args
        pkt += b"\x00" * (64 - len(pkt))
        assert len(pkt) == 64
        self.dev.write(WRITE_PREFIX + pkt)

    def recv(self, timeout_ms: int = 1000) -> bytes | None:
        # IMPORTANT (Windows): timeout_ms=0 in cython-hidapi means an
        # INFINITELY blocking read here (verified 2026-10-04) — clamp to
        # 1 ms so a "non-blocking" drain call does not hang.
        if timeout_ms <= 0:
            timeout_ms = 1
        data = self.dev.read(64, timeout_ms=timeout_ms)
        return bytes(data) if data else None

    def transact(self, cmd: int, sub: int, args: bytes = b"", timeout_ms: int = 1000,
                 echo: int | None = None) -> bytes | None:
        """Send a command and get the matching reply (by echo).
        After the call self.last_nak=True if the device answered with a real
        NAK `FF AA` (incl. "the display gate is closed"), False on timeout.
        Thread-safe: the volume rocker reader thread pushes 51 0C in parallel
        with the main loop — the lock serializes the whole send+recv cycle."""
        t0 = time.monotonic()
        with self._tlock:
            e = self._next_echo() if echo is None else echo
            self.send(cmd, sub, args, echo=e)
            self.last_nak = False
            r = None
            deadline = time.time() + timeout_ms / 1000
            while time.time() < deadline:
                r = self.recv(200)
                if not r:
                    continue
                if r[:2] == R_NAK_HDR:
                    rcmd = struct.unpack_from("<H", r, 2)[0]
                    if rcmd == (cmd | (sub << 8)) or rcmd == cmd:
                        self.last_nak = True
                        break
                    continue
                rcmd, rsub, recho = r[0], r[1], struct.unpack_from("<H", r, 2)[0]
                # IMPORTANT (live test 2026-10-05): the keyboard sends unsolicited
                # broadcasts 12 00/12 01 (echo bytes 0000). Without matching cmd,
                # the transactions with echo=0 (51 0C and the whole widget family)
                # falsely match them, the reply queue shifts, and the pushes
                # look like "NAK/no reply".
                if recho == e and rcmd == cmd and rsub == sub:
                    break
                r = None
            else:
                r = None
        if TXLOG:
            try:
                TXLOG(cmd, sub, e, r, self.last_nak, (time.monotonic() - t0) * 1000)
            except Exception:
                pass
        return r

    # ---------- system ----------
    def unlock(self) -> bool:
        """The magic code (needed for 0x61/0x62/0x67/0x6B/0xC0).

        WARNING: per the reverse engineering, after the magic the device goes
        to the bootloader (a reboot with the 0xDEADBEEF flag) - this is the
        firmware update path!
        """
        self.send(0x7B, 0xAA, b"ASUS\xAA")
        return True

    def get_connection_state(self) -> int | None:
        """0x12/0x00: 1 byte - the connection state (USB/BT/RF)."""
        r = self.transact(0x12, 0x00, b"")
        return r[4] if r and len(r) >= 5 else None

    def get_battery_flags(self) -> int | None:
        """0x12/0x02: charging/battery (the 0x30 bits of [0x23006F70])."""
        r = self.transact(0x12, 0x02, b"")
        return r[4] if r and len(r) >= 5 else None

    def get_pair_state(self) -> int | None:
        """0x12/0x06: the pairing state (2 = paired, 0xFF = no)."""
        r = self.transact(0x12, 0x06, b"")
        return r[4] if r and len(r) >= 5 else None

    # ---------- display/widgets (see analysis/PROTOCOL_STATUSBAR.md, PROTOCOL_OLED.md) ----------
    def set_widget(self, slot: int, on: bool, mode: int = 0x00) -> bool:
        """0x6A sub 0: enable/disable a slot widget (the slot bit of the 0x23008BCE mask).

        slot 0..4, mode - the pkt[4] environment byte (0x00/0x03 in the captures).
        Disabling the current slot automatically switches to the next enabled one.
        """
        assert 0 <= slot <= 4
        r = self.transact(0x6A, 0x00, bytes([slot, 0x01 if on else 0x00]))
        return bool(r) and r[0] == 0x6A

    def select_slot(self, slot: int) -> bool:
        """0x6A sub 1: make the slot active (only if its bit is enabled).

        For a disabled slot - silently ignored; slot==0 additionally resets
        the banner counter [0x23014C34] (PROTOCOL_OLED.md §3).
        """
        assert 0 <= slot <= 4
        r = self.transact(0x6A, 0x01, bytes([slot, 0x00]))
        return bool(r) and r[0] == 0x6A

    def get_status_flags(self) -> list[int] | None:
        """0x24 sub 0x02: the mask of the enabled slot widgets.

        Returns 5 items (the bits 0..4 of byte 0x23008BCE), each 0/1:
        slot 0 = clock/calendar, 1/2 = indicators (the rocker toggles / 0x64),
        3 = the double CPU+RAM indicator, 4 = the special slot. Bit 5 is not
        transmitted by the device (reserved).
        """
        r = self.transact(0x24, 0x02, b"")
        if r and r[0] == R_MATRIX and r[1] == 0x02:
            return list(r[4:9])
        return None

    def get_current_slot(self) -> int | None:
        """0x24 sub 0x01: the currently selected slot (0..4)."""
        r = self.transact(0x24, 0x01, b"")
        if r and r[0] == R_MATRIX and r[1] == 0x01:
            return r[4]
        return None

    def get_screen_compact(self) -> tuple[int, int] | None:
        """0x24 sub 0x00: "what is on screen" — {slot<<4|flags, sub-state}.

        The same encoding as IPC 0x95/0x96 to sysctrl (PROTOCOL_STATUSBAR.md §7):
        byte 0 - the slot in the high nibble + the mode flags; byte 1 - the
        sub-state (for the clock slot - the time nibbles and such).
        """
        r = self.transact(0x24, 0x00, b"")
        if r and r[0] == R_MATRIX and r[1] == 0x00:
            return (r[4], r[5])
        return None

    def set_widget_slot(self, slot: int, on: bool) -> bool:
        """0x6A sub 0: enable/disable a slot widget (the slot bit of the 0x23008BCE mask).

        slot 0..4; disabling the current slot switches the device to the next
        enabled one (visible via get_current_slot). Same as set_widget().
        """
        return self.set_widget(slot, on)

    def select_widget_slot(self, slot: int) -> bool:
        """0x6A sub 1: make the slot active (if its bit is enabled).

        The GearLink "show widget" pattern: set_widget_slot(slot, True) +
        select_widget_slot(slot). For a disabled slot - silently ignored.
        """
        return self.select_slot(slot)

    def slideshow(self, slots: list[int], interval: float = 5.0, cycles: int = 0) -> None:
        """A widget slideshow: cyclically switches the OLED over the enabled slots
        (6A 01, confirmed by the live test 2026-10-03 — GearLink shows the
        configurable widget this way). slots = the slots to rotate, interval =
        seconds per frame, cycles = the number of full rounds (0 = infinite,
        Ctrl+C to stop). Enable the slots beforehand: set_widget(s, True)
        for s in slots."""
        import time as _t
        assert all(0 <= s <= 4 for s in slots) and slots
        n = 0
        try:
            while True:
                for s in slots:
                    self.select_slot(s)
                    _t.sleep(interval)
                n += 1
                if cycles and n >= cycles:
                    break
        except KeyboardInterrupt:
            pass

    def get_current_page(self) -> int | None:
        """0x21 sub 0: the current screen page [0x23000C9F]."""
        r = self.transact(0x21, 0x00, b"")
        if r and r[0] == 0x21:
            return r[4]
        return None

    def get_screen_state(self) -> int | None:
        """0x23 sub 0x02: the "screen on" byte [0x230086BC+0x513]."""
        r = self.transact(0x23, 0x02, b"")
        if r and r[0] == 0x23:
            return r[4]
        return None

    def screen_is_on(self) -> bool:
        """True if the screen is explicitly on. None/a read error is treated as
        "on" — waking is allowed only on an EXPLICIT zero, otherwise false
        wake-ups (blinks) on a busy path (the rocker OSD, gestures)."""
        s = self.get_screen_state()
        return s is None or s != 0

    def get_status_struct(self) -> bytes | None:
        """0x12 sub 0 (echo=0): a 16-byte dump of 0x23000C98.

        +0 u16 CPU temp, +2 u16 CPU %, +4 u16 RAM %, +6 dev, +7 page,
        +9 slot, +10 the 0xB5 flag.
        """
        r = self.transact(0x12, 0x00, echo=0)
        if r and r[0] == R_STATUS:
            return bytes(r[4:20])
        return None

    def get_battery(self) -> int | None:
        """0x12 sub 1: the battery charge % - payload[1] ([0x2301D26D])."""
        r = self.transact(0x12, 0x01, b"")
        if r and r[0] == R_STATUS and r[1] == 0x01:
            return r[5]
        return None

    # ---------- OLED: time, metrics, brightness, wake (PROTOCOL_OLED.md) ----------
    def set_clock(self, year: int, month: int, day: int, hour: int, minute: int) -> bool:
        """0x63: set the date/time (like GearLink in the config batch).

        The wire: `63 00 <echo> 00 <date u32 LE> <time u16 LE>`, where
        date = year | month<<16 | day<<24, time = hour | minute<<8
        (capture 08: `63 00 00 00 00 EA 07 0A 02 11 35` = 2026-10-02 17:53).
        It also sets bit1 of [0x23014B49] ("the time is set").
        """
        assert 1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60
        date = (year & 0xFFFF) | (month << 16) | (day << 24)
        tval = (hour & 0xFF) | ((minute & 0xFF) << 8)
        args = b"\x00" + struct.pack("<IH", date, tval)
        r = self.transact(0x63, 0x00, args, echo=0)
        return bool(r) and r[0] == 0x63

    def send_cpu_usage(self, usage: int, temp: int, sel2: int = 2, val2: int = 0) -> bool:
        """0x66 (echo=1): a metrics push — two pairs {selector u8, rsv u8, value u16 LE}.

        The format per the captures (08b/09, byte for byte):
        `66 00 01 00 | 00 00 <usage u16> | 01 00 <temp u16>`
        sel 0 = usage %, sel 1 = temp °C (the selector space 0..5,
        nibbles: hi ≤ 4, lo ≤ 5). IMPORTANT (fixed 2026-10-03): byte 7
        used to be read as "RAM %" — that is wrong, it is the high byte
        of the usage u16 (always 00 in the captures; writing a non-zero
        byte there corrupts the usage value: 50%+10*256 = 2610%).
        """
        assert 0 <= usage <= 100 and temp < 65536 and sel2 <= 5 and val2 < 65536
        args = (bytes([0x00, 0x00]) + struct.pack("<H", usage) +
                bytes([sel2, 0x00]) + struct.pack("<H", temp if sel2 == 1 else val2))
        r = self.transact(0x66, 0x00, args, echo=1)
        return bool(r) and r[0] == 0x66

    def set_slot2_value(self, value: int, on: int = 0) -> bool:
        """0x64: the value of the slot 2 tile + on/off (confirmed by the capture
        10-battery-toggle: GearLink sends `64 00 <echo> <battery %> 00` —
        the host battery widget = slot 2, the value = percent)."""
        assert 0 <= value <= 255
        r = self.transact(0x64, 0x00, bytes([value, on]), echo=0)
        return bool(r) and r[0] == 0x64

    def send_metric(self, sel: int, digit: int, value: int) -> bool:
        """0x66 (echo=1) with an arbitrary selector: the sel lo-nibble = the value type
        (0=Usage %, 1=Temp °C, 2=Freq, 5=Volt, 3/4=Fan — per the capture
        11-mon-widgets), the hi-nibble = the tile header (0=CPU, 1=GPU, 2=VRM,
        3=DRAM, 4=CHA), digit = the instance digit (0xFF = no digit).
        Useful for testing the headers: sel=0x31 -> "DRAM0 / Temp.".
        The second pair is the neutral {00 00 0000}, as in GearLink's single pushes."""
        args = (bytes([sel & 0xFF, digit & 0xFF]) + struct.pack("<H", value) +
                bytes([0x00, 0x00]) + struct.pack("<H", 0))
        r = self.transact(0x66, 0x00, args, echo=1)
        return bool(r) and r[0] == 0x66

    def push_metrics(self, pairs, echo: int = 0) -> bool:
        """0x66: a push of 1-2 pairs {sel u8, digit u8, value u16 LE} — the generic
        version of send_cpu_usage/send_metric. sel: the lo-nibble = the value
        label (0=Usage, 1=Temp., 2=Freq., 5=Volt), the hi-nibble = the tile
        header (0=CPU, 1=GPU, 2=VRM, 3=DRAM, 4=CHA); the handler validates
        hi≤4, lo≤5 (PROTOCOL_OLED.md §10.3.2). digit = the instance digit
        (0 → "CPU0"/"DRAM0", 0xFF = no digit). A single push is padded with
        the neutral second pair {00 00 0000}, as in the GearLink captures.
        IMPORTANT (live test 2026-10-04): 0x66 has two paths by echo (§4) —
        echo=1 → 0x0E08D65A ("noisy", every push jumps the screen to
        the first page of the slot 4 carousel), echo=0 → the quiet IPC 0x1E.
        GearLink's slideshow sends single pushes with echo=0 (capture 20),
        the config pushes with echo=1. The default here is echo=0."""
        assert 1 <= len(pairs) <= 2
        args = b""
        for sel, digit, value in pairs:
            hi, lo = (sel >> 4) & 0xF, sel & 0xF
            assert hi <= 4 and lo <= 5 and 0 <= value <= 0xFFFF
            args += bytes([sel & 0xFF, digit & 0xFF]) + struct.pack("<H", value)
        if len(pairs) == 1:
            args += b"\x00\x00\x00\x00"
        r = self.transact(0x66, 0x00, args, echo=echo)
        return bool(r) and r[0] == 0x66

    def push_volume_osd(self, v: int) -> bool:
        """`51 0C 00 00 [V]` — the volume OSD "V%" (PROTOCOL_VOLUME.md §2).
        echo MUST be 0x0000: this is the OSD branch (the counter
        [0x23006F68]=1000, IPC 0x26 + the hide timer IPC 0x16); echo=1 goes
        to a write-only copy of "the last volume" without an OSD. The
        keyboard hides it after 1 s itself — the OSD only lives on the
        rocker ticks."""
        v = max(0, min(100, int(v)))
        r = self.transact(0x51, 0x0C, bytes([v]), echo=0)
        return bool(r) and r[0] == 0x51

    def wake_display(self, mode: int = 0xFF) -> bool:
        """0x65: "show/wake" the OLED (IPC 0x1F, the flag [0x2301D249]=1).

        GearLink in the captures (16/18) sends mode=0xFF — "just show/wake".
        Mode 0 in the live test 2026-10-04 switched the screen to the
        notification view (the "Mail" tile) — do not use without a need. The
        flag [0x2301D249] also gates the touchscreen gestures until it decays.
        """
        assert mode == 0xFF or mode <= 2
        r = self.transact(0x65, 0x00, bytes([mode]), echo=0)
        return bool(r) and r[0] == 0x65

    def screen_on(self, on: bool = True) -> bool:
        """0x69: turn the screen on ([0x230086BC+0x513]=1 when pkt[4]!=0)."""
        r = self.transact(0x69, 0x00, bytes([0x01 if on else 0x00]), echo=0)
        return bool(r) and r[0] == 0x69

    def set_brightness(self, value: int) -> bool:
        """0x68: the brightness 0..100 (100 at the end of the apply batch in the captures)."""
        assert 0 <= value <= 100
        r = self.transact(0x68, 0x00, bytes([value, 0x00]), echo=0)
        return bool(r) and r[0] == 0x68

    def set_page(self, page: int) -> bool:
        """0x61 sub 0: change the screen page (0..5; IPC {0x13, dev, page})."""
        assert page <= 5
        r = self.transact(0x61, 0x00, bytes([page]), echo=0)
        return bool(r) and r[0] == 0x61

    def commit(self) -> bool:
        """`50 55 00 00`: commit the batch - apply the RGB state + flush
        the FB queue to the iface2 IN (PROTOCOL_OLED.md §5). Ends the
        GearLink-style config batch. The reply is the echo `50 55`."""
        r = self.transact(0x50, 0x55, b"", echo=0)
        return bool(r) and r[0] == 0x50 and r[1] == 0x55

    def gearlink_session(self, on: bool) -> bool:
        """0x74 sub 0: the "vendor session active" flag [0x2301D21C].

        on=True enables `C0 81` (memory reads via sysctrl); 0x27 just
        reads this flag. Do not confuse with unlock() (the magic -> bootloader!).
        """
        r = self.transact(0x74, 0x00, bytes([0x01 if on else 0x00]), echo=0)
        return bool(r) and r[0] == 0x74

    def ping_gearlink(self) -> bool | None:
        """0x27: read the vendor-session flag ([0x2301D21C])."""
        r = self.transact(0x27, 0x00, b"")
        if r and r[0] == R_BOOL:
            return bool(r[4])
        return None

    # ---------- OLED: uploading a 184x97 RGB565 image (PROTOCOL_OLED.md §3-4) ----------
    PANEL_W = 184
    PANEL_H = 97
    CHUNK_PAYLOAD = 58          # the bitmap-stream bytes per `61 02` packet
    BITMAP_META = b"\x01\x00\xe8\x03"   # the fixed stream header (see §3.5)

    @staticmethod
    def rgb565_le(r: int, g: int, b: int) -> bytes:
        """An RGB888 pixel -> RGB565 little-endian (2 bytes, the low byte first)."""
        px = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return struct.pack("<H", px)

    def upload_image(self, pixels: bytes, width: int = PANEL_W, height: int = PANEL_H,
                     brightness: int = 100, select: bool = True,
                     progress=None) -> int:
        """Upload a custom picture to the OLED (the protocol of the 06-custom-image captures).

        pixels: width*height RGB565 **LE** pixels (2 bytes/pixel, the low byte
        first - the order is confirmed by the splash, PROTOCOL_OLED.md §4).
        The sequence: `6A 00 [0] 01` (enable slot 0) -> `6B 00` ->
        `61 01 <n u32 LE>` -> n×`61 02 <idx LE u32>` (idx from n-1 down to 0,
        58 B of the stream each; the stream = the meta `01 00 e8 03` + pixels
        + padding) -> `61 03` -> `6A 01` (select slot 0) -> `68` (brightness)
        -> `50 55` (commit).
        Returns the number of sent chunks. progress(i, n) - an optional callback.
        """
        assert len(pixels) == width * height * 2, "an RGB565 LE buffer of w*h*2 is required"
        stream = bytearray(self.BITMAP_META + pixels)
        n = (len(stream) + self.CHUNK_PAYLOAD - 1) // self.CHUNK_PAYLOAD
        stream += b"\x00" * (n * self.CHUNK_PAYLOAD - len(stream))
        self.set_widget(0, True)
        self.send(0x6B, 0x00, b"")
        self.send(0x61, 0x01, struct.pack("<I", n))
        sent = 0
        # the order as in the capture: the data streams in stream order (the meta
        # goes into the first packet), while the chunk index in the echo DECREASES
        # from n-1 to 0 (the device only checks monotonicity and pushes the data
        # into the ring in arrival order)
        for pos in range(n):
            idx = n - 1 - pos
            chunk = stream[pos * self.CHUNK_PAYLOAD:(pos + 1) * self.CHUNK_PAYLOAD]
            self.send(0x61, 0x02, b"\x00\x00" + chunk, echo=idx)
            sent += 1
            if progress and (sent % 32 == 0 or idx == 0):
                progress(sent, n)
            if sent % 16 == 0:
                self.recv(0)            # drain the ACK channel
        self.send(0x61, 0x03, b"")
        if select:
            self.select_slot(0)
        self.set_brightness(brightness)
        self.commit()
        return sent

    def banner_begin(self, width: int, height: int = 48) -> bool:
        """0x67 sub 0 (arg=2): start a "banner" session of slot 0.

        width 136..680, the height must be 48. The chunks follow via
        banner_chunk(). `67 00 <echo> 01` - cancel (banner_cancel)."""
        assert 136 <= width <= 680 and height == 48
        args = bytes([0x02]) + struct.pack("<HH", width, height)
        r = self.transact(0x67, 0x00, args)
        return bool(r) and r[0] == 0x67

    def banner_chunk(self, idx: int, data: bytes) -> bool:
        """0x67 sub 1: a banner chunk, idx decreases from 47 to 0 (echo = idx)."""
        assert len(data) == self.CHUNK_PAYLOAD
        self.send(0x67, 0x01, b"\x00\x00" + data, echo=idx)
        return True

    def banner_commit(self, params: bytes = b"\x00" * 28) -> bool:
        """0x67 sub 2: commit the banner -> [0x23014C34]=1 (the special slot 0
        render), auto-hide after 30 ticks; params - 28 bytes of pkt[4..0x1F]."""
        assert len(params) <= 28
        r = self.transact(0x67, 0x02, params.ljust(28, b"\x00"))
        return bool(r) and r[0] == 0x67

    def banner_cancel(self) -> bool:
        """0x67 sub 0 (arg=1): reset the slot 0 banner."""
        r = self.transact(0x67, 0x00, bytes([0x01]))
        return bool(r) and r[0] == 0x67


    # ---------- RGB ----------
    @staticmethod
    def rgb12_to_rgb888(c12: int) -> tuple[int, int, int]:
        r = (c12 >> 8) & 0xF
        g = (c12 >> 4) & 0xF
        b = c12 & 0xF
        return (r << 4) | r, (g << 4) | g, (b << 4) | b

    @staticmethod
    def rgb888_to_rgb12(r: int, g: int, b: int) -> int:
        return ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)

    def get_key_color(self, key: int, layer: int = 0) -> tuple[int, int, int] | None:
        """0x25/0x04: the color of a single key. key - the LED/HID index (see luts.json).

        (In the older client versions and docs it was mistakenly listed as
        "0xFD 0x04" - in fact the RGB get-family is the opcode 0x25.)
        The format is confirmed by the disassembly of 0x0E07BC18 on 2026-10-03:
        there is an echo, key@offset 4, layer@offset 5 (0x00/0x9F) - unlike
        set-color, which has no echo and has [key][layer] at offset 2..3.
        """
        layer_arg = 0x9F if layer else 0x00
        r = self.transact(0x25, 0x04, bytes([key & 0xFF, layer_arg]))
        if r and r[0] == R_LED and len(r) >= 6:
            c12 = struct.unpack_from("<H", r, 4)[0] & 0xFFF
            return self.rgb12_to_rgb888(c12)
        return None

    def set_key_color(self, key: int, r8: int, g8: int, b8: int,
                      layer: int = 0, time10: int = 0, sub: int = 0x21,
                      spec: int | None = None) -> bool:
        """0x51 sub 0x21/0x22: the color of ONE key, 12 bits (set-color 0x0E07BFC0).

        Fixed on 2026-10-03 per the disassembly of 0x0E07BFC0: set-color has NO
        echo field - the packet [51][21/22][key][layer][spec u16][color u16][time u16]:
        key at offset 2 (0..0xBC or the special code 0xD3), layer at offset 3
        (0x00 = layer 0, 0x9F = layer 1/Fn). The "Range" of the old version is
        spec, the encoding of an immediate write to the color table (by default
        spec = color: at >= 0x306 it is a direct 12-bit color); the profile
        record color is always pkt[6..7]. The 0x51 ack carries key | layer<<8
        in the echo field, so transact() with echo matching does not fit here.
        sub 0x21 does not touch time, sub 0x22 also writes time (pkt[8]/10).
        """
        layer_arg = 0x9F if layer else 0x00
        c12 = self.rgb888_to_rgb12(r8, g8, b8)
        if spec is None:
            spec = c12
        pkt = struct.pack("<BBBBHHH", 0x51, sub & 0xFF, key & 0xFF, layer_arg,
                          spec & 0xFFFF, c12, (time10 * 10) & 0xFFFF)
        pkt += b"\x00" * (64 - len(pkt))
        self.dev.write(WRITE_PREFIX + pkt)
        want = (key & 0xFF) | (layer_arg << 8)
        deadline = time.time() + 1.0
        while time.time() < deadline:
            r = self.recv(200)
            if not r:
                continue
            if r[:2] == R_NAK_HDR:
                return False
            if r[0] == R_SETACK and r[1] == (sub & 0xFF) and \
               struct.unpack_from("<H", r, 2)[0] == want:
                return True
        return False

    # ---------- bootloader ----------
    def enter_bootloader(self):
        """Sends the magic and waits for re-enumeration (the device will disconnect)."""
        self.unlock()

    # ---------- volume / OSD (see analysis/PROTOCOL_VOLUME.md) ----------
    def send_volume_osd(self, volume: int, store: bool = False) -> bool:
        """0x51 sub 0x0C: draw the volume OSD on the OLED for the rocker.

        volume - percent 0..100 (the step is 2 per rocker click in the capture).
        echo=0 (the default) - draw now (IPC 0x26 "V%" + IPC 0x16,
        auto-hide after ~1 s; works when [0x23000CA0]==0).
        store=True (echo=1) - only store the percent in [0x23013A4C+2].
        The reply is the echo `51 0C <echo> <V>`.
        """
        assert 0 <= volume <= 100
        r = self.transact(0x51, 0x0C, bytes([volume]), echo=1 if store else 0)
        return bool(r) and r[0] == R_SETACK and r[1] == 0x0C

    def get_key_assignment(self, page: int, usage: int) -> int | None:
        """0x25 sub 0x0C: the rocker special-key assignment on a profile page.

        NOT the volume! page 0..0x0A; usage - the HID usage of the special key:
        0xA3..0xA6 -> the matrix 0xD3..0xD6 (the rocker). Returns the stored
        assignment code (488/489 = vol-/vol+, 498 = push? in the capture)
        as u16; None on NAK.
        """
        assert 0 <= page <= 0x0A
        args = struct.pack("<BH", page, usage)
        r = self.transact(0x25, 0x0C, args)
        if r and r[0] == R_LED and r[1] == 0x0C:
            return struct.unpack_from("<H", r, 4)[0]
        return None

    def open_consumer(self):
        """Open iface2 (consumer control, usage page 0x0C) for reading
        the IN events of EP 0x83: `01 00 <bitmap20>` and the vendor `03 7x/03 91`."""
        for d in hid.enumerate(VID, PID):
            if d["usage_page"] == 0x0C:
                dev = hid.device()
                dev.open_path(d["path"])
                dev.set_nonblocking(False)
                return dev
        raise IOError("consumer interface (usage page 0x0C) not found")

    def open_ffc0(self):
        """Open the 0xFFC0 channel (Col03 of iface2, 20-byte reports with Report ID 3).

        The keyboard event mirror lands there: `03 93 <slot>` (a slot change),
        `03 95/96 00 00 30` (the widget status / a local change — empirically
        on 2026-10-04: one `03 96` per each accepted vertical swipe),
        `03 71 <the touch matrix>` (the touchscreen data). WARNING: on Windows
        iface2 has three collections with different device paths — the Col01
        (consumer) handle does NOT receive this stream."""
        for d in hid.enumerate(VID, PID):
            if d["usage_page"] == 0xFFC0:
                dev = hid.device()
                dev.open_path(d["path"])
                dev.set_nonblocking(False)
                return dev
        raise IOError("the 0xFFC0 channel (usage page 0xFFC0) not found")


def emulate_gearlink(kbd: M901, volume: int = 50, timeout_s: float | None = None) -> None:
    """A minimal GearLink replacement in the rocker volume loop.

    Reads EP 0x83 (iface2): an up tick = the consumer `01 00 04` and/or the
    vendor `03 72 01`, down = `01 00 02` / `03 72 04`, a release =
    `01 00 00` / `03 72 00`.
    On every tick it sends `51 0C 00 00 [V]` (a step of 2, as in the capture
    03b-jogdial) - the volume OSD appears on the OLED. Ctrl+C to exit.
    """
    cons = kbd.open_consumer()
    step = 2
    print("jogdial volume loop: V=%d%%, Ctrl+C to exit" % volume)
    try:
        deadline = time.time() + timeout_s if timeout_s else None
        while True:
            data = cons.read(64, timeout_ms=200)
            if data:
                rid = data[0]
                if rid == 0x01 and len(data) >= 3:
                    bits = data[2]
                    if bits & 0x04:
                        delta = +step   # bit10 = Volume Increment (0xE9)
                    elif bits & 0x02:
                        delta = -step   # bit9 = Volume Decrement (0xEA)
                    else:
                        delta = 0       # a release (00) or an unrelated bit
                    if delta:
                        volume = max(0, min(100, volume + delta))
                        kbd.send_volume_osd(volume)
                        print("consumer tick %s -> V=%d%%" % (
                            "up" if delta > 0 else "down", volume))
                elif rid == 0x03 and len(data) >= 3 and data[1] == 0x72:
                    names = {0x01: "up", 0x04: "down", 0x02: "press", 0x00: "release"}
                    print("vendor 03 72 %s" % names.get(data[2], hex(data[2])))
                elif rid == 0x03 and len(data) >= 5 and data[1] == 0x91:
                    print("vendor 03 91 phase=%d" % data[4])
            if deadline and time.time() > deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        cons.close()


def main():
    import sys
    dev = M901()
    print("connected:", dev.dev.get_product_string())
    print("status flags (0x24/0x02):", dev.get_status_flags())
    print("current slot (0x24/0x01):", dev.get_current_slot())
    print("current page (0x21/0x00):", dev.get_current_page())
    print("screen state (0x23/0x02):", dev.get_screen_state())
    print("battery (0x12/0x01):", dev.get_battery())
    st = dev.get_status_struct()
    if st:
        print("status struct (0x12/0x00): temp=%d cpu=%d ram=%d dev=%d page=%d slot=%d" % (
            int.from_bytes(st[0:2], "little"), int.from_bytes(st[2:4], "little"),
            int.from_bytes(st[4:6], "little"), st[6], st[7], st[9]))
    if len(sys.argv) > 1 and sys.argv[1] == "--boot":
        print("sending magic -> device reboots to bootloader")
        dev.enter_bootloader()
    elif len(sys.argv) > 1 and sys.argv[1] == "--volume":
        # the rocker volume loop without GearLink: python m901_client.py --volume [initial %]
        v0 = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        emulate_gearlink(dev, volume=v0)
    elif len(sys.argv) > 1 and sys.argv[1] == "--osd":
        # a one-shot volume OSD: python m901_client.py --osd [0..100]
        v = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        print("volume OSD %d%% ->" % v, dev.send_volume_osd(v))
    elif len(sys.argv) > 1 and sys.argv[1] == "--jogdial-map":
        # the rocker assignments (proof that 25 0C is not the volume):
        # python m901_client.py --jogdial-map [page]
        page = int(sys.argv[2], 0) if len(sys.argv) > 2 else 9
        for usage in (0xA3, 0xA4, 0xA5, 0xA6):
            print("page %d usage 0x%02X -> %s" % (
                page, usage, dev.get_key_assignment(page, usage)))
    elif len(sys.argv) > 1 and sys.argv[1] == "--wake":
        # wake the OLED without GearLink: python m901_client.py --wake
        print("wake_display ->", dev.wake_display())
    elif len(sys.argv) > 1 and sys.argv[1] == "--clock":
        # set the current local time: python m901_client.py --clock
        t = time.localtime()
        print("set_clock ->", dev.set_clock(t.tm_year, t.tm_mon, t.tm_mday,
                                            t.tm_hour, t.tm_min))
    elif len(sys.argv) > 1 and sys.argv[1] == "--cpu":
        # a metrics push: python m901_client.py --cpu [usage] [temp]
        u = int(sys.argv[2]) if len(sys.argv) > 2 else 42
        tp = int(sys.argv[3]) if len(sys.argv) > 3 else 55
        print("send_cpu_usage ->", dev.send_cpu_usage(u, tp))
    elif len(sys.argv) > 1 and sys.argv[1] == "--image":
        # upload a raw RGB565 LE file (w*h*2 bytes): python m901_client.py --image frame.raw
        with open(sys.argv[2], "rb") as f:
            raw = f.read()
        w, h = 184, 97
        assert len(raw) >= w * h * 2, "the file is shorter than 184*97*2"
        n = dev.upload_image(raw[:w * h * 2],
                             progress=lambda i, t: print("chunk %d/%d" % (i, t)))
        print("uploaded %d chunks" % n)


if __name__ == "__main__":
    main()
