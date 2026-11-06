"""Connection of the WiFi module to the local server."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .parameters import CONNECTION_ENTITY_ID
from .sensor import device_info


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ModuleConnection(entry.runtime_data, entry.entry_id)])


class ModuleConnection(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "System connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_connection"
        self._attr_device_info = device_info(entry_id)
        self.entity_id = CONNECTION_ENTITY_ID

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("data_fresh"))
