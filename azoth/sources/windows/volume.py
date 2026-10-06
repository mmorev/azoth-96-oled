"""The Windows master output volume as a provider (moved 1:1 from the former
azoth-companion.py; the volume keys live in the common registry, D2). The
extended interface (fresh/nudge/muted/on_unmute) is used by the volume
overlay directly."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..base import Source


class WindowsVolume(Source):
    """The Windows master output volume via IAudioEndpointVolume
    (pure ctypes, no dependencies). The rocker sends consumer events —
    the OS changes the volume itself; all we have to do is READ the new
    value and mirror it onto the keyboard OSD (`51 0C`, like GearLink,
    PROTOCOL_VOLUME.md §0-§1)."""

    def __init__(self):
        self._ep = None
        self._dead = False
        self._cache = None      # the value from the background poller
        self._cache_t = 0.0
        self._muted = None      # None = not known yet
        self.on_unmute: Callable[[], None] | None = None   # the "mute cleared" callback (a level push to the OSD)
        self._stop = threading.Event()
        self._poller = None     # the poller thread (for the idempotent start())
        self._init_com()

    def keys(self) -> tuple[str, ...]:
        return ("volume.percent", "volume.muted")

    def get(self, key: str) -> int | None:
        return self.muted() if key == "volume.muted" else self.percent()

    def start(self) -> None:
        # 50 ms: the unmute is caught by polling the state, the poll quantum = the push delay
        if self._poller is not None and self._poller.is_alive():
            return
        self.start_poller(0.05)

    def _init_com(self):
        import ctypes
        from ctypes import byref, cast, c_float, c_long, c_uint32, c_void_p, POINTER

        self._ct = ctypes
        self._byref, self._cast = byref, cast
        self._c_float, self._c_void_p = c_float, c_void_p
        self._POINTER = POINTER

        class _GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        def guid(s):
            h = s.strip("{}").replace("-", "")
            return _GUID(int(h[:8], 16), int(h[8:12], 16), int(h[12:16], 16),
                         (ctypes.c_ubyte * 8)(
                             *[int(h[16 + 2 * i:18 + 2 * i], 16) for i in range(8)]))

        self._CLSID_enum = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        self._IID_enum = guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        self._IID_epvol = guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        # the COM method prototypes (the vtable indexes are in the calls below)
        self._P_EP = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_long,
                                        c_long, POINTER(c_void_p))
        self._P_ACT = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(_GUID),
                                         c_uint32, c_void_p, POINTER(c_void_p))
        self._P_F = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(c_float))
        self._P_M = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(ctypes.c_int))
        ctypes.oledll.ole32.CoInitializeEx(None, 0)
        self._GUID_T = _GUID

    def _open(self) -> bool:
        ct, byref, c_void_p = self._ct, self._byref, self._c_void_p
        try:
            enum, dev = c_void_p(), c_void_p()
            ct.oledll.ole32.CoCreateInstance(byref(self._CLSID_enum), None, 1,
                                             byref(self._IID_enum), byref(enum))
            self._call(enum, 4, self._P_EP, 0, 1, byref(dev))   # GetDefaultAudioEndpoint
            ep = c_void_p()
            self._call(dev, 3, self._P_ACT, byref(self._IID_epvol), 1, None,
                       byref(ep))                               # Activate
            self._ep = ep
            return True
        except Exception:
            return False

    def _call(self, obj, idx, proto, *args):
        """Call a COM object method by the vtable index."""
        vt = self._cast(self._cast(obj, self._POINTER(self._c_void_p)).contents,
                        self._POINTER(self._c_void_p))
        fn = self._ct.cast(vt[idx], proto)
        return fn(obj, *args)

    def percent(self) -> int | None:
        """0-100 or None (no audio device). A fresh cache from the background
        poller is returned instantly, otherwise a direct COM call (~2 ms)."""
        if self._cache is not None and time.monotonic() - self._cache_t < 0.5:
            return self._cache
        if self._dead:
            return None
        for attempt in (1, 2):
            if self._ep is None and not self._open():
                break
            v = self._c_float()
            hr = self._call(self._ep, 9, self._P_F, self._byref(v))
            if hr == 0:
                self._cache = max(0, min(100, int(round(v.value * 100))))
                self._cache_t = time.monotonic()
                return self._cache
            self._ep = None        # the device may have been recreated — reopen
        return None

    def fresh(self) -> int | None:
        """The exact value straight from the OS, bypassing the cache (for the
        "settle" push)."""
        if self._dead:
            return None
        for _ in (1, 2):
            if self._ep is None and not self._open():
                break
            v = self._c_float()
            hr = self._call(self._ep, 9, self._P_F, self._byref(v))
            if hr == 0:
                val = max(0, min(100, int(round(v.value * 100))))
                self._cache, self._cache_t = val, time.monotonic()
                return val
            self._ep = None
        return None

    def nudge(self, v: int) -> None:
        """The cache is given a predicted value (after a push on a rocker tick)."""
        self._cache = max(0, min(100, int(v)))
        self._cache_t = time.monotonic()

    def muted(self) -> bool | None:
        """The mute state (GetMute, vtable 15) or None."""
        if self._dead or self._ep is None:
            return None
        m = self._ct.c_int()
        hr = self._call(self._ep, 15, self._P_M, self._byref(m))
        return None if hr != 0 else bool(m.value)

    def start_poller(self, interval: float = 0.1) -> None:
        """Background: keep the volume "at hand" (a ~10 Hz cache) so the OSD
        push does not wait for either a COM call or the main loop; along the
        way catch the mute→unmute transition and call on_unmute (a level OSD
        after Unmute)."""
        self._cache_t = 0.0
        self._cache = None

        def poll():
            while not self._stop.is_set():
                try:
                    self.percent()
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
