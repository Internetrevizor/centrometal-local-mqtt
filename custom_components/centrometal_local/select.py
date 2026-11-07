"""The DHW / heating mode of the boiler (controller manual 5.1), written to slot 4 and read back from it."""
from homeassistant.components.select import SelectEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .parameters import DHW_HEATING_MODES, DHW_MODE_ENTITY_ID, DHW_MODE_SLOT, commands_available, setting_value
from .sensor import device_info
from .switch import _deliver


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DhwHeatingMode(entry.runtime_data, entry.entry_id)])


class DhwHeatingMode(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "DHW / Heating Mode Setting"
    _attr_icon = "mdi:water-boiler"
    _attr_options = list(DHW_HEATING_MODES.values())

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_setting_{DHW_MODE_SLOT}"
        self._attr_device_info = device_info(entry_id)
        self.entity_id = DHW_MODE_ENTITY_ID

    @property
    def current_option(self):
        value = setting_value((self.coordinator.data or {}).get("values"), DHW_MODE_SLOT, "B_PTV/GRI_SEL")
        return DHW_HEATING_MODES.get(int(value)) if value is not None and value == int(value) else None

    @property
    def available(self):
        return self.coordinator.last_update_success and commands_available(self.coordinator.data)

    @property
    def extra_state_attributes(self):
        return {"Setting index": DHW_MODE_SLOT}

    async def async_select_option(self, option):
        value = next(code for code, name in DHW_HEATING_MODES.items() if name == option)
        await _deliver(self.coordinator, lambda: self.coordinator.async_send_setting(DHW_MODE_SLOT, value), "DHW / heating mode")
