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

# Sleutel om bij te houden of het panel al geregistreerd is (in hass.data)
_PANEL_REGISTERED_KEY = f"{DOMAIN}_panel_registered"

# ------------------------------------------------------------------ #
#  REST API View voor cascade data                                     #
# ------------------------------------------------------------------ #


class PeakGuardCascadeView(HomeAssistantView):
    """REST API voor het ophalen en opslaan van de cascades."""

    url = "/api/peak_guard/cascade"
    name = "peak_guard:cascade_api"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]
        controller = hass.data.get(DOMAIN)
        if not controller:
            return self.json_message(
                "Peak Guard niet geïnitialiseerd", status_code=503
            )
        return self.json(controller.to_dict())

    async def post(self, request):
        hass = request.app["hass"]
        controller = hass.data.get(DOMAIN)
        if not controller:
            return self.json_message(
                "Peak Guard niet geïnitialiseerd", status_code=503
            )
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


# ------------------------------------------------------------------ #
#  Setup                                                               #
# ------------------------------------------------------------------ #

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """YAML-setup wordt niet ondersteund – gebruik de UI config flow."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stel Peak Guard in vanuit een config entry."""
    controller = PeakGuardController(hass, dict(entry.data))
    hass.data[DOMAIN] = controller

    await controller.async_load()

    # Registreer de REST API view
    hass.http.register_view(PeakGuardCascadeView())

    # Registreer het static pad en panel slechts éénmalig per HA-instantie
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

    _LOGGER.info("Peak Guard succesvol geladen")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Verwijder Peak Guard."""
    controller = hass.data.pop(DOMAIN, None)
    if controller:
        await controller.stop_monitoring()

    # Verwijder panel-registratie vlag zodat een herlaad het panel opnieuw registreert
    hass.data.pop(_PANEL_REGISTERED_KEY, None)
    frontend.async_remove_panel(hass, PANEL_URL)
    return True