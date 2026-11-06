"""Home Assistant UI configuration flow."""
import voluptuous as vol
from homeassistant import config_entries

from .const import DEFAULT_HOST, DEFAULT_PORT, DOMAIN


class CentrometalLocalFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._discovered = None

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

    async def async_step_hassio(self, discovery_info):
        """The app announced itself through the Supervisor. An existing entry only takes the new details."""
        config = discovery_info.config
        self._discovered = {"host": config["host"], "port": config["port"], "token": config["token"]}
        for entry in self._async_current_entries():  # entries from before 1.2 have another unique id: look at all of them
            data = {**entry.data, **self._discovered}
            if data != dict(entry.data) or entry.unique_id != DOMAIN:
                self.hass.config_entries.async_update_entry(entry, data=data, unique_id=DOMAIN)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(DOMAIN)
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="PelTec local", data=self._discovered)
        return self.async_show_form(step_id="hassio_confirm")
