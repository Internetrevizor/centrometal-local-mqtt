"""Home Assistant UI configuration flow. The app announces itself: there is nothing to type."""
from homeassistant import config_entries

from .const import DOMAIN


class CentrometalLocalFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._discovered = None

    async def async_step_user(self, user_input=None):
        """The address and the token exist only in the app, which sends them through the Supervisor."""
        return self.async_abort(reason="app_required")

    async def async_step_hassio(self, discovery_info):
        """The app announced itself through the Supervisor. An existing entry only takes the new details."""
        config = discovery_info.config
        self._discovered = {"host": config["host"], "port": config["port"], "token": config["token"]}
        for entry in self._async_current_entries():  # entries from before 1.2 have another unique id: look at all of them
            data = {**entry.data, **self._discovered}
            changed = data != dict(entry.data) or entry.unique_id != DOMAIN
            # Older versions put the address in the title. A title the user chose stays.
            title = "PelTec local" if entry.title.startswith("PelTec local (") else entry.title
            if changed or title != entry.title:
                self.hass.config_entries.async_update_entry(entry, data=data, unique_id=DOMAIN, title=title)
            if changed:
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(DOMAIN)
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="PelTec local", data=self._discovered)
        return self.async_show_form(step_id="hassio_confirm")
