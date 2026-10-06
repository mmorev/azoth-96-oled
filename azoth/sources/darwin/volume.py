"""The macOS master output volume as a provider (CoreAudio, moved 1:1; D2 —
the volume keys in the common registry, the extended interface for the
overlay). The interface is 1:1 with WindowsVolume."""
from __future__ import annotations

import ctypes                  # the class-level _AOPA(ctypes.Structure); WindowsVolume imports ctypes lazily (in _init_com)
import threading
import time
from collections.abc import Callable

from ...log import log
from ..base import Source


class MacVolume(Source):
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
        self._poller = None     # the poller thread (for the idempotent start())
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

    def keys(self) -> tuple[str, ...]:
        return ("volume.percent", "volume.muted")

    def get(self, key: str) -> int | None:
        return self.muted() if key == "volume.muted" else self.percent()

    def start(self) -> None:
        # 50 ms: the unmute is caught by polling the state, the poll quantum = the push delay
        if self._poller is not None and self._poller.is_alive():
            return
        self.start_poller(0.05)

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
        self._poller = threading.Thread(target=poll, daemon=True)
        self._poller.start()

    def stop(self) -> None:
        self._stop.set()
