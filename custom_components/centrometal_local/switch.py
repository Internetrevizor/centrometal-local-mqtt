"""PelTec on/off through the local server. Available only in local mode with a synchronised module."""
import asyncio

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .parameters import K1_SWITCH_ENTITY_ID, K1_SWITCH_SLOT, SWITCH_ENTITY_ID
from .sensor import device_info


async def _deliver(coordinator, send, what):
    """The module reconnects within seconds when its link drops, so a refused command is tried again before failing."""
    for attempt in range(4):
        if await send():
            await asyncio.sleep(1)  # the echo from the module arrives in about 0.3 s
            await coordinator.async_request_refresh()
            return
        if attempt < 3:
            await asyncio.sleep(5)
    raise HomeAssistantError(f"The local server refused the {what}")


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PelTecCommand(entry.runtime_data, entry.entry_id), K1Circuit(entry.runtime_data, entry.entry_id)])


class PelTecCommand(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Power Switch"
    _attr_icon = "mdi:fire"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_command"
        self.entity_id = SWITCH_ENTITY_ID
        self._attr_device_info = device_info(entry_id)

    @property
    def available(self):
        d = self.coordinator.data or {}
        return self.coordinator.last_update_success and d.get("mode") == "local" and bool(d.get("data_fresh"))

    @property
    def is_on(self):
        value = (self.coordinator.data or {}).get("values", {}).get("B_CMD")
        return None if value is None else value == 1

    @property
    def extra_state_attributes(self):
        values = (self.coordinator.data or {}).get("values", {})
        return {"Command Active (B_CMD)": values.get("B_CMD"), "Boiler State (B_STATE)": values.get("B_STATE")}

    async def _send(self, value):
        await _deliver(self.coordinator, lambda: self.coordinator.async_send_command(value), "PelTec command")

    async def async_turn_on(self, **kwargs):
        await self._send(1)

    async def async_turn_off(self, **kwargs):
        await self._send(0)


class K1Circuit(CoordinatorEntity, SwitchEntity):
    """The K1 circuit, which loads the DHW tank on scheme 43. Written to slot 272, read from K1B_onOff."""

    _attr_has_entity_name = True
    _attr_name = "DHW Circuit"
    _attr_icon = "mdi:water-boiler"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_k1_circuit"
        self._attr_device_info = device_info(entry_id)
        self.entity_id = K1_SWITCH_ENTITY_ID

    @property
    def available(self):
        d = self.coordinator.data or {}
        return self.coordinator.last_update_success and d.get("mode") == "local" and bool(d.get("data_fresh"))

    @property
    def is_on(self):
        value = (self.coordinator.data or {}).get("values", {}).get("K1B_onOff")
        return None if value is None else value == 1

    @property
    def extra_state_attributes(self):
        values = (self.coordinator.data or {}).get("values", {})
        return {"State source": "K1B_onOff", "K1B_onOff": values.get("K1B_onOff"), "PVAL": values.get(f"PVAL_{K1_SWITCH_SLOT}_0")}

    async def _send(self, value):
        await _deliver(self.coordinator, lambda: self.coordinator.async_send_setting(K1_SWITCH_SLOT, value), "K1 circuit command")

    async def async_turn_on(self, **kwargs):
        await self._send(1)

    async def async_turn_off(self, **kwargs):
        await self._send(0)
