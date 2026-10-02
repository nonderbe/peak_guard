"""
quarter_store.py
----------------
Beheert de opslag en bevraging van historische kwartierpiek-waarden.

Slaat maximaal 30 dagen × 96 kwartieren = 2880 entries op in
HA's persistente opslag (homeassistant.helpers.storage).

Elke entry: {"ts": "2026-03-01T00:00:00+00:00", "kw": 3.141}

Tijdstempels worden in UTC opgeslagen. De maand waartoe een kwartier behoort
wordt in lokale tijd bepaald (utils.local_year_month): het capaciteitstarief
loopt per kalendermaand in Belgische tijd.
"""

from __future__ import annotations

import logging
from collections import deque
from functools import lru_cache
from datetime import datetime, timezone, timedelta
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CAPACITY_MIN_KW,
    STORAGE_KEY_QUARTERS,
    STORAGE_VERSION_QUARTERS,
    QUARTER_HISTORY_DAYS,
)
from .utils import local_year_month

_LOGGER = logging.getLogger(__name__)

# Maximale entries = 30 dagen × 96 kwartieren per dag
_MAX_ENTRIES = QUARTER_HISTORY_DAYS * 96


@lru_cache(maxsize=2 * _MAX_ENTRIES)
def _local_month_of(ts: str, time_zone: str) -> tuple[int, int]:
    """(year, month) in lokale tijd voor een ISO-tijdstempel.

    Gecachet: elke entry wordt per sensorupdate tientallen keren aan een
    maand getoetst, en een tijdstempel verandert nooit van maand. De tijdzone
    zit in de sleutel zodat een gewijzigde HA-tijdzone meteen doorwerkt.
    """
    return local_year_month(datetime.fromisoformat(ts))


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
        return self.get_month_peak(*local_year_month(dt_util.utcnow()))

    def get_monthly_peaks_last_12(self) -> list[dict]:
        """
        Lijst van de laatste 12 maandpieken (oudste eerst).

        Elke entry: {"year": int, "month": int, "ts": str, "kw": float}
        """
        current_year, current_month = local_year_month(dt_util.utcnow())
        results = []
        for delta in range(12):
            # Loop terug van de huidige maand
            month = current_month - delta
            year = current_year
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

    def get_billed_avg_kw(self) -> float:
        """
        Aangerekende piek (kW): gemiddelde van de laatste 12 maandpieken,
        waarbij elke maandpiek eerst wordt opgetrokken naar CAPACITY_MIN_KW —
        Fluvius past het minimum per maand toe, niet op het gemiddelde.
        """
        peaks = self.get_monthly_peaks_last_12()
        if not peaks:
            return CAPACITY_MIN_KW
        return round(
            sum(max(p["kw"], CAPACITY_MIN_KW) for p in peaks) / len(peaks), 4
        )

    def get_all_entries(self) -> list[dict]:
        """Alle opgeslagen entries (voor debugging/diagnostics)."""
        return list(self._entries)

    # ---------------------------------------------------------------- #
    #  Hulpfuncties                                                     #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _entry_month(entry: dict) -> tuple[int, int]:
        """Geeft (year, month) in lokale tijd voor een entry-dict."""
        try:
            return _local_month_of(entry["ts"], str(dt_util.DEFAULT_TIME_ZONE))
        except (KeyError, ValueError, TypeError):
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