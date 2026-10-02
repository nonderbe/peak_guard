"""Peak Guard — utils.py: shared helpers used by multiple modules."""

from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util

from .const import CAPACITY_MIN_KW


def quarter_start(dt: datetime) -> datetime:
    """Return the start of the 15-minute quarter block containing dt (UTC)."""
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def effective_peak_w(raw_peak_w: float) -> float:
    """Return the monthly peak (W) Peak Guard should steer on.

    Below CAPACITY_MIN_KW no extra capacity tariff is due, so a lower
    P1 reading (typical early in the month) is raised to that minimum.
    """
    return max(raw_peak_w, CAPACITY_MIN_KW * 1000.0)


def local_year_month(dt: datetime) -> tuple[int, int]:
    """Return (year, month) of dt in Home Assistant's configured time zone.

    The capacity tariff is billed per calendar month in local (Belgian) time,
    and the P1 meter resets its monthly peak at local midnight. Timestamps
    stay stored in UTC; only the month they are attributed to is local.
    """
    local = dt_util.as_local(dt)
    return (local.year, local.month)
