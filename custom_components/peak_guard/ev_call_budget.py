"""Peak Guard — ev_call_budget.py

Persistente, dagelijkse harde bovengrens op het aantal echte EV-API
service-calls (bv. Tesla Fleet API).

Dit is het lange-horizon vangnet bovenop EVRateLimiter (12 calls / 10 min,
zie models.py). Die sliding window beschermt tegen thrashing/bursts binnen
één cyclus, maar kan een structureel falend apparaat niet tegenhouden dat
uur na uur tegen zijn plafond aan blijft botsen (zie v1.8.12).

Het budget is bewust DAGELIJKS, niet maandelijks: EV_API_DAILY_BUDGET is
het maandquotum gedeeld door 30 (zie const.py). Een storing op één dag put
dan hooguit het budget van die dag uit — de volgende dag reset de teller
volledig, dus normaal gebruik voor de rest van de maand blijft gewoon
werken. Een cumulatief maandbudget zou een storing vroeg in de maand
laten doorwerken op elke dag erna, ook nadat de storing zelf al lang
verholpen is.

Overleeft herstarts van Home Assistant via de HA Store API, in tegenstelling
tot EVRateLimiter die bewust in-memory blijft (kortlopend venster, reset bij
herstart is onschadelijk).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    EV_API_DAILY_BUDGET,
    STORAGE_KEY_EV_CALL_BUDGET,
    STORAGE_VERSION_EV_CALL_BUDGET,
)

_LOGGER = logging.getLogger(__name__)


def _current_day_key(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


class EVDailyCallBudget:
    """Telt EV-API-calls binnen de huidige kalenderdag (UTC), persistent."""

    def __init__(self, hass: HomeAssistant, max_calls: int = EV_API_DAILY_BUDGET) -> None:
        self._store = Store(hass, STORAGE_VERSION_EV_CALL_BUDGET, STORAGE_KEY_EV_CALL_BUDGET)
        self._max_calls = max_calls
        self._day = _current_day_key()
        self._count = 0

    async def async_load(self) -> None:
        """Laad de opgeslagen teller. Een andere dag dan vandaag → start op 0."""
        data = await self._store.async_load()
        if data and data.get("day") == _current_day_key():
            self._day = data["day"]
            self._count = int(data.get("count", 0))
        _LOGGER.debug(
            "EVDailyCallBudget: %d/%d calls geladen voor dag %s",
            self._count, self._max_calls, self._day,
        )

    def _roll_over_if_needed(self) -> None:
        current = _current_day_key()
        if current != self._day:
            self._day = current
            self._count = 0

    def is_allowed(self) -> bool:
        self._roll_over_if_needed()
        return self._count < self._max_calls

    def record(self) -> None:
        """Registreer één echte API-call. Nooit aanroepen vóór de call zelf slaagt/faalt."""
        self._roll_over_if_needed()
        self._count += 1

    async def async_save(self) -> None:
        try:
            await self._store.async_save({"day": self._day, "count": self._count})
        except Exception as exc:
            _LOGGER.debug("EVDailyCallBudget: opslaan mislukt: %s", exc)

    @property
    def calls_today(self) -> int:
        self._roll_over_if_needed()
        return self._count

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def remaining(self) -> int:
        return max(0, self._max_calls - self.calls_today)
