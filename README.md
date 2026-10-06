# Azoth Companion v0.3 — a GearLink replacement prototype

**File:** `azoth-companion.py` (transport — `analysis/m901_client.py`).
**Updated:** 2026-10-05 (v0.3: `--slides`, `--log-file`, autostart; the protocol
logic is unchanged). Previous findings — 2026-10-04, GearLink captures
(`C:/azoth-capture/16-gearlink-startup.pcap`, `18-gearlink-minimal.pcap`).

## The protocol model (verified against a live session + captures)

| Widget | Slot | Content | Command |
|---|---|---|---|
| Clock | 1 | date+time; the dial ticks by itself | `63 00 <echo> 00 <date u32 LE> <time u16 LE>` — a push at startup, a re-sync every 10 min |
| PC battery | 2 | percent | `64 00 <echo> <pct> 00` — a push on change |
| CPU monitoring | 3 | {sel, digit, u16} pairs | `66 00 01 00 <selA digA valA u16> <selB digB valB u16>` — a push on change |
| Banner/music | 0 | the GearLink frame stream | `67 00 02 01 <w u16> <h u16> <param>` → `67 01` chunks → `67 02` ~16 fps — **not used by the prototype** |
| Carousel | 4 | see "Slot 4 — what was learned" | leave alone |

The `0x66` selectors: the hi-nibble = the header (0=CPU, 1=GPU, 2=VRM, 3=DRAM, 4=CHA),
the lo-nibble = the label (0=Usage, 1=Temp., 2=Freq., 5=Volt). GearLink pushes only the CPU:
a single `{01 00 <freq>}` / a double `{00 00 <usage> | 01 00 <temp>}`.

### Single widgets — the GearLink way (capture 19, 2026-10-04)

The "switched the double CPU+temp to a single one" scenario in the traffic:

```
6a 00 00 00 03 00     disable slot 3 (the double one)
6a 00 00 00 04 01     enable slot 4 (the carousel)
6a 01 00 00 04 00     select slot 4
68 00 00 00 64        brightness
50 55 00 00           commit
```

- **There are NO 0x66 pushes after the switch** — the single tile renders the
  records already stored in the table (after the double usage+temp the screen
  kept showing "CPU0 Temp"). There is no separate "single mode" command in the
  protocol.
- Switching the widget type "double → single" without reconnecting the
  keyboard went rough even for GearLink (repeated slot 3↔4 flapping at
  t=43..104 s); the user resolved it by re-plugging the USB.
- Swipes on that slot did not work — gated by `65 FF` (wake), normal for
  GearLink as well.
- A bonus find in the capture (t=106): the "custom picture" view = slots
  {0 on, 1-3 off, 4 on}, select 0, `61 00 01` (page 1), **`51 60 3F 00`**
  (the status-bar mask in sysctrl, mode 0x14) + `6B 00` (begin bitmap) —
  the first live example of `51 60` being used.
- The KPS counter (key presses/sec) is a native carousel tile; the host
  does not control it.

### 0x66 tile geometry — a probe experiment (2026-10-04, session 2)

Probes with recognizable values in different positions of the push (11/22 in
the second pair, 33/44 in the first, sel 0x30 in both cases) showed:

- **Tiles are bound to the type (selector), not to the pair's position in the
  push**: DRAM0 rendered with the same tile regardless of position; the
  neutral pair `{00 00 0000}` always shows up as "CPU0 Usage 0" in the second
  cell.
- **The monitoring layout is always two tiles.** GearLink's "single" mode is
  a second tile with zeros (the carousel pages pick which metric is live).
- Consequence: a "slideshow of singles" via 0x66 is impossible; the sensible
  modes are `--metrics cpu-ram` (both cells live, recommended) or alternating
  metrics on the top tile with a zero below (as GearLink does in capture 12).
- A truly single tile is a separate carousel mechanism (pages 0..4,
  RAM @0x23019014) — outside the 0x66 host protocol, a subject for separate RE.

### Mac/PC — status

The reverse-engineered protocol has NO mac/pc command. Candidates for where
the mode is stored: the bits of byte `[0x2301D27E]` (the status-bar icon pairs
0/1, 2/3, 4/5 — one of them is mac/pc; the writers are RAM code),
`51 21/22`, the iface1 service channel 0xFFC0.
Plan: capture the Mac/PC switch in the GearLink UI (if it exists) — the
command will show up in the traffic. Auto-switching on Mac: the prototype
carries over (hidapi is cross-platform, WRITE_PREFIX is already
per-platform) — the Mac agent will set the mode itself on connect.

