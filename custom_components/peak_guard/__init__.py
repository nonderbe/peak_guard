"""Peak Guard — __init__.py"""

import logging
import os

from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_URL, PANEL_TITLE, PANEL_ICON
from .controller import PeakGuardController

_LOGGER = logging.getLogger(__name__)

_PANEL_REGISTERED_KEY = f"{DOMAIN}_panel_registered"

# Platforms die door deze integratie geladen worden
PLATFORMS = ["sensor", "button", "switch", "number"]


class PeakGuardCascadeView(HomeAssistantView):
    """REST API voor het ophalen en opslaan van de cascades."""

    url = "/api/peak_guard/cascade"
    name = "peak_guard:cascade_api"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]
        controller = hass.data.get(DOMAIN, {}).get("controller")
        if not controller:
            return self.json_message("Peak Guard niet geïnitialiseerd", status_code=503)
        return self.json(controller.to_dict())

    async def post(self, request):
        hass = request.app["hass"]
        controller = hass.data.get(DOMAIN, {}).get("controller")
        if not controller:
            return self.json_message("Peak Guard niet geïnitialiseerd", status_code=503)
        try:
            data = await request.json()
        except Exception:
            return self.json_message("Ongeldige JSON", status_code=400)

        cascade_type = data.get("type")
        devices = data.get("devices", [])

        if cascade_type not in ("peak", "inject"):
            return self.json_message(
                "Ongeldig type: gebruik 'peak' of 'inject'", status_code=400
            )

        controller.update_cascade(cascade_type, devices)
        await controller.async_save()
        return self.json({"status": "ok", "opgeslagen": len(devices)})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stel Peak Guard in vanuit een config entry."""
    controller = PeakGuardController(hass, dict(entry.data))
    await controller.async_load()

    # Sla alles op onder DOMAIN als dict zodat meerdere objecten
    # (controller + toekomstige uitbreiding) naast elkaar kunnen leven
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["controller"] = controller

    # REST API
    hass.http.register_view(PeakGuardCascadeView())

    # Frontend panel (eenmalig)
    if not hass.data.get(_PANEL_REGISTERED_KEY):
        frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
        static_url = "/peak_guard_static"
        await hass.http.async_register_static_paths([
            StaticPathConfig(static_url, frontend_path, cache_headers=False)
        ])
        await async_register_panel(
            hass,
            webcomponent_name="peak-guard-panel",
            frontend_url_path=PANEL_URL,
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            module_url=f"{static_url}/peak_guard_panel.js",
            embed_iframe=False,
            require_admin=False,
        )
        hass.data[_PANEL_REGISTERED_KEY] = True

    await controller.start_monitoring()

    # Registreer service: peak_guard.get_dashboard_yaml
    async def _handle_get_dashboard_yaml(call):
        from .dashboard_yaml import COMPACT_CARD_YAML
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Peak Guard -- Dashboard card YAML",
                "message": (
                    "Voeg toe via Dashboard > Bewerken > + Kaart > Handmatig:\n\n"
                    "```yaml\n" + COMPACT_CARD_YAML + "\n```"
                ),
                "notification_id": "peak_guard_dashboard_yaml",
            },
        )

    hass.services.async_register(
        DOMAIN, "get_dashboard_yaml", _handle_get_dashboard_yaml
    )

    # Laad sensor-platform (sensor.py)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("Peak Guard succesvol geladen")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Verwijder Peak Guard."""
    # Stop de minuut-timer van SharedCapacityState voor unload van de platforms
    shared = hass.data.get(DOMAIN, {}).get("shared")
    if shared:
        shared.stop()

    # Unload sensor- en button-platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    controller = hass.data[DOMAIN].pop("controller", None)
    if controller:
        await controller.stop_monitoring()

    hass.data.pop(_PANEL_REGISTERED_KEY, None)
    hass.services.async_remove(DOMAIN, "get_dashboard_yaml")
    frontend.async_remove_panel(hass, PANEL_URL)
    return unload_ok
