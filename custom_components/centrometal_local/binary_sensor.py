"""Connection of the WiFi module to the local server."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .parameters import code_description, product_of
from .sensor import device_info


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ModuleConnection(entry.runtime_data, entry.entry_id), BoilerAlarm(entry.runtime_data, entry.entry_id)])


class ModuleConnection(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "System connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_connection"
        self._attr_device_info = device_info(entry_id, coordinator)

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("data_fresh"))


class BoilerAlarm(CoordinatorEntity, BinarySensorEntity):
    """On while the boiler reports an error (E) or a warning (W). Information messages (I) only go to Event History."""

    _attr_has_entity_name = True
    _attr_name = "Alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_alarm"
        self._attr_device_info = device_info(entry_id, coordinator)

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("alarms"))

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        codes = sorted(data.get("alarms") or {})
        attributes = {"codes": codes}
        # What each code means, from the boiler's manual, where it is known here (BioTec-L, CM Pelet-set).
        meanings = {code: code_description(code, product_of(data), data.get("values")) for code in codes}
        meanings = {code: text for code, text in meanings.items() if text}
        if meanings:
            attributes["Descriptions"] = meanings
        return attributes
