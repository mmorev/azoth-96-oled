"""The unit check of the SourceRouter key resolution (task 2.1): the
priorities, the warn-once per dead key, the None fallthrough, the build-time
key validation. Run: python tests/test_router.py"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azoth.sources.base import Source
from azoth.sources.router import VALID_KEYS, SourceRouter


class Fake(Source):
    def __init__(self, keys, values=None, fail=None):
        self._keys = tuple(keys)
        self._values = values or {}
        self.fail = fail

    def keys(self):
        return self._keys

    def get(self, key):
        if self.fail:
            raise self.fail
        return self._values.get(key)


# 1. the priority: the first provider in the chain wins
hi, lo = Fake(["cpu.usage"], {"cpu.usage": 11}), Fake(["cpu.usage"], {"cpu.usage": 22})
assert SourceRouter([hi, lo]).get("cpu.usage") == 11

# 2. None falls through to the next provider
hi2, lo2 = Fake(["cpu.usage"], {}), Fake(["cpu.usage"], {"cpu.usage": 22})
r2 = SourceRouter([hi2, lo2])
assert r2.get("cpu.usage") == 22

# 3. the warn-once per dead key: claimed by nobody-serves → exactly one warning
r3 = SourceRouter([Fake(["gpu.temp"], {}), Fake(["gpu.temp"], {})])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    assert r3.get("gpu.temp") is None
    assert r3.get("gpu.temp") is None
assert buf.getvalue().count("gpu.temp") == 1, repr(buf.getvalue())

# 4. an unclaimed key → None silently (the loop logs the slide skip itself)
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    assert r3.get("ram.freq") is None
assert buf2.getvalue() == "", repr(buf2.getvalue())

# 5. the build-time key validation (fail-fast)
try:
    SourceRouter([Fake(["cpu.bogus"])])
    raise AssertionError("no validation error")
except ValueError:
    pass

# 6. provider(): the first claimant (the volume overlay's entry, D2)
assert r2.provider("cpu.usage") is hi2

# 7. a provider's SystemExit propagates (the psutil fail-fast, D-risks)
r7 = SourceRouter([Fake(["cpu.usage"], fail=SystemExit(1))])
try:
    r7.get("cpu.usage")
    raise AssertionError("no fail-fast")
except SystemExit:
    pass

# 8. the key grid: one for the CLI validation and the router
assert {"cpu.temp", "gpu.fan", "ram.usage"} <= VALID_KEYS
assert {"battery.percent", "volume.percent", "volume.muted"} <= VALID_KEYS

print("router unit checks OK")
