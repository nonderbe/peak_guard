"""
quarter_store.py
----------------
Beheert de opslag en bevraging van historische kwartierpiek-waarden.

Slaat maximaal 30 dagen × 96 kwartieren = 2880 entries op in
HA's persistente opslag (homeassistant.helpers.storage).

Elke entry: {"ts": "2026-03-01T00:00:00+00:00", "kw": 3.141}
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    STORAGE_KEY_QUARTERS,
    STORAGE_VERSION_QUARTERS,
    QUARTER_HISTORY_DAYS,
)

_LOGGER = logging.getLogger(__name__)

# Maximale entries = 30 dagen × 96 kwartieren per dag
_MAX_ENTRIES = QUARTER_HISTORY_DAYS * 96


class QuarterStore:
    """Persistente opslag voor 15-minuten kwartierpiek-waarden."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION_QUARTERS, STORAGE_KEY_QUARTERS)
        # deque met max. _MAX_ENTRIES items: (timestamp_str, kw_float)
        self._entries: deque[dict] = deque(maxlen=_MAX_ENTRIES)

    # ---------------------------------------------------------------- #
    #  Laden en opslaan                                                 #
    # ---------------------------------------------------------------- #

    async def async_load(self) -> None:
        """Laad opgeslagen kwartierpiek-waarden vanuit HA-opslag."""
        data = await self._store.async_load()
        if data and isinstance(data.get("quarters"), list):
            cutoff = datetime.now(timezone.utc) - timedelta(days=QUARTER_HISTORY_DAYS)
            for entry in data["quarters"]:
                try:
                    ts = datetime.fromisoformat(entry["ts"])
                    if ts >= cutoff:
                        self._entries.append({"ts": entry["ts"], "kw": float(entry["kw"])})
                except (KeyError, ValueError):
                    pass
            _LOGGER.debug("QuarterStore: %d entries geladen", len(self._entries))

    async def async_save(self) -> None:
        """Bewaar kwartierpiek-waarden naar HA-opslag."""
        await self._store.async_save({"quarters": list(self._entries)})

    # ---------------------------------------------------------------- #
    #  Schrijven                                                        #
    # ---------------------------------------------------------------- #

    async def add_quarter(self, ts: datetime, kw: float) -> None:
        """
        Voeg een afgesloten kwartierpiek toe en sla op.

        Parameters
        ----------
        ts  : datetime  Start-tijdstip van het kwartier (UTC).
        kw  : float     Gemiddeld vermogen gedurende het kwartier (kW).
        """
        self._entries.append({
            "ts": ts.isoformat(),
            "kw": round(kw, 4),
        })
        await self.async_save()

    # ---------------------------------------------------------------- #
    #  Bevragen                                                         #
    # ---------------------------------------------------------------- #

    def get_month_peak(self, year: int, month: int) -> Optional[float]:
        """Hoogste kwartierpiek-waarde voor de gegeven maand (kW), of None."""
        values = [
            e["kw"]
            for e in self._entries
            if self._entry_month(e) == (year, month)
        ]
        return max(values) if values else None

    def get_current_month_peak(self) -> Optional[float]:
        """Hoogste kwartierpiek-waarde voor de huidige maand (kW), of None."""
        now = datetime.now(timezone.utc)
        return self.get_month_peak(now.year, now.month)

    def get_monthly_peaks_last_12(self) -> list[dict]:
        """
        Lijst van de laatste 12 maandpieken (oudste eerst).

        Elke entry: {"year": int, "month": int, "ts": str, "kw": float}
        """
        now = datetime.now(timezone.utc)
        results = []
        for delta in range(12):
            # Loop terug van de huidige maand
            month = now.month - delta
            year = now.year
            while month <= 0:
                month += 12
                year -= 1
            peak_kw = self.get_month_peak(year, month)
            if peak_kw is not None:
                # Zoek de tijdstempel van het piekmoment
                peak_ts = self._peak_ts_for_month(year, month)
                results.append({
                    "year": year,
                    "month": month,
                    "ts": peak_ts,
                    "kw": peak_kw,
                })
        results.reverse()   # Oudste eerst
        return results

    def get_rolling_12_month_avg(self) -> Optional[float]:
        """Gemiddelde van de laatste 12 maandpieken (kW), of None."""
        peaks = self.get_monthly_peaks_last_12()
        if not peaks:
            return None
        return round(sum(p["kw"] for p in peaks) / len(peaks), 4)

    def get_all_entries(self) -> list[dict]:
        """Alle opgeslagen entries (voor debugging/diagnostics)."""
        return list(self._entries)

    # ---------------------------------------------------------------- #
    #  Hulpfuncties                                                     #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _entry_month(entry: dict) -> tuple[int, int]:
        """Geeft (year, month) voor een entry-dict."""
        try:
            dt = datetime.fromisoformat(entry["ts"])
            return (dt.year, dt.month)
        except (KeyError, ValueError):
            return (0, 0)

    def _peak_ts_for_month(self, year: int, month: int) -> Optional[str]:
        """Tijdstempel van de hoogste entry voor de gegeven maand."""
        month_entries = [
            e for e in self._entries
            if self._entry_month(e) == (year, month)
        ]
        if not month_entries:
            return None
        return max(month_entries, key=lambda e: e["kw"])["ts"]