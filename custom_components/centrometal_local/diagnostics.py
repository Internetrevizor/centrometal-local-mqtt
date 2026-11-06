"""The Download diagnostics button: the app's state, without anything that opens the door."""
from homeassistant.components.diagnostics import async_redact_data

TO_REDACT = {"token", "host", "B_INST", "rejected_ip"}


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    return async_redact_data({
        "entry": dict(entry.data),
        "last_update_success": coordinator.last_update_success,
        "status": {key: value for key, value in data.items() if key not in ("values", "value_age_seconds")},
        "values": data.get("values", {}),
        "value_age_seconds": data.get("value_age_seconds", {}),
    }, TO_REDACT)
