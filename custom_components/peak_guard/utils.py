"""Peak Guard — utils.py: shared helpers used by multiple modules."""

from __future__ import annotations

from datetime import datetime


def quarter_start(dt: datetime) -> datetime:
    """Return the start of the 15-minute quarter block containing dt (UTC)."""
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
