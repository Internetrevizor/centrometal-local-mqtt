"""A sensor for every parameter the boiler sends, created the first time it arrives."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .parameters import COUNTER_SENSORS, display_value, enabled_by_default, extra_attributes, is_entity, is_fresh, parameter_info


def device_info(entry_id):
    return DeviceInfo(identifiers={(DOMAIN, entry_id)}, name="PelTec II Lambda", manufacturer="Centrometal",
                      model="PelTec II Lambda", sw_version="Centrometal Local MQTT Protocol")


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    seen = set()

    def add_keys():
        values = (coordinator.data or {}).get("values")
        if not isinstance(values, dict):
            return
        keys = [k for k in values if is_entity(k) and k not in seen]
        if keys:
            seen.update(keys)
            async_add_entities([PelTecValue(coordinator, entry.entry_id, key) for key in keys])

    async_add_entities([BridgeStatus(coordinator, entry.entry_id), EventHistory(coordinator, entry.entry_id),
                        *(GatewayCounter(coordinator, entry.entry_id, key) for key in COUNTER_SENSORS)])
    add_keys()
    entry.async_on_unload(coordinator.async_add_listener(add_keys))


class BridgeStatus(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Gateway State"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_state"
        self._attr_device_info = device_info(entry_id)

    @property
    def native_value(self):
        return self.coordinator.data.get("mode") if self.coordinator.data else None

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        attributes = {k: d.get(k) for k in ("module_connected", "upstream_connected", "data_fresh", "connection_mode",
                                            "read_interval_seconds", "invalid_messages", "rejected_values", "malformed_frames", "stale_messages",
                                            "last_malformed", "at_commands", "last_command", "pending_command", "commands",
                                            "sessions_closed", "close_reasons", "last_session_end",
                                            "last_session_end_time", "identity_changes", "rejected_ip")}
        attributes["parameters"] = len(d.get("values") or {})
        return attributes


class GatewayCounter(CoordinatorEntity, SensorEntity):
    """Kept by the app across restarts, and as a sensor it stays in the long-term history."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, entry_id, key):
        super().__init__(coordinator)
        self.key = key
        self._attr_name = COUNTER_SENSORS[key][0]
        self._attr_unique_id = f"{entry_id}_counter_{key}"
        self._attr_device_info = device_info(entry_id)

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get(self.key)


class EventHistory(CoordinatorEntity, SensorEntity):
    """Errors, warnings and information messages seen since the app started."""

    _attr_has_entity_name = True
    _attr_name = "Event History"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_event_history"
        self._attr_device_info = device_info(entry_id)

    @property
    def native_value(self):
        d = self.coordinator.data or {}
        alarms = d.get("alarms") or {}
        if alarms:
            return ", ".join(sorted(alarms))[:250]
        return f"{len(d.get('alarm_history') or [])} past events" if d.get("alarm_history") else "No events"

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        history = d.get("alarm_history") or []
        return {"Active": d.get("alarms") or {}, "Event count": len(history), "Events": history, "Original name": "Errors_History"}


class PelTecValue(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id, key):
        super().__init__(coordinator)
        self.key = key
        name, kind, extra, diagnostic, entity_id = parameter_info(key)
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = device_info(entry_id)
        self._attr_entity_registry_enabled_default = enabled_by_default(key)
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if kind in ("temperature", "setpoint"):
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        if kind in ("temperature", "measure"):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if kind == "counter":
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        if kind in ("number", "counter", "measure") and extra:
            self._attr_native_unit_of_measurement = extra

    @property
    def native_value(self):
        return display_value(self.key, (self.coordinator.data or {}).get("values", {}).get(self.key))

    @property
    def extra_state_attributes(self):
        return extra_attributes(self.key, (self.coordinator.data or {}).get("values", {}))

    @property
    def available(self):
        d = self.coordinator.data or {}
        return self.coordinator.last_update_success and is_fresh((d.get("value_age_seconds") or {}).get(self.key), self.key)