### Slideshow and carousel — how it actually works (capture 20 + live tests 2026-10-04)

- GearLink's slideshow = single `0x66` pushes with alternating selectors
  (Usage ↔ Temp, every ~1-3 s), **without** slot/page changes. Our
  `--slideshow` is the same mechanism, byte for byte.
- **CRITICAL: `0x66` has two paths depending on echo** (STATUSBAR §4,
  confirmed by a live test): `echo=1` → the path `0x0E08D65A` — "noisy":
  every push snaps the slot 4 carousel back to the first page (the double
  tile) and "eats" swipes; `echo=0` → the quiet IPC `0x1E` — the content
  updates, the carousel stays where it is. GearLink's slideshow pushes go
  with `echo=0` (capture 20), the config pushes with `echo=1`.
  Our `push_metrics()` sends with `echo=0` by default.
- The carousel pages (2 singles + the double) read ONE tile table: the values
  update on all pages at once. The carousel is paged only by swipes
  (locally; there is no host page command).
- FD `A2 <v>` ("mode switch" from the reverse engineering, `[0x2301D26C]=v`)
  — no effect on the screen/status bar at v=0/1. Not mac/pc.
- **Mac/PC is fully keyboard-internal state**: Fn+Tab generates not a single
  USB packet; regwatch (a diff of all the 0x12/21/23/24/25/27 getters across
  presses) showed changes only in the drifting RSSI/voltage byte
  (`12.01` payload[7], the 0x18–0x2D sawteeth). There is no host protocol for
  mac/pc — the mode is stored in the keyboard and survives reconnects;
  there is nothing for the prototype to touch.
- The display falls asleep on its own timeout if nobody pushes: the sleeping
  screen "swallows" swipes and the tiles freeze. A working prototype feeds
  the screen continuously (the values update every ~2 s) — that is why, with
  the prototype stopped, the carousel looks dead. The startup `65 FF` arms
  the gesture gate for ~30–60 s (like GearLink) — the swipes come back to
  life after it decays.
  ⚠ Per the code it is the other way around (`PROTOCOL_GESTURES.md` §2.5):
  `65 FF` (v>2) CLEARS the gate, `65` with v≤2 sets it; the observed "dead
  window" is the dispatcher tail while the screen wakes up. Live test: after
  the "dead window", send `65 FF` — the swipes should revive instantly.

### The iface2 channel — the right listener and the event map (evtlog, 2026-10-04)

- **On Windows iface2 has three collections with different device paths**
  (Col01 consumer 0x0C, Col02 system control, Col03 vendor 0xFFC0).
  `open_consumer` opens Col01, while the whole mirrored event stream lives on
  **Col03 (0xFFC0)**. Tool: `analysis/evtlog.py` (listens to both).
- The event map of `03 <type> ...` (report ID 3):
  - `03 95 00 00 30` — the widget-state heartbeat (~0.86 s; it freezes during
    swipes and recovers after ~5 s);
  - `03 96 00 00 <slot<<4|flags>` — a swipe DOWN (only it gets sent — that is
    why it is visible on the bus);
  - `03 94` — a touchscreen double-tap; `03 93 00 00 <slot>` — a slot change
    (LEFT/RIGHT);
  - `03 71` — a mirror of the key states (a bitmap; byte 2 = the modifiers:
    04 = LAlt during Alt+Tab); sporadic bursts = palm touches on the keys.
    The early "touchscreen stream" hypothesis was NOT confirmed (2026-10-05).
  (the map was refined on 2026-10-05 per `PROTOCOL_GESTURES.md`; earlier 95/96
  were considered one "state mirror ~1.5 Hz").
- **"Vertical swipes are unreliable" — explained (2026-10-05)**: a swipe DOWN
  reliably produces `0396`; UP sends 0x95, which is byte-for-byte identical
  to the heartbeat and is not distinguished by the host at all. "Some swipes
  gave 0x93" — gestures that slipped sideways. A timer-driven slideshow makes
  the vertical swipes optional.

### Swipe direction — settled: only DOWN is visible to the host (2026-10-05)

Full sessions with labeled swipes (`analysis/dircam.py` — a continuous reader
of both iface2 channels with ms timestamps; the logs `analysis/scan_swipe.log`,
`analysis/scan_up.log`):

