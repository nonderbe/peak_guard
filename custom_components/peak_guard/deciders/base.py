"""
Peak Guard — deciders/base.py

Abstracte BaseDecider met gedeelde helpers:
  - _sensor_value            : float lezen uit een HA state
  - _track_action            : actie registreren voor de beslissingslog
  - _get_restore_candidates  : geef te herstellen apparaten gesorteerd op prioriteit
  - _run_cascade             : voer een cascade uit (piek of solar)
  - _restore_device          : herstel één apparaat naar originele staat
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    CONF_DEBUG_DECISION_LOGGING,
)
from ..models import (
    CascadeContext,
    BaseCascadeDevice,
    DeviceSnapshot,
)

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
    from .ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)


def track_action(
    config: dict,
    iteration_actions: list,
    entity_id: str,
    action: str,
    value=None,
) -> None:
    if not config.get(CONF_DEBUG_DECISION_LOGGING, False):
        return
    entry: dict = {"entity_id": entity_id, "action": action}
    if value is not None:
        entry["value"] = value
    iteration_actions.append(entry)


def read_sensor(hass: HomeAssistant, entity_id: Optional[str]) -> Optional[float]:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


class BaseDecider:
    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        peak_tracker: "PeakAvoidTracker",
        solar_tracker: "SolarShiftTracker",
        ev_guard: "EVGuard",
        iteration_actions: list,
        save_fn: Callable,
    ) -> None:
        self.hass = hass
        self.config = config
        self.peak_tracker = peak_tracker
        self.solar_tracker = solar_tracker
        self.ev_guard = ev_guard
        self._iteration_actions = iteration_actions
        self._save_fn = save_fn

    # ------------------------------------------------------------------ #
    #  Hulpfuncties                                                        #
    # ------------------------------------------------------------------ #

    def _warn(self, msg: str, *args) -> None:
        self.ev_guard._warn(msg, *args)

    def _sensor_value(self, entity_id: Optional[str]) -> Optional[float]:
        return read_sensor(self.hass, entity_id)

    def _track_action(self, entity_id: str, action: str, value=None) -> None:
        track_action(self.config, self._iteration_actions, entity_id, action, value)

    def _make_ctx(self, cascade_type: str) -> CascadeContext:
        return CascadeContext(
            hass=self.hass,
            cascade_type=cascade_type,
            peak_tracker=self.peak_tracker,
            solar_tracker=self.solar_tracker,
            ev_guard=self.ev_guard,
            track_action=self._track_action,
            warn=self._warn,
        )

    # ------------------------------------------------------------------ #
    #  Herstel-kandidaten                                                  #
    # ------------------------------------------------------------------ #

    def _get_restore_candidates(
        self,
        cascade: List[BaseCascadeDevice],
        snapshots: Dict[str, DeviceSnapshot],
        reverse: bool = True,
    ) -> List[tuple]:
        candidates = []
        for device in cascade:
            if device.entity_id in snapshots:
                if device.manual_override:
                    del snapshots[device.entity_id]
                    _LOGGER.info(
                        "Peak Guard: snapshot voor '%s' verwijderd — manuele bediening actief",
                        device.name,
                    )
                else:
                    candidates.append((device, snapshots[device.entity_id]))
        candidates.sort(key=lambda x: x[0].priority, reverse=reverse)
        return candidates

    # ------------------------------------------------------------------ #
    #  Cascade uitvoering                                                  #
    # ------------------------------------------------------------------ #

    async def _run_cascade(
        self,
        cascade: List[BaseCascadeDevice],
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
    ) -> None:
        sorted_devices = sorted(
            [d for d in cascade if d.enabled and not d.manual_override],
            key=lambda x: x.priority,
        )
        label = "PIEK" if cascade_type == "peak" else "SOLAR"
        _LOGGER.info(
            "Peak Guard [%s cascade]: start — overschot=%.0f W, %d apparaat/apparaten "
            "(prioriteitsvolgorde: %s)",
            label, excess, len(sorted_devices),
            ", ".join(f"'{d.name}'[{d.action_type}]" for d in sorted_devices) or "–",
        )

        ctx = self._make_ctx(cascade_type)
        remaining = excess

        for device in sorted_devices:
            if remaining <= 0:
                _LOGGER.info(
                    "Peak Guard [%s cascade]: overschot opgelost (0 W resterend) — "
                    "verdere apparaten niet verwerkt",
                    label,
                )
                break
            before = remaining
            ctx.last_skip_reason = ""
            remaining = await device.apply(remaining, snapshots, ctx)
            handled = before - remaining
            if handled > 0:
                _LOGGER.info(
                    "Peak Guard [%s cascade]:   ✓ '%s' — %.0f W verwerkt, resterend: %.0f W",
                    label, device.name, handled, remaining,
                )
            elif handled < 0:
                _LOGGER.info(
                    "Peak Guard [%s cascade]:   ✓ '%s' — gestart (%.0f W > surplus), "
                    "resterend: %.0f W",
                    label, device.name, abs(handled), remaining,
                )
            else:
                _LOGGER.info(
                    "Peak Guard [%s cascade]:   · '%s' — geen actie: %s",
                    label, device.name, ctx.last_skip_reason or "zie detail-logs",
                )

        if remaining > 0:
            self._warn(
                "Peak Guard [%s cascade]: klaar — nog %.0f W overschot onverwerkt "
                "(alle apparaten doorlopen)",
                label, remaining,
            )
        else:
            _LOGGER.info(
                "Peak Guard [%s cascade]: klaar — overschot volledig verwerkt ✓", label)

        await self._save_fn()

    # ------------------------------------------------------------------ #
    #  Apparaat herstel                                                    #
    # ------------------------------------------------------------------ #

    async def _restore_device(
        self,
        device: BaseCascadeDevice,
        snapshot: DeviceSnapshot,
        cascade_type: str = "peak",
    ) -> bool:
        ctx = self._make_ctx(cascade_type)
        return await device.restore(snapshot, ctx)
