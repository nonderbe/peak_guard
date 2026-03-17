import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_CONSUMPTION_SENSOR,
    CONF_PEAK_SENSOR,
    CONF_BUFFER_WATTS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_BUFFER_WATTS,
    DEFAULT_UPDATE_INTERVAL,
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
            data_schema=vol.Schema(
                {
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
                            min=0,
                            max=2000,
                            step=50,
                            unit_of_measurement="W",
                            mode="slider",
                        )
                    ),
                    vol.Optional(
                        CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=60,
                            step=1,
                            unit_of_measurement="s",
                            mode="box",
                        )
                    ),
                }
            ),
        )

