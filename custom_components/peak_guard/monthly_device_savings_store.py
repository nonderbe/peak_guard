"""
monthly_device_savings_store.py
--------------------------------
Persistente, per-apparaat maandhistoriek van piekbeperkings-besparingen.

Slaat voor elke (jaar, maand, device_id)-combinatie de hoogste hypothetische
maandpiek op die dat apparaat heeft vermeden, samen met de werkelijke
maandpiek op dat moment en de resulterende kW- en EUR-besparing
(zie PeakAvoidTracker.get_device_monthly_savings voor de berekening).

Elke entry wordt continu bijgewerkt zolang de maand loopt ("finalized": False)
en definitief bevroren zodra de maand afsluit ("finalized": True) — zie
SharedCapacityState._async_update in sensor.py. Een maandwissel die gemist
wordt omdat Home Assistant uit stond, wordt bij de volgende start alsnog
afgesloten via async_finalize_before(). Zo overleeft de attributie
per apparaat de maandwissel, in tegenstelling tot de events-log en
hypothetical_peaks_this_month, die bij reset_month() verloren gaan.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY_DEVICE_SAVINGS, STORAGE_VERSION_DEVICE_SAVINGS

_LOGGER = logging.getLogger(__name__)


class MonthlyDeviceSavingsStore:
    """Persistente opslag voor maandelijkse piekbesparing per apparaat."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION_DEVICE_SAVINGS, STORAGE_KEY_DEVICE_SAVINGS)
        self._entries: list[dict] = []

    # ---------------------------------------------------------------- #
    #  Laden en opslaan                                                 #
    # ---------------------------------------------------------------- #

    async def async_load(self) -> None:
        """Laad opgeslagen maandrecords vanuit HA-opslag."""
        data = await self._store.async_load()
        if data and isinstance(data.get("entries"), list):
            self._entries = data["entries"]
            _LOGGER.debug("MonthlyDeviceSavingsStore: %d entries geladen", len(self._entries))

    async def async_save(self) -> None:
        """Bewaar alle maandrecords naar HA-opslag."""
        await self._store.async_save({"entries": self._entries})

    # ---------------------------------------------------------------- #
    #  Schrijven                                                        #
    # ---------------------------------------------------------------- #

    async def async_upsert(
        self,
        year: int,
        month: int,
        device_id: str,
        device_name: str,
        hypothetical_peak_kw: float,
        actual_monthly_peak_kw: float,
        avoided_kw: float,
        savings_euro: float,
        finalized: bool,
    ) -> None:
        """
        Werk de record voor (year, month, device_id) bij, of maak ze aan.

        Wordt elke maand meermaals aangeroepen met finalized=False zolang de
        maand loopt, en één laatste keer met finalized=True vlak vóór de
        tracker gereset wordt bij de maandwissel.
        """
        for e in self._entries:
            if e["year"] == year and e["month"] == month and e["device_id"] == device_id:
                e.update({
                    "device_name": device_name,
                    "hypothetical_peak_kw": round(hypothetical_peak_kw, 4),
                    "actual_monthly_peak_kw": round(actual_monthly_peak_kw, 4),
                    "avoided_kw": round(avoided_kw, 4),
                    "savings_euro": round(savings_euro, 4),
                    "finalized": finalized,
                })
                break
        else:
            self._entries.append({
                "year": year,
                "month": month,
                "device_id": device_id,
                "device_name": device_name,
                "hypothetical_peak_kw": round(hypothetical_peak_kw, 4),
                "actual_monthly_peak_kw": round(actual_monthly_peak_kw, 4),
                "avoided_kw": round(avoided_kw, 4),
                "savings_euro": round(savings_euro, 4),
                "finalized": finalized,
            })
        await self.async_save()

    async def async_finalize_before(self, year: int, month: int) -> int:
        """
        Bevries alle nog open records van maanden vóór (year, month).

        Vangt de maandwissel op die gemist wordt als Home Assistant uit staat
        op het moment van de wissel: de record van die maand blijft dan op
        finalized=False staan. De opgeslagen waarden zijn de laatst bekende
        stand van die maand (ze worden bij elke wijziging weggeschreven), dus
        ze worden ongewijzigd bevroren. Geeft het aantal bevroren records terug.
        """
        closed = 0
        for e in self._entries:
            entry_year, entry_month = e.get("year"), e.get("month")
            # Sla beschadigde records over: dit draait bij het opstarten en
            # mag de sensor-setup nooit afbreken.
            if not isinstance(entry_year, int) or not isinstance(entry_month, int):
                continue
            if (entry_year, entry_month) < (year, month) and not e.get("finalized"):
                e["finalized"] = True
                closed += 1
        if closed:
            await self.async_save()
        return closed

    # ---------------------------------------------------------------- #
    #  Bevragen                                                         #
    # ---------------------------------------------------------------- #

    def get_device_history(self, device_id: str) -> list[dict]:
        """Alle maandrecords voor een specifiek apparaat, oudste eerst."""
        return sorted(
            (e for e in self._entries if e["device_id"] == device_id),
            key=lambda e: (e["year"], e["month"]),
        )

    def get_month(self, year: int, month: int) -> list[dict]:
        """Alle apparaat-records voor een specifieke (jaar, maand)-combinatie."""
        return [e for e in self._entries if e["year"] == year and e["month"] == month]

    def get_all_entries(self) -> list[dict]:
        """Alle opgeslagen records (voor debugging/diagnostics)."""
        return list(self._entries)
