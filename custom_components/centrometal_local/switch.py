"""PelTec on/off through the local server. Available in local mode while the readings are fresh."""
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .parameters import (DHW_PRIORITY_ENTITY_ID, DHW_PRIORITY_SLOT, K1_SWITCH_ENTITY_ID, K1_SWITCH_SLOT, SWITCH_ENTITY_ID,
                         commands_available, number, setting_value)
from .sensor import device_info

LOGGER = logging.getLogger(__name__)


async def _deliver(coordinator, send, what):
    """Waits for what came of the request. The app keeps trying through a reconnect, so running out of patience here is not
    a failure: the switch shows the real state, and the result arrives later as an event."""
    answer = await send()
    if answer is None:
        raise HomeAssistantError(f"The local server did not answer the {what}")
    state = answer.get("state")
    if not answer.get("accepted") or state == "expired":
        raise HomeAssistantError(f"The local server could not deliver the {what}: {state}")
    if state not in ("confirmed", "unchanged"):
        LOGGER.warning("The %s is not confirmed yet: %s", what, state)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PelTecCommand(entry.runtime_data, entry.entry_id), K1Circuit(entry.runtime_data, entry.entry_id),
                        DhwPriority(entry.runtime_data, entry.entry_id)])


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
        return self.coordinator.last_update_success and commands_available(self.coordinator.data)

    @property
    def is_on(self):
        value = (self.coordinator.data or {}).get("values", {}).get("B_CMD")
        value = number(value)
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
        return self.coordinator.last_update_success and commands_available(self.coordinator.data)

    @property
    def is_on(self):
        value = (self.coordinator.data or {}).get("values", {}).get("K1B_onOff")
        value = number(value)
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


class DhwPriority(CoordinatorEntity, SwitchEntity):
    """DHW priority (controller manual 5.2): while the DHW pump runs, the heating pumps wait. Written to slot 898."""

    _attr_has_entity_name = True
    _attr_name = "DHW Priority Setting"
    _attr_icon = "mdi:priority-high"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_setting_{DHW_PRIORITY_SLOT}"
        self._attr_device_info = device_info(entry_id)
        self.entity_id = DHW_PRIORITY_ENTITY_ID

    @property
    def available(self):
        return self.coordinator.last_update_success and commands_available(self.coordinator.data)

    @property
    def is_on(self):
        value = setting_value((self.coordinator.data or {}).get("values"), DHW_PRIORITY_SLOT, "B_PTV_PRI")
        return None if value is None else value == 1

    @property
    def extra_state_attributes(self):
        return {"Setting index": DHW_PRIORITY_SLOT}

    async def _send(self, value):
        await _deliver(self.coordinator, lambda: self.coordinator.async_send_setting(DHW_PRIORITY_SLOT, value), "DHW priority")

    async def async_turn_on(self, **kwargs):
        await self._send(1)

    async def async_turn_off(self, **kwargs):
        await self._send(0)
