"""The provider contract (D2): a source of values by keys.

keys()      — the keys the provider can currently serve (dynamic: a dead
              subsystem shrinks its list, e.g. macmon not in PATH → ());
get(key)    — an int in the 0x66 tile units or None (None = the provider
              does not serve the key right now; None is the protocol
              language: the slide is skipped with a single warning);
start()/stop() — lazy and idempotent resource hooks (the macmon reader
              thread and the volume poller are their special cases;
              psutil — a no-op).
"""
from __future__ import annotations

from collections.abc import Callable


class Source:
    """The base of the value providers (the contract is in the module docstring).

    on_unmute — the optional extended-interface hook of the volume providers
    (the "mute cleared" callback, D2); the value sources do not use it."""

    on_unmute: Callable[[], None] | None = None

    def keys(self) -> tuple[str, ...]:
        raise NotImplementedError

    def get(self, key: str) -> int | None:
        raise NotImplementedError

    def start(self) -> None:
        pass              # lazy by default: a provider without resources

    def stop(self) -> None:
        pass
