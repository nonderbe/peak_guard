"""
Peak Guard — deciders/peak_decider.py

PeakDecider: bewaakt het kwartiervermogen en schakelt apparaten uit
als het verbruik de maandpiek (+buffer) dreigt te overschrijden.
Herstelt apparaten zodra er weer voldoende headroom is.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Dict, List

from homeassistant.core import HomeAssistant

from ..const import (
    CONF_BUFFER_WATTS,
    CONF_PEAK_SENSOR,
    DEFAULT_BUFFER_WATTS,
)
from ..models import CascadeDevice, DeviceSnapshot
from .base import BaseDecider

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
    from .ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)


class PeakDecider(BaseDecider):
    """
    Beslisser voor piekbeperking (Modus 1).

    check()         — overschrijdt het verbruik de piekgrens + buffer?
                      Zo ja, start de cascade om apparaten uit te schakelen.
    check_restore() — is er genoeg headroom om eerder uitgeschakelde
                      apparaten te herstellen?
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
        Controleer of het verbruik de maandpiek + buffer overschrijdt.
        Zo ja, start de piek-cascade.
        """
        peak = self._sensor_value(self.config.get(CONF_PEAK_SENSOR))
        if peak is None:
            _LOGGER.warning(
                "Peak Guard: piek-sensor '%s' niet beschikbaar — piekcheck overgeslagen",
                self.config.get(CONF_PEAK_SENSOR),
            )
            return
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        excess = consumption - peak + buffer
        _LOGGER.debug(
            "Peak Guard _check_peak: verbruik=%.0f W, piek=%.0f W, "
            "buffer=%.0f W, overschot=%.0f W",
            consumption, peak, buffer, excess,
        )
        if excess > 0:
            enabled_devices = [d for d in self._cascade if d.enabled]
            _LOGGER.warning(
                "Peak Guard [PIEK cascade]: gestart — piek overschreden met %.0f W "
                "(verbruik=%.0f W, piekgrens=%.0f W, buffer=%.0f W, "
                "%d apparaat/apparaten: %s)",
                excess, consumption, peak, buffer, len(enabled_devices),
                ", ".join(f"'{d.name}'" for d in enabled_devices) or "–",
            )
            if not enabled_devices:
                _LOGGER.warning(
                    "Peak Guard: geen actieve apparaten in piek-cascade — niets te doen!"
                )
            await self._run_cascade(self._cascade, excess, self._snapshots, "peak")

    async def check_restore(self, consumption: float) -> None:
        """
        Controleer of eerder uitgeschakelde apparaten veilig hersteld kunnen
        worden zonder de piekgrens opnieuw te overschrijden.
        """
        if not self._snapshots:
            return
        peak = self._sensor_value(self.config.get(CONF_PEAK_SENSOR))
        if peak is None:
            return
        buffer = float(self.config.get(CONF_BUFFER_WATTS, DEFAULT_BUFFER_WATTS))
        headroom = peak - buffer - consumption
        if headroom <= 0:
            _LOGGER.debug(
                "Peak Guard [PIEK] herstel geblokkeerd: verbruik=%.0f W, "
                "piekgrens=%.0f W, buffer=%.0f W → headroom=%.0f W (≤ 0)",
                consumption, peak, buffer, headroom,
            )
            return

        snapshots_to_restore = self._get_restore_candidates(
            self._cascade, self._snapshots, reverse=True
        )
        if not snapshots_to_restore:
            return

        # Herstel kandidaten in omgekeerde prioriteitsvolgorde
        # (laagste prioriteit eerst = laatste uitgeschakeld).
        # Reduceer de beschikbare headroom per hersteld apparaat.
        remaining_headroom = headroom
        any_restored = False
        for device, snapshot in snapshots_to_restore:
            nominal_w = float(device.power_watts) if device.power_watts else 0.0
            if nominal_w > 0 and remaining_headroom < nominal_w:
                _LOGGER.info(
                    "Peak Guard [PIEK]:   · '%s' herstel GEBLOKKEERD — "
                    "headroom %.0f W < nominaal %.0f W (te weinig marge)",
                    device.name, remaining_headroom, nominal_w,
                )
                # Als dit apparaat te groot is, zijn hogere-prioriteit apparaten
                # (meer vermogen) dat zeker ook — stop hier.
                break

            restored = await self._restore_device(device, snapshot)
            if restored:
                del self._snapshots[device.entity_id]
                remaining_headroom -= nominal_w
                any_restored = True
                _LOGGER.info(
                    "Peak Guard [PIEK]: '%s' terug AAN — headroom was %.0f W "
                    "(piek=%.0f W, buffer=%.0f W, verbruik=%.0f W)",
                    device.name, headroom, peak, buffer, consumption,
                )
        if any_restored:
            await self._save_fn()
