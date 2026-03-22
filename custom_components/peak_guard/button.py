"""button.py -- Peak Guard
Eén button entity die de dashboard-YAML als notificatie toont.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([PeakGuardDashboardButton(entry)])


class PeakGuardDashboardButton(ButtonEntity):
    """
    Knop die een persistent_notification aanmaakt met de
    kant-en-klare card-YAML en stap-voor-stap uitleg.
    """

    _attr_has_entity_name = True
    _attr_should_poll     = False
    _attr_icon            = "mdi:view-dashboard-edit-outline"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_open_dashboard_example"
        self._attr_name      = "Toon dashboard-instructies"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "capaciteit")},
            "name": "Peak Guard Capaciteitstarief",
            "manufacturer": "Peak Guard",
        }

    async def async_press(self) -> None:
        """Stuur notificatie met card-YAML en installatiestappen."""
        from .dashboard_yaml import COMPACT_CARD_YAML  # lokale import

        message = (
            "**Voeg de Peak Guard card toe in 4 stappen:**\n\n"
            "1. Open je dashboard (bijv. Overzicht)\n"
            "2. Klik rechtsboven op het potlood-icoon **Bewerken**\n"
            "3. Klik rechtsonder op **+ Kaart toevoegen**\n"
            "4. Scroll helemaal naar beneden en klik op **Handmatig**\n"
            "5. Vervang alle tekst door de YAML hieronder en klik **Opslaan**\n\n"
            "```yaml\n"
            + COMPACT_CARD_YAML
            + "\n```\n\n"
            "_Tip: installeer optioneel `apexcharts-card` via HACS > Frontend "
            "voor rijkere grafieken._"
        )

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Peak Guard -- Dashboard card YAML",
                "message": message,
                "notification_id": "peak_guard_dashboard_yaml",
            },
        )
        _LOGGER.info("Peak Guard: dashboard-instructies verstuurd als notificatie")