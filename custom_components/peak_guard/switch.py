"""
switch.py — Peak Guard
======================
Eén SwitchEntity per apparaat in de piek- en inject-cascade.

De switch geeft de gebruiker directe controle over een apparaat via de
standaard HA-UI (dashboard, more-info popup, spraakopdrachten, automations).

Gedrag:
  - Normaal apparaat (action_type = switch_off / switch_on):
      toggle schakelt de onderliggende HA-switch direct aan of uit.
  - EV Charger (action_type = ev_charger):
      toggle schakelt de ev_switch_entity aan of uit.

De Peak Guard cascade-logica heeft altijd voorrang: als de cascade een
apparaat uitschakelt, wordt de switch-state bijgewerkt zodra HA de staat
van de onderliggende entity verwerkt. Handmatige override via deze switch
wordt bij de volgende cascade-cyclus mogelijk teruggezet.

Entities worden dynamisch aangemaakt/verwijderd via
PeakGuardController.register_entity_listener().
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ACTION_EV_CHARGER
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
    """Registreer switch entities voor alle cascade-apparaten."""
    controller: PeakGuardController = hass.data[DOMAIN]["controller"]

    # Houdt bij welke device-IDs al een entity hebben om duplicaten te voorkomen
    known_ids: set[str] = set()

    def _build_switches() -> list[PeakGuardDeviceSwitch]:
        """Bouw een lijst van switches voor alle (nieuwe) cascade-apparaten."""
        new_entities = []
        all_devices = list(controller.peak_cascade) + list(controller.inject_cascade)
        seen_in_batch: set[str] = set()
        for device in all_devices:
            # Elk apparaat maar één switch, ook al staat het in beide cascades
            if device.id in known_ids or device.id in seen_in_batch:
                continue
            seen_in_batch.add(device.id)
            known_ids.add(device.id)
            new_entities.append(PeakGuardDeviceSwitch(hass, controller, device))
        return new_entities

    # Maak switches aan voor de initiële cascade-inhoud
    initial = _build_switches()
    if initial:
        async_add_entities(initial)

    # Luister naar cascade-wijzigingen (apparaten toegevoegd via de UI)
    @callback
    def _on_cascade_updated() -> None:
        new_entities = _build_switches()
        if new_entities:
            _LOGGER.debug(
                "Peak Guard switch: %d nieuwe entity/entities aangemaakt na cascade-update",
                len(new_entities),
            )
            async_add_entities(new_entities)

    controller.register_entity_listener(_on_cascade_updated)


class PeakGuardDeviceSwitch(SwitchEntity):
    """
    Directe schakelaar voor één apparaat in de Peak Guard cascade.

    Toont de actuele staat van de onderliggende HA-entiteit en laat de
    gebruiker die direct omschakelen vanuit de HA-UI.
    """

    _attr_has_entity_name = True
    _attr_should_poll     = False

    def __init__(
        self,
        hass: HomeAssistant,
        controller: PeakGuardController,
        device: CascadeDevice,
    ) -> None:
        self._hass       = hass
        self._controller = controller
        self._device     = device

        # Voor EV: gebruik ev_switch_entity als primaire entity
        self._target_entity = (
            device.ev_switch_entity or device.entity_id
            if device.action_type == ACTION_EV_CHARGER
            else device.entity_id
        )

        slug = device.id.replace("-", "_").lower()
        self._attr_unique_id   = f"{DOMAIN}_switch_{slug}"
        self._attr_name        = device.name
        self._attr_icon        = "mdi:car-electric" if device.action_type == ACTION_EV_CHARGER else "mdi:toggle-switch"
        self._attr_device_info = DEVICE_INFO_CASCADE

    # ------------------------------------------------------------------ #
    #  State                                                               #
    # ------------------------------------------------------------------ #

    @property
    def is_on(self) -> Optional[bool]:
        """True als de onderliggende entiteit aan staat."""
        state = self._hass.states.get(self._target_entity)
        if state is None:
            return None
        return state.state == "on"

    @property
    def available(self) -> bool:
        """Onbeschikbaar als de onderliggende entiteit niet bestaat of unavailable is."""
        state = self._hass.states.get(self._target_entity)
        if state is None:
            return False
        return state.state not in ("unavailable", "unknown")

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {
            "cascade_device_id":   self._device.id,
            "action_type":         self._device.action_type,
            "onderliggende_entity": self._target_entity,
        }
        if self._device.action_type == ACTION_EV_CHARGER:
            attrs["laadstroom_entity"] = self._device.ev_current_entity
        return attrs

    # ------------------------------------------------------------------ #
    #  Acties                                                              #
    # ------------------------------------------------------------------ #

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Zet het apparaat aan."""
        await self._hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": self._target_entity},
            blocking=True,
        )
        _LOGGER.info(
            "Peak Guard switch: '%s' handmatig AANgeschakeld via HA-UI",
            self._device.name,
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Zet het apparaat uit."""
        await self._hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": self._target_entity},
            blocking=True,
        )
        _LOGGER.info(
            "Peak Guard switch: '%s' handmatig UITgeschakeld via HA-UI",
            self._device.name,
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------ #
    #  HA lifecycle                                                        #
    # ------------------------------------------------------------------ #

    async def async_added_to_hass(self) -> None:
        """Luister naar state-changes van de onderliggende entity."""
        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                [self._target_entity],
                self._on_state_changed,
            )
        )

    @callback
    def _on_state_changed(self, event: Any) -> None:
        """Update de HA-state als de onderliggende entiteit verandert."""
        self.async_write_ha_state()
