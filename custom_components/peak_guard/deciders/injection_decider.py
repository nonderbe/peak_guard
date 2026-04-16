"""
Peak Guard — deciders/injection_decider.py

InjectionDecider: bewaakt het solar-overschot en schakelt extra verbruikers
in om teruglevering aan het net te minimaliseren (Modus 2).
Herstelt apparaten zodra het overschot verdwenen is.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Dict, List

from homeassistant.core import HomeAssistant

from ..const import (
    CONF_BUFFER_WATTS,
    DEFAULT_BUFFER_WATTS,
)
from ..models import CascadeDevice, DeviceSnapshot
from .base import BaseDecider

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
    from .ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)


class InjectionDecider(BaseDecider):
    """
    Beslisser voor injectiepreventie (Modus 2).

    check()         — is het solar-overschot groter dan de buffer?
                      Zo ja, start de inject-cascade om verbruikers in te schakelen.
    check_restore() — is het overschot verdwenen? Herstel dan de verbruikers.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        ev_guard: "EVGuard",
        iteration_actions: list,
        save_fn: Callable,
        cascade: List[CascadeDevice],
        snapshots: Dict[str, DeviceSnapshot],
    ) -> None:
        super().__init__(
            hass=hass,
            config=config,
            peak_tracker=peak_tracker,
            solar_tracker=solar_tracker,
            ev_guard=ev_guard,
            iteration_actions=iteration_actions,
            save_fn=save_fn,
        )
        self._cascade = cascade
        self._snapshots = snapshots

    # ------------------------------------------------------------------ #
    #  Publieke interface                                                  #
    # ------------------------------------------------------------------ #

    async def check(self, consumption: float) -> None:
        """
        Controleer of het netto-verbruik negatief is (injectie op het net).
        Zo ja, start de inject-cascade om het overschot lokaal te verbruiken.

        consumption: actueel netto-verbruik in W (negatief = injectie)
        """
        injection = abs(consumption)
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        _LOGGER.debug(
            "Peak Guard _check_injection: injectie=%.0f W, buffer=%.0f W, "
            "actief=%d snapshot(s)",
            injection, buffer, len(self._snapshots),
        )
        if injection > buffer:
            enabled_devices = [d for d in self._cascade if d.enabled]
            _LOGGER.warning(
                "Peak Guard [SOLAR cascade]: gestart — overschot = %.0f W "
                "(buffer=%.0f W, %d apparaat/apparaten: %s)",
                injection, buffer, len(enabled_devices),
                ", ".join(f"'{d.name}'" for d in enabled_devices) or "–",
            )
            if not enabled_devices:
                _LOGGER.warning(
                    "Peak Guard: geen actieve apparaten in inject-cascade — "
                    "%.0f W wordt teruggeleverd aan het net zonder actie!",
                    injection,
                )
            await self._run_cascade(self._cascade, injection, self._snapshots, "solar")
        else:
            _LOGGER.debug(
                "Peak Guard: injectie %.0f W ≤ buffer %.0f W — geen actie vereist",
                injection, buffer,
            )

    async def check_restore(self, consumption: float) -> None:
        """
        Controleer of eerder ingeschakelde verbruikers teruggezet kunnen
        worden (overschot verdwenen — netto-verbruik ≥ 0).
        """
        if not self._snapshots:
            return
        _LOGGER.debug(
            "Peak Guard _check_inject_restore: verbruik=%.0f W, %d snapshot(s) actief",
            consumption, len(self._snapshots),
        )
        if consumption < 0:
            _LOGGER.debug(
                "Peak Guard: inject-herstel geblokkeerd — verbruik nog negatief (%.0f W)",
                consumption,
            )
            return
        snapshots_to_restore = self._get_restore_candidates(
            self._cascade, self._snapshots, reverse=True
        )
        if not snapshots_to_restore:
            return
        # Herstel één apparaat per cyclus (laagste prioriteit eerst)
        device, snapshot = snapshots_to_restore[0]
        restored = await self._restore_device(device, snapshot)
        if restored:
            del self._snapshots[device.entity_id]
            _LOGGER.info("Peak Guard: '%s' hersteld", device.name)
            await self._save_fn()
