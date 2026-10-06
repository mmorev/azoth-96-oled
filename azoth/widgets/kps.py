"""The KPS widget (slot 4): passive — the native keys/s tile is drawn by the
firmware itself, the host only enables the slot (formerly the "carousel" —
the "single tile" hypothesis was not confirmed, see README.md)."""
from __future__ import annotations

from ..constants import SLOT_KPS
from .base import Widget


class KpsWidget(Widget):
    slot = SLOT_KPS