- **`03 96` = one accepted swipe DOWN**: 10 down → 8 events, 3 down → 3.
  The payload is constant (`00 00 30 00…`) — there is no direction in it.
- **A swipe UP is undetectable by the host**: a clean session of >10 up →
  zero events (no 0396, no 0394, no 0371, no consumer frames) while the
  background 0395 stream at ~1 Hz was alive. The user confirms the UX:
  down pages, up does not. There is no "neighbor" on the wire.
- **Confirmation on slot 0 (gifs)**: the session "8 up, 8 down, 8 up" →
  exactly 8× `0396` from the MIDDLE (lower) group (`scan_gif.log`); both
  groups up — zero, even though the gifs page locally in both directions.
  I.e. even where both directions do something on the device, only down is
  reported to the host. Byte 4 of `0396` = the slot/page context: `30` —
  monitoring, `00` — banner/gifs, `20` seen in an early session.
- The `0395` stream freezes during gesture interaction and resumes ~5 s after
  the last accepted swipe (a convenient marker of the "live" window). The
  first swipes after an idle period go out silently (the wake gate) — not a
  single frame.
- Capture 20 (GL): the 01-frames on Col01 are a mirror of the HID usages
  (`04 00 2b` = Alt+Tab, 0x05-0x39 = keys), `0361/0372` = the Fn combos
  (Fn+Tab), `0394` — twice, semantics unknown (logged by the prototype for
  the future).
- There is no "last gesture" register: `analysis/swipereg.py` (polling
  12.00-0x1F, 0x20-0x2B during swipes) — only the battery bytes change.

**The decode from the code (2026-10-05, `analysis/PROTOCOL_GESTURES.md`) —
refines everything above:**

- UP is fully processed (decoder mode 3 → slot 3 → 0x0E08D868), but it sends
  the IPC envelope **0x95** — the same type and the same payload collector
  ({slot<<4|flags, sub}) as the periodic 0395 stream. The gesture has no
  signature whatsoever: an UP frame is byte-for-byte equal to the heartbeat
  (a post-analysis of the logs: EVT-96 in the out-of-Tick phase, the "extra"
  0395s on the noisy 0.70–1.26 s ticker are indistinguishable). This is a
  firmware asymmetry, not lost gestures; it cannot be enabled by
  configuration. "Byte 4 = the slot context" is confirmed by the code: it is
  the collector's `slot<<4|flags` (30/20/00 = the slots 3/2/0).
- `0394` = a touchscreen **double-tap** (0x0E08DB58): a toggle of bit0
  [0x23014B49] + a flash save + the same collector. A candidate for
  "paging BACK" in the prototype (currently 0394 is logged and ignored);
  the bit0 semantics needs a live check (a candidate for "screen on/off").
- The direction map: UP=0x95, DOWN=0x96, LEFT=slot+1 (`0393 <slot+1>`),
  RIGHT=slot−1 (wrap 0→4). Vertical gestures on slot 4 **reset** the KPS ring
  @0x23019014 (0x0E093F58) rather than paging it — the "carousel" is the KPS
  graph of the slot 4 tile, not pages. The freezing of the 0395 stream during
  swipes = the gesture path monopolizes sending (consistent with the
  observation above).
- The wake gate per the code: `65` with v≤2 **sets** the gate [0x2301D249],
  with v>2 (including our `65 FF`) — **clears** it; the "deaf" first swipes
  after an idle period are the dispatcher tail while the screen wakes up
  ([0x23002348]==0 → gate=1). This contradicts the line about "65 FF arms the
  gate" above — re-verify live; per the code it is the other way around.

⇒ Azoth Companion: paging FORWARD only, driven by 0396 (exactly like
GearLink). In the prototype: 0396 → the next slide + an auto-paging pause of
`SWIPE_PAUSE_S = 5` s.

### The volume rocker — the handler in the daemon (2026-10-05)

The loop breakdown: `analysis/PROTOCOL_VOLUME.md` §0–§2. The roles are split:
the rocker sends consumer events (`01 00 04/02 00` on Col01) — Windows
CHANGES THE VOLUME ITSELF; GearLink merely reads the new OS value and pushes
`51 0C 00 00 [V]` (V = %, the echo must be 0x0000 — the OSD branch;
echo=1 = a write-only copy without an OSD). The keyboard draws "V%" and
hides the OSD itself after 1 s.

