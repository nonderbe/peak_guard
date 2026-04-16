"""
Peak Guard — deciders/ev_guard.py

Beheert de volledige EV-lader state machine, rate-limiting en debounce-logica.

Publieke API (aangeroepen vanuit BaseDecider):
  apply_action(device, excess, snapshots, cascade_type, peak_tracker, solar_tracker) -> float
  restore(device, snapshot, peak_tracker, solar_tracker) -> bool

Properties (aangeroepen vanuit controller voor to_dict):
  guards      -> Dict[str, EVDeviceGuard]
  rate_limiter -> EVRateLimiter
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    CONF_DEBUG_DECISION_LOGGING,
    DEFAULT_EV_CABLE_ENTITY,
    DEFAULT_EV_MAX_AMPERE,
    DEFAULT_EV_MIN_AMPERE,
    DEFAULT_EV_SOLAR_STOP_THRESHOLD_W,
)
from ..models import (
    CascadeDevice,
    DeviceSnapshot,
    EVDeviceGuard,
    EVRateLimiter,
    EVState,
    EV_DEBOUNCE_BYPASS_SURPLUS_W,
    EV_DEBOUNCE_STABLE_S,
    EV_DEBOUNCE_TOLERANCE_W,
    EV_HYSTERESIS_AMPS,
    EV_MIN_OFF_DURATION_S,
    EV_MIN_ON_DURATION_S,
    EV_MIN_UPDATE_INTERVAL_S,
    EV_RATE_LIMIT_MAX_CALLS,
    EV_RATE_LIMIT_WINDOW_S,
    EV_VOLTS_1PHASE,
    EV_VOLTS_3PHASE,
)

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker

_LOGGER = logging.getLogger(__name__)


class EVGuard:
    """
    Beheert alle per-apparaat EV-lader state, rate-limiting en debounce.

    Eén instantie gedeeld door PeakDecider en InjectionDecider.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        iteration_actions: list,
    ) -> None:
        self.hass = hass
        self.config = config
        self._iteration_actions = iteration_actions   # gedeelde mutable lijst
        self._guards: Dict[str, EVDeviceGuard] = {}
        self._rate_limiter = EVRateLimiter()
        # Wordt gezet door apply_action; BaseDecider leest dit na de aanroep.
        self.last_skip_reason: str = ""

    # ------------------------------------------------------------------ #
    #  Properties voor controller.to_dict()                               #
    # ------------------------------------------------------------------ #

    @property
    def guards(self) -> Dict[str, EVDeviceGuard]:
        return self._guards

    @property
    def rate_limiter(self) -> EVRateLimiter:
        return self._rate_limiter

    # ------------------------------------------------------------------ #
    #  Interne helpers                                                     #
    # ------------------------------------------------------------------ #

    def get_guard(self, device_id: str) -> EVDeviceGuard:
        """Geef de EVDeviceGuard voor device_id; maak aan als nog niet bestaat."""
        if device_id not in self._guards:
            self._guards[device_id] = EVDeviceGuard()
        return self._guards[device_id]

    def _rate_check(self, device_name: str, reason: str) -> bool:
        """
        Geeft True als een EV service call is toegestaan.
        Logt een warning en geeft False als de rate-limiter vol is.
        """
        if self._rate_limiter.is_allowed():
            return True
        _LOGGER.warning(
            "Peak Guard EV '%s': service call OVERGESLAGEN wegens globale rate-limiter "
            "(%d/%d calls in %.0f s). Reden: %s",
            device_name,
            self._rate_limiter.calls_in_window,
            EV_RATE_LIMIT_MAX_CALLS,
            EV_RATE_LIMIT_WINDOW_S,
            reason,
        )
        return False

    def _record_call(self) -> None:
        """Registreer dat we zojuist een EV service call hebben gemaakt."""
        self._rate_limiter.record()

    def _track_action(self, entity_id: str, action: str, value=None) -> None:
        """Registreer een service-call in de gedeelde iteratie-actielijst."""
        if not self.config.get(CONF_DEBUG_DECISION_LOGGING, False):
            return
        entry: dict = {"entity_id": entity_id, "action": action}
        if value is not None:
            entry["value"] = value
        self._iteration_actions.append(entry)
