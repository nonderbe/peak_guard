"""
Peak Guard — deciders/base.py

Abstracte BaseDecider met gedeelde helpers:
  - _sensor_value            : float lezen uit een HA state
  - _track_action            : actie registreren voor de beslissingslog
  - _get_restore_candidates  : geef te herstellen apparaten gesorteerd op prioriteit
  - _run_cascade             : voer een cascade uit (piek of solar)
  - _apply_action            : voer één apparaat-actie uit
  - _restore_device          : herstel één apparaat naar originele staat
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    ACTION_EV_CHARGER,
    ACTION_SWITCH_OFF,
    ACTION_SWITCH_ON,
    ACTION_THROTTLE,
    CONF_DEBUG_DECISION_LOGGING,
)
from ..models import CascadeDevice, DeviceSnapshot

if TYPE_CHECKING:
    from ..avoided_peak_tracker import PeakAvoidTracker, SolarShiftTracker
    from .ev_guard import EVGuard

_LOGGER = logging.getLogger(__name__)


class BaseDecider:
    """
    Basisklasse voor Peak Guard deciders.

    Bevat alle gedeelde cascade-logica zodat PeakDecider en InjectionDecider
    alleen hun eigen check() en check_restore() methodes hoeven te implementeren.

    Parameters
    ----------
    hass              : HomeAssistant instantie
    config            : configuratie dict van de config entry
    peak_tracker      : PeakAvoidTracker voor vermeden piekvermogen events
    solar_tracker     : SolarShiftTracker voor verschoven zonne-energie events
    ev_guard          : EVGuard die alle EV-specifieke logica afhandelt
    iteration_actions : gedeelde lijst voor de beslissingslog (gereset per iteratie)
    save_fn           : coroutine-functie om de cascade-config op te slaan
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
    ) -> None:
        self.hass = hass
        self.config = config
        self.peak_tracker = peak_tracker
        self.solar_tracker = solar_tracker
        self.ev_guard = ev_guard
        self._iteration_actions = iteration_actions
        self._save_fn = save_fn
        self._last_skip_reason: str = ""

    # ------------------------------------------------------------------ #
    #  Hulpfuncties                                                        #
    # ------------------------------------------------------------------ #

    def _warn(self, msg: str, *args) -> None:
        """Log een waarschuwing én sla hem op in de GUI-buffer via EVGuard."""
        _LOGGER.warning(msg, *args)
        try:
            self.ev_guard.add_warning(msg % args if args else msg)
        except TypeError:
            self.ev_guard.add_warning(str(msg))

    def _sensor_value(self, entity_id: Optional[str]) -> Optional[float]:
        """Lees een float-waarde uit een HA sensor state. Geeft None bij onbekend/unavailable."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _track_action(
        self,
        entity_id: str,
        action: str,
        value=None,
    ) -> None:
        """
        Registreer een uitgevoerde service-call voor de beslissingslog.

        No-op als CONF_DEBUG_DECISION_LOGGING uitstaat; geen overhead in productie.
        """
        if not self.config.get(CONF_DEBUG_DECISION_LOGGING, False):
            return
        entry: dict = {"entity_id": entity_id, "action": action}
        if value is not None:
            entry["value"] = value
        self._iteration_actions.append(entry)

    # ------------------------------------------------------------------ #
    #  Herstel-kandidaten                                                  #
    # ------------------------------------------------------------------ #

    def _get_restore_candidates(
        self,
        cascade: List[CascadeDevice],
        snapshots: Dict[str, DeviceSnapshot],
        reverse: bool = True,
    ) -> List[tuple]:
        """
        Geef lijst van (device, snapshot) tuples voor apparaten die hersteld kunnen worden,
        gesorteerd op prioriteit (reverse=True → laagste prioriteit eerst).
        """
        candidates = []
        for device in cascade:
            if device.entity_id in snapshots:
                candidates.append((device, snapshots[device.entity_id]))
        candidates.sort(key=lambda x: x[0].priority, reverse=reverse)
        return candidates

    # ------------------------------------------------------------------ #
    #  Cascade uitvoering                                                  #
    # ------------------------------------------------------------------ #

    async def _run_cascade(
        self,
        cascade: List[CascadeDevice],
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
    ) -> None:
        """
        Voer een cascade uit in prioriteitsvolgorde totdat het overschot opgelost is.

        cascade_type: "peak" (piekbeperking) of "solar" (injectiepreventie).
        Slaat de cascade-config op na afloop.
        """
        sorted_devices = sorted(
            [d for d in cascade if d.enabled], key=lambda x: x.priority
        )
        label = "PIEK" if cascade_type == "peak" else "SOLAR"
        self._warn(
            "Peak Guard [%s cascade]: start — overschot=%.0f W, %d apparaat/apparaten "
            "(prioriteitsvolgorde: %s)",
            label, excess, len(sorted_devices),
            ", ".join(f"'{d.name}'[{d.action_type}]" for d in sorted_devices) or "–",
        )
        remaining = excess
        for device in sorted_devices:
            if remaining <= 0:
                self._warn(
                    "Peak Guard [%s cascade]: overschot opgelost (0 W resterend) — "
                    "verdere apparaten niet verwerkt",
                    label,
                )
                break
            before = remaining
            self._last_skip_reason = ""
            remaining = await self._apply_action(device, remaining, snapshots, cascade_type)
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
                self._warn(
                    "Peak Guard [%s cascade]:   · '%s' — geen actie: %s",
                    label, device.name, self._last_skip_reason or "zie detail-logs",
                )
        if remaining > 0:
            self._warn(
                "Peak Guard [%s cascade]: klaar — nog %.0f W overschot onverwerkt "
                "(alle apparaten doorlopen)",
                label, remaining,
            )
        else:
            self._warn(
                "Peak Guard [%s cascade]: klaar — overschot volledig verwerkt ✓",
                label,
            )
        await self._save_fn()

    async def _apply_action(
        self,
        device: CascadeDevice,
        excess: float,
        snapshots: Dict[str, DeviceSnapshot],
        cascade_type: str = "peak",
    ) -> float:
        """
        Voer de actie uit voor één apparaat in de cascade.

        Geeft het resterende overschot terug na de actie.
        """
        state = self.hass.states.get(device.entity_id)
        if state is None:
            self._warn(
                "Peak Guard: entity '%s' ('%s') niet gevonden in HA — apparaat overgeslagen",
                device.entity_id, device.name,
            )
            return excess

        # ---- Switch OFF (piekbeperking) -------------------------------- #
        if device.action_type == ACTION_SWITCH_OFF:
            if state.state == "on":
                if device.entity_id not in snapshots:
                    snapshots[device.entity_id] = DeviceSnapshot(
                        entity_id=device.entity_id,
                        original_state=state.state,
                    )
                try:
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": device.entity_id}, blocking=True
                    )
                except HomeAssistantError as err:
                    self._warn(
                        "Peak Guard: '%s' niet bereikbaar voor turn_off — "
                        "snapshot teruggedraaid, volgende cyclus opnieuw (%s)",
                        device.name, err,
                    )
                    snapshots.pop(device.entity_id, None)
                    return excess
                self._track_action(device.entity_id, "switch.turn_off")
                _LOGGER.info(
                    "Peak Guard: → '%s' UITgeschakeld "
                    "(piekbeperking, -%d W, overschot was %.0f W)",
                    device.name, device.power_watts, excess,
                )
                self.peak_tracker.record_pending_avoid(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=device.power_watts / 1000.0,
                    ts=datetime.now(timezone.utc),
                )
                return excess - device.power_watts
            else:
                _LOGGER.info(
                    "Peak Guard: → '%s' al UIT — overgeslagen "
                    "(piekbeperking, staat=%s)",
                    device.name, state.state,
                )
                return excess

        # ---- Switch ON (injectiepreventie) ----------------------------- #
        if device.action_type == ACTION_SWITCH_ON:
            if state.state == "off":
                if device.entity_id not in snapshots:
                    snapshots[device.entity_id] = DeviceSnapshot(
                        entity_id=device.entity_id,
                        original_state=state.state,
                    )
                try:
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": device.entity_id}, blocking=True
                    )
                except HomeAssistantError as err:
                    self._warn(
                        "Peak Guard: '%s' niet bereikbaar voor turn_on — "
                        "snapshot teruggedraaid, volgende cyclus opnieuw (%s)",
                        device.name, err,
                    )
                    snapshots.pop(device.entity_id, None)
                    return excess
                self._track_action(device.entity_id, "switch.turn_on")
                _LOGGER.info(
                    "Peak Guard: → '%s' AANgeschakeld "
                    "(injectiepreventie, +%d W, overschot was %.0f W)",
                    device.name, device.power_watts, excess,
                )
                self.solar_tracker.start_solar_measurement(
                    device_id=device.id,
                    device_name=device.name,
                    nominal_kw=device.power_watts / 1000.0,
                    ts=datetime.now(timezone.utc),
                )
                return excess - device.power_watts
            else:
                _LOGGER.info(
                    "Peak Guard: → '%s' al AAN — overgeslagen "
                    "(injectiepreventie, staat=%s)",
                    device.name, state.state,
                )
                return excess

        # ---- Throttle (legacy) ----------------------------------------- #
        if device.action_type == ACTION_THROTTLE:
            try:
                current = float(state.state)
                ppu = device.power_per_unit or 690.0
                new_value = max(device.min_value or 0, current - (excess / ppu))
                new_value = round(new_value, 1)
                reduction = (current - new_value) * ppu
                if new_value < current:
                    if device.entity_id not in snapshots:
                        snapshots[device.entity_id] = DeviceSnapshot(
                            entity_id=device.entity_id,
                            original_state=str(current),
                        )
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": device.entity_id, "value": new_value},
                        blocking=True,
                    )
                    self._track_action(device.entity_id, "number.set_value", new_value)
                    _LOGGER.info(
                        "Peak Guard: '%s' teruggeschroefd %.1f → %.1f (-%d W)",
                        device.name, current, new_value, reduction,
                    )
                    return excess - reduction
            except (ValueError, TypeError) as err:
                _LOGGER.error("Peak Guard throttle '%s': %s", device.name, err)

        # ---- EV Charger ------------------------------------------------ #
        if device.action_type == ACTION_EV_CHARGER:
            result = await self.ev_guard.apply_action(
                device=device,
                excess=excess,
                snapshots=snapshots,
                cascade_type=cascade_type,
                peak_tracker=self.peak_tracker,
                solar_tracker=self.solar_tracker,
            )
            # Bug fix: propageer de skip-reden van EVGuard zodat _run_cascade
            # hem correct kan loggen.
            self._last_skip_reason = self.ev_guard.last_skip_reason
            return result

        return excess

    # ------------------------------------------------------------------ #
    #  Apparaat herstel                                                    #
    # ------------------------------------------------------------------ #

    async def _restore_device(
        self,
        device: CascadeDevice,
        snapshot: DeviceSnapshot,
        cascade_type: str = "peak",
    ) -> bool:
        """
        Herstel één apparaat naar zijn originele staat na een Peak Guard ingreep.

        Geeft True terug als het herstel geslaagd is (of al klaar was).
        """
        state = self.hass.states.get(device.entity_id)
        if state is None:
            self._warn(
                "Peak Guard: kan '%s' niet herstellen — entity niet gevonden",
                device.name,
            )
            return False

        try:
            if device.action_type == ACTION_SWITCH_OFF:
                if snapshot.original_state == "on" and state.state != "on":
                    await self.hass.services.async_call(
                        "switch", "turn_on",
                        {"entity_id": device.entity_id},
                        blocking=True,
                    )
                    _LOGGER.info(
                        "Peak Guard: '%s' terug ingeschakeld na piekbeperking "
                        "(originele staat: %s)",
                        device.name, snapshot.original_state,
                    )
                    self.peak_tracker.start_measurement_on_turnon(
                        device_id=device.id,
                        device_name=device.name,
                        ts=datetime.now(timezone.utc),
                    )
                elif snapshot.original_state == "on" and state.state == "on":
                    now_ts = datetime.now(timezone.utc)
                    event = self.peak_tracker.complete_peak_calculation(
                        device_id=device.id, now=now_ts
                    )
                    if event:
                        _LOGGER.info(
                            "Peak Guard: piek-event afgerond voor '%s' — "
                            "duur=%.1f min, vermeden=%.3f kW, besparing=€%.4f",
                            device.name, event.measured_duration_min,
                            event.avoided_peak_kw, event.savings_euro,
                        )
                return True

            if device.action_type == ACTION_SWITCH_ON:
                if snapshot.original_state == "off" and state.state != "off":
                    await self.hass.services.async_call(
                        "switch", "turn_off",
                        {"entity_id": device.entity_id},
                        blocking=True,
                    )
                    _LOGGER.info(
                        "Peak Guard: '%s' terug uitgeschakeld na injectiepreventie",
                        device.name,
                    )
                    event = self.solar_tracker.complete_solar_calculation(
                        device_id=device.id,
                        now=datetime.now(timezone.utc),
                    )
                    if event:
                        _LOGGER.info(
                            "Peak Guard: solar-event afgerond voor '%s' — "
                            "duur=%.1f min, verschoven=%.4f kWh, besparing=€%.4f",
                            device.name, event.measured_duration_min,
                            event.shifted_kwh, event.savings_euro,
                        )
                return True

            if device.action_type == ACTION_THROTTLE:
                original = float(snapshot.original_state)
                current  = float(state.state)
                new_value = round(original, 1)
                if new_value != round(current, 1):
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": device.entity_id, "value": new_value},
                        blocking=True,
                    )
                    _LOGGER.info(
                        "Peak Guard: '%s' hersteld %s → %s",
                        device.name, current, new_value,
                    )
                return True

            if device.action_type == ACTION_EV_CHARGER:
                return await self.ev_guard.restore(
                    device=device,
                    snapshot=snapshot,
                    peak_tracker=self.peak_tracker,
                    solar_tracker=self.solar_tracker,
                    cascade_type=cascade_type,
                )

        except HomeAssistantError as err:
            self._warn(
                "Peak Guard: '%s' niet bereikbaar bij herstel — "
                "volgende cyclus opnieuw proberen (%s)",
                device.name, err,
            )
        except (ValueError, TypeError) as err:
            _LOGGER.error(
                "Peak Guard: fout bij herstellen '%s': %s",
                device.name, err,
            )

        return False