In the daemon: a tick = `03 72 01` (vol+) / `03 72 04` (vol−) on 0xFFC0; the
reader thread hands the tick to the worker immediately, and the worker pushes
`51 0C` on EVERY tick with a PREDICTED "cache ± 2%" value (without waiting
for Windows; the background `WindowsVolume` poller at ~10 Hz continuously
reconciles the cache with the OS). The tick direction is passed to the worker
(`tick(up)`); a failed push is retried by the next pass. The push is quiet
(no robust_push wake-up); transact runs under the lock.

**The blink of the first rocker series** (a series of experiments
2026-10-05, the user):
- it reproduces with NO host at all (the daemon stopped) — this is a
  fallback of the firmware itself: if a tick is not acknowledged by the host
  quickly enough, it draws its own indicator by re-laying out the screen;
- GearLink has it too (a vestige: "very short, almost invisible to the
  eye"); after stopping GearLink the blink comes back — the "warm" state of
  the path is not preserved; only an instant reaction to every tick creates it;
- warming up with a single `51 0C` push at daemon startup does NOT help;
- a session with TXLOG (a log of all the transactions, ms): at the moment of
  the blink there were ZERO NAKs and zero timeouts — all the `51 0C`s were
  ACKed within 1-2 ms, the host is completely clean; the hypothesis "some
  NAK reboots the display" is ruled out;
- holding the rocker (auto-repeats without the release edges `03 72 00`) does
  NOT blink, while discrete clicks do → the dimming is tied to the firmware's
  gesture path (the press edges); during a click series the display is
  suspended as a whole ("it lights up no earlier than the end of the
  burst"), this is not per-pixel flicker;
- bottom line: strictly a firmware cosmetic; it cannot be fixed in Azoth
  Companion without decompiling the sysctrl OSD path; the UX impact is
  minimal.

**Hold and intermediate values** (2026-10-05): auto-repeats while held are
NOT sent to the mirror — exactly one `03 72` per press, silence until
release (the dump). Firing fresh OS values (a 0.6 s window, `--vol-hz`,
the default 12) yields the exact final value right after the release; during
a hold there are only the first 1-2 intermediate updates, and this is NOT a
rate limit (25 Hz and 12 Hz give the same "2 → … → 100%" picture).
**Localized via the decompile**: the keyboard side is flawless — the echo-0
branch at 0x0E07DF8C was disassembled, the only gate [0x23000CA0] is held
live at 0, the formatter 0x0E08C4E4 and the timer 0x0E08C548 are
unconditional → the IPC 0x26 (draw) goes out on EVERY push.
**A user observation (a long hold)**: the composer does NOT stall — during a
long hold the OSD layer disappears while the selected widget
(monitor/gif/KPS) keeps rendering. I.e. sysctrl accepts the draws, but after
the first 1-2 frames the OSD layer hides and does not show again until the
end of the episode; the final frame is drawn on release (the episode resets
the layer state). The show/hide/suppress mechanics of the OSD layer live in
the not-yet-decompiled consumer of IPC 0x26/0x16 on sysctrl (see
prompts/05-sysctrl-osd.md).

**Unmute → a level OSD** (a feature GL cannot do): the mute key bypasses the
iface2 mirror (capture 04-mute: it looks like a regular iface1 key), so we
detect the STATE FLIP through Windows: the `WindowsVolume` 10 Hz poller reads
GetMute (vtable 15) and, on the muted→unmuted transition, pushes `51 0C` with
the level (`VolumeWorker.push_level`). 0% while muted is a real zero
(0% ≠ mute in Windows); the "Mute/Unmute" caption on the OLED itself is the
firmware's native indicator, and our push after unmute is drawn with a "+"
(firmware handwriting, correct).

**"Settling" after a series** (the hold lag): the predicted cache values may
lag behind the OS during auto-repeat; 0.3 s after the last tick the worker
pushes the EXACT value (`WindowsVolume.fresh()`, a direct COM call) — the
final plate always equals the OS volume.

### Display sleep and blinks — the mechanics (2026-10-04)

- The screen falls asleep after ~30 ticks without **changing** data:
  event-driven pushes at stable values leave gaps, the screen sleeps → the
  display family `0x61-67` NAKs → a blink (a black frame on wake-up).
  GearLink doesn't sleep because its slideshow pushes guaranteed-changing
  data every 1-3 s. The prototype's solution: a heartbeat `HEARTBEAT_S=10`
  (an unconditional push) + `robust_push` with DISTINGUISHED failure modes:
  a real NAK `FF AA` (the sleep gate) → `65 FF`+`69`+retry (with a short
  blink, only when necessary); a timeout/silence (the device is busy
  rendering) → a quiet retry after 0.3 s without a wake-up. The failure-mode
  flag is `M901.last_nak` (set in transact).
- The residual rare blinks, per the user's observation, coincide with the
  per-minute `0x63` push (a full re-render of the clock text) — the price of
  an accurate clock; if it gets in the way, we'll return to the rare sync.
- **NAK ≠ "the screen is asleep"** (refined 2026-10-05): `FF AA` also arrives
  on a BUSY path — the first rocker burst monopolized the OSD path, the
  parallel `0x66` pushes NAKed for the whole series, and the old robust_push
  woke the screen on every NAK → a blink lasting the entire series. The
  rule: wake (`65 FF`+`69`) only on a NAK + an EXPLICITLY off screen
  (`0x23/02 == 0`, `screen_is_on()`); a NAK with the screen on, and
  timeouts, = a quiet retry.
- **The anatomy of "wake" per the decompile (2026-10-05, testing the "it's a
  reset/reheal" hypothesis)**: the hypothesis was NOT confirmed, but the
  mechanics surfaced:
  - the `65` handler (case 0x65 of the mega-handler 0x0E08CE6C) — ONLY the
    IPC `{0x1F, v}` + a toggle of the gesture gate (v≤2 →
    [0x2301D249]=1 "gestures present", v>2 → =0); no display operations;
  - on the sysctrl synchronizer, a mode byte of 0xFF = "nothing" — i.e.
    `65 FF` is a NO-OP for the display pipeline; a real RESET is a mode >0x27
    (unavailable to us, we never write there);
  - the real screen switch is the flag [0x230086BC+0x513]: `69` sets it to 1,
    `61 01`/the banner branches set it to 0 (this is exactly what "falling
    asleep" is: the mega-handler gate `[+0x513] != 1 → return 1` = our NAKs);
    the sync tick 0x0E08C800, upon a CHANGE of the flag, sends the IPC
    `{0x11, flag, u16}` — that is the true carrier of on/off to sysctrl (the
    panel is dimmed/lit by it);
  - the sync tick also self-heals: `[+0x513]==0 → =1` in one of the branches;
  - the black frame on wake-up is the panel's 0→1 transition (a re-render/the
    first frame), not a reset by command. Conclusion: `65 FF` is useful only
    through clearing the gesture gate; it is `69` that turns the screen on.
- The dim mode (a brightness reduction from idle) does NOT interfere with
  updates.

### Slot 4 — what was learned (live test 2026-10-04)

- The hypothesis "slot 4 = a single tile" was **NOT confirmed**: when slot 4
  is selected the screen shows the same double 0x66 tile layout + the
  **native KPS counter** (key presses per second — keyboard statistics, not
  host-controlled).
- Pushing single 0x66 pairs on slot 4 changes only the FIRST (top) tile; the
  second keeps the previous selB (our "neutral" `{00 00 0000}` renders as
  "CPU0 Usage 0").
- Confirmed earlier: "DRAM0 Usage" (sel `0x30`) renders and updates — the
  RAM widget works on the double tile as the second pair.
- **An open question for the next session**: how did GearLink draw single
  widgets (capture 12: it froze on "CPU0 Usage 10%")? Candidates: the
  carousel pages (an index 0..4 in RAM @0x23019014, the writers
  0x0E093F58/0x0E093F9E — on slot 4 they are paged by a vertical swipe) or
  rendering a single pair with an "empty" selB (which selB makes the second
  tile empty — enumerating 0x?? is forbidden by validation when hi>4, but
  the digit/value can be zeroed).
  ⚠ A correction (2026-10-05, `PROTOCOL_GESTURES.md` §4): the "carousel"
  @0x23019014 is the KPS history ring of the slot 4 tile (5 cells,
  IPC {0x25, total, max}); a vertical swipe on slot 4 RESETS it (0x0E093F58)
  rather than paging it. The "carousel pages" candidate for single widgets is
  out — what remains is an "empty selB" / the parameters of the second tile.

### The GearLink startup batch (capture 18, the minimal configuration)

```
12 01 / 12 00 / 12 14 / 24 01 …   state polls
65 00 00 00 FF                    wake: mode = 0xFF (NOT 0! 0 = the "Mail"/notification view)
27 00                             ping
66 00 01 00 …                     a metrics push (usage+temp)
64 00 00 00 63 00                 a battery push (doubled 0.5 s later)
63 00 00 00 00 EA 07 0A 04 01 1A  a time push
23 02 / 24 00..03 / 23 03 04      screen state reads
```

- GearLink sends the `6A` (mask) + `68` (brightness) + `50 55` (commit)
  commands ONLY when the widget set changes, not on regular value pushes.
- GearLink never sends the vendor-session flag `74`.
- The mask for the "clock+battery+CPU" configuration = `[0,1,1,1,0]`.
- **GearLink does not let you turn off the last widget** (at least 1 stays
  active); a zero mask, per the user's observation, hangs the OLED — the
  prototype never turns widgets off; keep this limitation in mind when
  extending.
- The on-screen clock ticks by itself; `0x63` is only a sync (startup +
  every 10 min).

### Pitfalls found by the live test of 2026-10-04

1. **Windows-hidapi requires `0x00` (the report ID) at the start of a
   write** — without it the packet "shifts" by a byte and the device answers
   NAK `FF AA 00 00`. In the client: `WRITE_PREFIX` (win32 only). Reads
   arrive without a prefix.
2. **`read(timeout_ms=0)` on Windows = infinite blocking** — the
   "non-blocking" drain must be done with `timeout_ms=1`.
3. **`65` with mode=0** opens the notification view (the "Mail" tile) — use
   only `65 FF`.
4. The screen may be left on another page (it was `page=5`): the widgets
   live on `page=1`; the fix is `61 00 01` (the client `set_page(1)`), but
   normally the page is not touched.
5. Touchscreen swipes get "swallowed" while the wake flag is active (`65`
   with v≤2 arms `[0x2301D249]`, `65 FF` clears it — per the code; the
   window of "deaf" swipes after the wake comes from the dispatcher tail)
   — the same behavior occurred with live GearLink too.

## Running

```
pip install hidapi psutil
python azoth-companion.py --status                # read-only
python azoth-companion.py --once --cpu 42 --temp 55 --bat 77   # a one-shot check
python azoth-companion.py --demo --bat 42         # a sensor test: 0→100→0 (~5.5 s each way)
python azoth-companion.py --events                # the working loop (+ the iface2 event log)
python azoth-companion.py                         # the working loop: --metrics (cpu-temp)
```

The `--metrics` modes (the double tile of slot 3, no slideshow): `cpu-temp`
(the default; without a sensor — usage only), `cpu-ram` (the second tile
"DRAM0 Usage"), `cpu`. Temperature: needs a running LibreHardwareMonitor +
`pip install wmi` (there is no ACPI thermal zone on the test machine).

### Slideshow and slides (v0.3)

```
python azoth-companion.py --slideshow 3           # a slideshow every 3 s, the default set
python azoth-companion.py --slides cpu.usage,cpu.freq,ram.usage --slideshow 5
python azoth-companion.py --slides cpu.usage,ram.usage   # --slides without --slideshow → a 2 s period
```

A slide = a single `0x66` push (echo=0, the GearLink mechanics §10.6):
auto-paging by timer; a swipe down (`03 96`) pages manually and puts the
auto-paging on a 5 s pause (`SWIPE_PAUSE_S`); a swipe up is not reported by
the firmware. The slide names use the "source.metric" format that repeats
the GearLink config grid; it is also the nibble layout of the 0x66 selector
(PROTOCOL_OLED.md §10.5: hi = the header {0=CPU, 1=GPU, 2=VRM, 3=DRAM,
4=CHA}, lo = the label {0=Usage, 1=Temp., 2=Freq., 5=Volt}):

| Name | Selector | Data source | OLED label |
|---|---|---|---|
| `cpu.usage` | 0x00 | psutil cpu_percent, % | CPU0 Usage |
| `cpu.temp` | 0x01 | LHM Temperature "CPU Package", °C | CPU0 Temp |
| `cpu.freq` | 0x02 | psutil cpu_freq().current, MHz | CPU0 Freq |
| `cpu.volt` | 0x05 | LHM Voltage "Vcore", **mV** | CPU0 Volt |
| `gpu.usage` | 0x10 | LHM Load "GPU Core", % | GPU0 Usage |
| `gpu.temp` | 0x11 | LHM Temperature GPU ("Core"/"Hot Spot"), °C | GPU0 Temp |
| `gpu.freq` | 0x12 | LHM Clock "GPU Core", MHz | GPU0 Freq |
| `gpu.volt` | 0x15 | LHM Voltage GPU, mV | GPU0 Volt |
| `ram.usage` | 0x30 | psutil virtual_memory, % | DRAM0 Usage |
| `ram.temp` | 0x31 | LHM Temperature (SODIMM/DIMM), °C | DRAM0 Temp |
| `ram.freq` | 0x32 | LHM Clock "Memory Clock", MHz | DRAM0 Freq |
| `ram.volt` | 0x35 | LHM Voltage (DIMM/DRAM), mV | DRAM0 Volt |

- The units were confirmed by capture 12 (§10.5): Usage=%, Temp=°C, Freq=MHz,
  **Volt=mV** (sel 05, val 525–981 = the laptop's Vcore).
- VRM (hi=2) and CHA (hi=4) are supported by the firmware but absent from the
  GearLink config grid — we don't set them.
- The default is `cpu.usage,ram.usage,cpu.freq` (the v0.2 set); duplicate
  names collapse; an empty/unknown `--slides` is a startup error listing the
  allowed sources/metrics. The short first-draft v0.3 names
  (`cpu`, `gpu`, `ram`, `usage`, `temp`, `freq`, `volt`) are accepted as
  aliases (`cpu` = `cpu.usage`, etc.).
- Sensor slides (all temp/freq/volt and gpu.usage — everything that is not
  psutil) without LHM: the slide is **skipped** with a single warning (the
  auto-paging moves on); `cpu.usage`/`ram.usage` without psutil — a stop.
  The manual `--cpu/--temp/--ram-val` take priority in cpu.usage/cpu.temp/
  ram.usage respectively.
- The LHM providers look up sensors by name (a per-slide priority list, see
  `HostSensors._lhm_pick`); the GPU/RAM sensors are platform-dependent — if
  LHM doesn't export them, the slide silently goes to skip after a single
  warning.
- **Double tiles — `a+b` pairs**: `--monitor-items
  cpu.usage+ram.usage,cpu.temp,cpu.fan` — a pair goes out as ONE `0x66`
  with two pairs (both cells live, the double tile). At most two halves;
  if one half has no sensor, the whole slide is skipped (a single warning
  per missing half). Aliases work inside pairs (`cpu+ram`).
- **The visible layout follows the echo of the last push** (live test
  2026-10-06): `echo=0` renders the top tile only (the second cell falls
  back to "CPU0 Usage 0" — the neutral pair), `echo=1` renders both cells.
  Therefore pair slides are pushed with `echo=1`, singles with `echo=0`.
  The "echo=1 eats swipes" note above does NOT reproduce at the slideshow
  cadence: 20× `0396` received during `echo=1` pushes every 2 s (a 30 s
  window). This also closes the "pairing value↔type" live test from
  PROTOCOL_OLED.md §10.3.2: the handler writes both pairs into the tile
  table; the second cell is just not rendered unless the push goes through
  the `echo=1` path.

### Log file (v0.3)

`--log-file [PATH]` — mirror into a file THE SAME lines that go to stdout
(not a redirection): UTF-8, size-based rotation ~2 MB, 3 files in total
(`azoth-companion.log`, `.1`, `.2`; the standard
`logging.handlers.RotatingFileHandler`). Without PATH —
`logs/azoth-companion.log` next to `azoth-companion.py` (the directory is
created); a relative PATH is resolved from the CWD. Important for pythonw:
there `sys.stdout` is absent and `print()` stays silent — the log lives only
in the file.

### Autostart (v0.3)

```
python azoth-companion.py --install-autostart     # create the "Azoth Companion" task
python azoth-companion.py --uninstall-autostart   # remove it
```

- A current-user scheduler task: starts at logon via `pythonw.exe` (no
  console window) with the flags `--slideshow 2 --log-file`; the daemon is
  not started at install/uninstall time.
- **A two-stage install** (live test 2026-10-05): the
  `schtasks /create /sc onlogon …` command is tried first, but its trigger
  is "ANY logon", and without admin rights it gets rejected ("Access is
  denied"). The code then registers an XML task
  (`schtasks /create /tn Azoth Companion /xml …`): a LogonTrigger for the
  current user only + `LogonType=InteractiveToken` +
  `RunLevel=LeastPrivilege` — exactly what the scheduler GUI allows a
  regular user to create; administrator rights are NOT required.
  `ExecutionTimeLimit=PT0S` — no standard 72 h limit, the daemon lives
  forever; `MultipleInstancesPolicy=IgnoreNew` — a repeated logon doesn't
  spawn extra instances.
- The task's XML description is left in `logs/azoth-companion-task.xml`
  (for inspection).
- Verified on a live machine without a reboot:
  `schtasks /run /tn Azoth Companion` brings up the pythonw daemon and the
  log gets written; the idempotent reinstall (`/f`) and removal work; a
  repeated `--uninstall-autostart` with no task present is not an error.

## Live test status (2026-10-04, session 2)

- ✅ Transport (the report-ID prefix), status, mask, slot, time `0x63` — the
  on-screen clock updated upon a push.
- ✅ Battery `0x64` — confirmed visually (42% in the demo).
- ✅ `0x66` is pushed and acknowledged; **"DRAM0 Usage" confirmed on screen**
  (a running triangle, the second pair of the double tile).
- ✅ The slideshow mechanics (alternating selectors within one push) work —
  but so far only on the top tile of the double indicator.
- ❌ Slot 4 as a "single tile" — not confirmed (the double layout + KPS).
- ⏳ Truly single indicators — an open question (see above).
- ⏳ Temperature — waiting for LibreHardwareMonitor + `pip install wmi`.

## Known limitations v0.2

- The host polling is fixed (2 s / 0.4 s in demo / 1 s in slideshow, a push
  on change) — not strictly event-driven like GearLink.
- Without LibreHardwareMonitor the prototype pushes only Usage (the second
  tile of the double indicator stays at "CPU0 Usage 0" — visible to the
  user).
- Slideshow: right after a slide change the first value may flash from the
  previous metric (the shift of the "type/value" pairs §10.3.2).

## Live test v0.3 (2026-10-05, GearLink killed, `ZEPHYRUS`, Python 3.14)

- ✅ The slides `cpu,ram,freq` page by timer: `DRAM0 Usage → CPU0 Freq →
  CPU0 Usage` every 2 s, the `0x66` push is acknowledged; one NAK in 40 s was
  absorbed by a quiet retry (robust_push without a wake-up), the protocol
  unchanged.
- ✅ A format refinement (2026-10-05, the second iteration): `--slides` moved
  to the GearLink "source.metric" grid (`cpu.usage,ram.usage,cpu.freq` — the
  selectors 0x00/0x30/0x02 confirmed in the log; `gpu.usage` → 0x10); the
  short first-draft names work as aliases; `gpu.usage` without a sensor is
  skipped with a single warning and the slideshow runs over the live slides;
  `--once` after the `HostSensors` refactor (a shared `_lhm_pick` + the
  gpu.*/ram.* providers) works as before.
- ✅ `--slides cpu,temp,volt,freq` without wmi/LHM: both sensor slides were
  skipped with one-time warnings (plus the regular hints from
  `HostSensors`), the slideshow ran over the live slides `cpu → freq`.
- ✅ Validation: `--slides bogus` / `--slides ","` — startup errors listing
  the allowed names; `--slideshow 0` — a clean argparse error.
- ✅ `--log-file`: `logs/azoth-companion.log` gets created, mirrors the
  stdout lines, valid UTF-8; the rotation was verified on a real
  `setup_log_file()` with a reduced limit — exactly 3 files
  (`.log`, `.1`, `.2`), no losses.
- ✅ Autostart: `schtasks /sc onlogon` without admin → "Access is denied" →
  the XML fallback created the "Azoth Companion" task (LogonTrigger
  `ZEPHYRUS\mmore`, InteractiveToken, no 72 h limit);
  `schtasks /run /tn Azoth Companion` brought up the pythonw daemon WITHOUT
  a console window, the log was written and the slides pushed; the reinstall
  is idempotent, `--uninstall-autostart` removes the task, and a repeated
  removal gives a soft "nothing to remove".
- ✅ The `--once` regression (the old `--metrics` path) — unchanged.
- ⏳ Swipe down (manual paging + the pause) and the rocker hold (the final
  value after the release) — these need hands on the device; the code of
  these paths was not touched in v0.3 (see the `0396` handler and
  `VolumeWorker`). The verification command:
  `python azoth-companion.py --slideshow 2 --log-file`.
- ⏳ The labels/units of the gpu.*/ram.* tiles on screen and the LHM GPU/RAM
  sensors themselves — to be checked on a machine with a running
  LibreHardwareMonitor (the cpu.volt units of mV were confirmed by
  capture 12).
