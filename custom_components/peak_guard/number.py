"""
number.py — Peak Guard
======================
NumberEntity voor het instellen van de laadstroom (A) van een EV Charger.

Per EV-charger in de cascade wordt één NumberEntity aangemaakt.
De gebruiker kan hiermee de laadstroom rechtstreeks instellen vanuit de
HA-UI (dashboard, more-info popup, automations, spraakopdrachten).

Bereik: min_value (of DEFAULT_EV_MIN_AMPERE) t/m max_value (of DEFAULT_EV_MAX_AMPERE).
Eenheid: A (ampère).

De entity leest de huidige waarde live uit de ev_current_entity van het apparaat.
Bij instellen wordt number.set_value aangeroepen op diezelfde entity.

Entities worden dynamisch aangemaakt/verwijderd via
PeakGuardController.register_entity_listener().
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ACTION_EV_CHARGER, DEFAULT_EV_MIN_AMPERE, DEFAULT_EV_MAX_AMPERE
from .controller import CascadeDevice, PeakGuardController

_LOGGER = logging.getLogger(__name__)

DEVICE_INFO_CASCADE = {
    "identifiers": {(DOMAIN, "cascade_devices")},
    "name": "Peak Guard — Cascade-apparaten",
    "manufacturer": "Peak Guard",
    "model": "Cascade-module",
    "entry_type": "service",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Registreer number entities voor alle EV-chargers in de cascade."""
    controller: PeakGuardController = hass.data[DOMAIN]["controller"]

    known_ids: set[str] = set()

    def _build_numbers() -> list[PeakGuardEVCurrentNumber]:
        new_entities = []
        all_devices = list(controller.peak_cascade) + list(controller.inject_cascade)
        seen_in_batch: set[str] = set()
        for device in all_devices:
            if device.action_type != ACTION_EV_CHARGER:
                continue
            if not device.ev_current_entity:
                continue  # geen stroomsensor → kan stroom niet instellen
            if device.id in known_ids or device.id in seen_in_batch:
                continue
            seen_in_batch.add(device.id)
            known_ids.add(device.id)
            new_entities.append(PeakGuardEVCurrentNumber(hass, controller, device))
        return new_entities

    initial = _build_numbers()
    if initial:
        async_add_entities(initial)

    @callback
    def _on_cascade_updated() -> None:
        new_entities = _build_numbers()
        if new_entities:
            _LOGGER.debug(
                "Peak Guard number: %d nieuwe entity/entities aangemaakt na cascade-update",
                len(new_entities),
            )
            async_add_entities(new_entities)

    controller.register_entity_listener(_on_cascade_updated)


class PeakGuardEVCurrentNumber(NumberEntity):
    """
    Instelbare laadstroom (A) voor één EV Charger.

    Leest de actuele waarde live uit de ev_current_entity.
    Schrijven stuurt number.set_value naar diezelfde entity.
    """

    _attr_has_entity_name = True
    _attr_should_poll     = False
    _attr_icon            = "mdi:current-ac"
    _attr_native_unit_of_measurement = "A"
    _attr_mode            = NumberMode.SLIDER

    def __init__(
        self,
        hass: HomeAssistant,
        controller: PeakGuardController,
        device: CascadeDevice,
    ) -> None:
        self._hass          = hass
        self._controller    = controller
        self._device        = device
        self._current_entity = device.ev_current_entity  # type: ignore[assignment]

        slug = device.id.replace("-", "_").lower()
        self._attr_unique_id = f"{DOMAIN}_ev_current_{slug}"
        self._attr_name      = f"{device.name} — laadstroom"
        self._attr_device_info = DEVICE_INFO_CASCADE

        # Bereik uit device-configuratie
        self._attr_native_min_value = float(
            device.ev_min_current if device.ev_min_current is not None
            else (device.min_value if device.min_value is not None else DEFAULT_EV_MIN_AMPERE)
        )
        self._attr_native_max_value = float(
            device.max_value if device.max_value is not None else DEFAULT_EV_MAX_AMPERE
        )
        self._attr_native_step = 1.0

    # ------------------------------------------------------------------ #
    #  State                                                               #
    # ------------------------------------------------------------------ #

    @property
    def native_value(self) -> Optional[float]:
        """Actuele laadstroom uit de ev_current_entity."""
        state = self._hass.states.get(self._current_entity)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:
        state = self._hass.states.get(self._current_entity)
        if state is None:
            return False
        return state.state not in ("unavailable", "unknown")

    @property
    def extra_state_attributes(self) -> dict:
        phases = self._device.ev_phases or 1
        voltage = 400.0 if phases == 3 else 230.0
        current_a = self.native_value
        return {
            "cascade_device_id":    self._device.id,
            "ev_current_entity":    self._current_entity,
            "ev_switch_entity":     self._device.ev_switch_entity or self._device.entity_id,
            "fasen":                phases,
            "spanning_v":           voltage,
            "huidig_vermogen_w":    round(current_a * voltage, 0) if current_a else None,
            "hardware_min_a":       self._attr_native_min_value,
        }

    # ------------------------------------------------------------------ #
    #  Actie                                                               #
    # ------------------------------------------------------------------ #

    async def async_set_native_value(self, value: float) -> None:
        """Stel de laadstroom in via de ev_current_entity."""
        # Afronden naar hele ampères (Tesla accepteert geen decimalen)
        rounded = round(value)
        rounded = max(int(self._attr_native_min_value),
                      min(int(self._attr_native_max_value), rounded))

        await self._hass.services.async_call(
            "number", "set_value",
            {"entity_id": self._current_entity, "value": float(rounded)},
            blocking=True,
        )
        _LOGGER.info(
            "Peak Guard number: '%s' laadstroom handmatig ingesteld op %d A via HA-UI",
            self._device.name, rounded,
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------ #
    #  HA lifecycle                                                        #
    # ------------------------------------------------------------------ #

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._hass.bus.async_listen(
                "state_changed",
                self._on_state_changed,
            )
        )

    @callback
    def _on_state_changed(self, event: Any) -> None:
        if event.data.get("entity_id") == self._current_entity:
            self.async_write_ha_state()
