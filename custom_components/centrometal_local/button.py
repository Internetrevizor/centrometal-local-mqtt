"""Ends an alarm by hand. Nobody has seen yet how a PelTec II reports that an alarm has passed."""
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .parameters import CLEAR_ALARMS_ENTITY_ID
from .sensor import device_info


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ClearAlarms(entry.runtime_data, entry.entry_id)])


class ClearAlarms(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Clear Alarms"
    _attr_icon = "mdi:bell-off"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_clear_alarms"
        self._attr_device_info = device_info(entry_id)
        self.entity_id = CLEAR_ALARMS_ENTITY_ID

    async def async_press(self):
        if await self.coordinator.async_clear_alarms() is None:
            raise HomeAssistantError("The local server did not answer")
