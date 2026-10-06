"""macmon values (macOS, Apple Silicon): the CPU/GPU temperatures, the real
P-cluster frequency, the GPU load, the fans — sudoless IOReport via
`macmon pipe`. Moved 1:1 from HostSensors._macmon/_macmon_reader/_macmon_val.
The background reader thread is a special case of the lazy start()/the first
get(); a dead subsystem shrinks keys() to () — one warning, the slides are
skipped normally."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from contextlib import suppress

from ...log import log
from ..base import Source


class MacmonSource(Source):
    def __init__(self):
        self._mm = None           # the last macmon JSON (the background reader)
        self._reader = None       # the macmon pipe reader thread (started lazily)
        self._reader_t0 = 0.0     # when the reader was spawned (the first-frame wait)
        self._dead = False        # macmon is not in PATH / does not run

    def keys(self) -> tuple[str, ...]:
        if self._dead:
            return ()
        return ("cpu.temp", "gpu.temp", "cpu.freq", "gpu.usage", "cpu.fan")

    def start(self) -> None:
        self._ensure_reader()

    def _ensure_reader(self) -> None:
        if self._reader is None:
            self._reader = threading.Thread(target=self._reader_body,
                                            daemon=True)
            self._reader_t0 = time.monotonic()
            self._reader.start()

    def _reader_body(self) -> None:
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
        self._dead = True
        if self._mm is None:
            log("macmon unavailable — the temp/freq/gpu slides on macOS will be "
                "skipped (brew install macmon)")

    def get(self, key: str) -> int | None:
        if self._dead:
            return None
        self._ensure_reader()
        if self._mm is None:
            # let the first frame arrive (~0.9 s sample): the first value read
            # waits at most 1.2 s from the reader spawn (as _macmon did)
            time.sleep(max(0.0, 1.2 - (time.monotonic() - self._reader_t0)))
        path, scale = {
            "cpu.temp":  (["temp", "cpu_temp_avg"], 1.0),
            "gpu.temp":  (["temp", "gpu_temp_avg"], 1.0),
            "cpu.freq":  (["pcpu_freq_mhz"], 1.0),
            "gpu.usage": (["gpu_active_ratio"], 100),
            "cpu.fan":   (["fans", 0, "rpm"], 1.0),
        }[key]
        return self._val(path, scale)

    def _val(self, path: list, scale: float = 1.0) -> int | None:
        """A number from the macmon JSON by the key path, or None."""
        m = self._mm
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
