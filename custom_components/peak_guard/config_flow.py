"""config_flow.py — Peak Guard configuratiewizard."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_CONSUMPTION_SENSOR,
    CONF_PEAK_SENSOR,
    CONF_ENERGY_SENSOR,
    CONF_REGIO,
    CONF_BUFFER_WATTS,
    CONF_UPDATE_INTERVAL,
    CONF_POWER_DETECTION_TOLERANCE_PERCENT,
    CONF_SOLAR_NETTO_EUR_PER_KWH,
    CONF_DEBUG_DECISION_LOGGING,
    DEFAULT_BUFFER_WATTS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_REGIO,
    DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT,
    DEFAULT_SOLAR_NETTO_EUR_PER_KWH,
    FLUVIUS_REGIO_TARIEVEN,
)


class PeakGuardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow voor Peak Guard."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Peak Guard", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                # ---- Cascade-sensoren --------------------------------
                vol.Required(CONF_CONSUMPTION_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_PEAK_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    CONF_BUFFER_WATTS, default=DEFAULT_BUFFER_WATTS
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=2000, step=50,
                        unit_of_measurement="W", mode="slider",
                    )
                ),
                vol.Optional(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=60, step=1,
                        unit_of_measurement="s", mode="box",
                    )
                ),
                # ---- Capaciteitstarief -------------------------------
                vol.Required(CONF_ENERGY_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="energy",
                    )
                ),
                vol.Optional(
                    CONF_REGIO, default=DEFAULT_REGIO
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(FLUVIUS_REGIO_TARIEVEN.keys()),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                # ---- Vermeden piek detectie --------------------------
                vol.Optional(
                    CONF_POWER_DETECTION_TOLERANCE_PERCENT,
                    default=DEFAULT_POWER_DETECTION_TOLERANCE_PERCENT,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=50, step=5,
                        unit_of_measurement="%", mode="slider",
                    )
                ),
                # ---- Injectiepreventie besparing ---------------------
                vol.Optional(
                    CONF_SOLAR_NETTO_EUR_PER_KWH,
                    default=DEFAULT_SOLAR_NETTO_EUR_PER_KWH,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.0, max=1.0, step=0.01,
                        unit_of_measurement="€/kWh", mode="box",
                    )
                ),
                # ---- Debug logging -----------------------------------
                vol.Optional(
                    CONF_DEBUG_DECISION_LOGGING, default=False
                ): selector.BooleanSelector(),
            }),
        )

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry):
        """Geef de options flow terug (voor 'Configureren' in de HA-UI)."""
        return PeakGuardOptionsFlow(config_entry)


class PeakGuardOptionsFlow(config_entries.OptionsFlow):
    """Options flow — laat toe om instellingen na de initiële setup te wijzigen."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options.get(
            CONF_DEBUG_DECISION_LOGGING, False
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_DEBUG_DECISION_LOGGING, default=current
                ): selector.BooleanSelector(),
            }),
        )