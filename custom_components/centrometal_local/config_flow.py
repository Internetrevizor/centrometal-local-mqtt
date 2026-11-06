"""Home Assistant UI configuration flow."""
import voluptuous as vol
from homeassistant import config_entries

from .const import DEFAULT_HOST, DEFAULT_PORT, DOMAIN


class CentrometalLocalFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            host = str(user_input["host"]).strip()
            port = int(user_input["port"])
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"PelTec local ({host})", data={"host": host, "port": port})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("host", default=DEFAULT_HOST): str,
                vol.Required("port", default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
            }),
        )
