"""The banner widget (slot 0): passive — the banner/music mode/a custom
bitmap is static and lives without the daemon; the host only enables the
slot in the mask (and never turns it off at shutdown)."""
from __future__ import annotations

from .base import Widget


class BannerWidget(Widget):
    slot = 0   # SLOT_BANNER
