"""The writable PelTec settings. Limits come from the controller; what is shown is read back from it."""
import asyncio

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .parameters import LIMITS, NUMBER_PREFIX, SETTINGS, WRITE_SETTLE_SECONDS, commands_available, number
from .sensor import device_info
from .switch import _deliver

PARALLEL_UPDATES = 0  # a later click must be able to replace one still waiting


async def async_setup_entry(hass, entry, async_add_entities):
    # 1.6.9 showed the stop buffer tank difference as a sensor; it is a setting again, and the sensor would stay behind.
    registry = er.async_get(hass)
    if old := registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_PVAL_71_0"):
        registry.async_remove(old)
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
        return (self.coordinator.last_update_success and commands_available(self.coordinator.data)
                and self.native_value is not None)

    async def async_set_native_value(self, value):
        """Only the value the box settles on is written: every click of its arrows is a call of its own."""
        self._wanted = value
        await asyncio.sleep(WRITE_SETTLE_SECONDS)
        if getattr(self, "_wanted", None) != value:
            return
        await _deliver(self.coordinator, lambda: self.coordinator.async_send_setting(self.slot, value), "setting")
