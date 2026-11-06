"""The writable PelTec settings. Limits come from the controller; what is shown is read back from it."""
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .parameters import LIMITS, NUMBER_PREFIX, SETTINGS, number
from .sensor import device_info


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PelTecSetting(entry.runtime_data, entry.entry_id, slot) for slot in SETTINGS])


class PelTecSetting(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry_id, slot):
        super().__init__(coordinator)
        self.slot = slot
        name, step, object_id, self.default = SETTINGS[slot]
        self._attr_name = name
        self._attr_native_step = step
        self._attr_native_min_value, self._attr_native_max_value = LIMITS[slot]
        self._attr_unique_id = f"{entry_id}_setting_{slot}"
        self._attr_device_info = device_info(entry_id)
        self.entity_id = NUMBER_PREFIX + object_id

    @property
    def extra_state_attributes(self):
        return {"Setting index": self.slot, "Default": self.default}

    @property
    def native_value(self):
        return number((self.coordinator.data or {}).get("values", {}).get(f"PVAL_{self.slot}_0"))

    @property
    def available(self):
        d = self.coordinator.data or {}
        return self.coordinator.last_update_success and d.get("mode") == "local" and self.native_value is not None

    async def async_set_native_value(self, value):
        if not await self.coordinator.async_send_setting(self.slot, value):
            raise HomeAssistantError("The local server refused this setting")
        await self.coordinator.async_request_refresh()
