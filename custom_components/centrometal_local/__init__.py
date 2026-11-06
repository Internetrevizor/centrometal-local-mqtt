"""Centrometal Local MQTT Protocol: Home Assistant side of the local server."""
from datetime import timedelta
import logging

from aiohttp import ClientError, ClientTimeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "number", "sensor", "switch"]


class GatewayCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, host: str, port: int):
        super().__init__(hass, LOGGER, config_entry=entry, name="Centrometal Local MQTT Protocol",
                         update_interval=timedelta(seconds=10))
        self.base_url = f"http://{host}:{port}"

    async def _async_update_data(self):
        try:
            async with async_get_clientsession(self.hass).get(
                f"{self.base_url}/status", timeout=ClientTimeout(total=5)
            ) as response:
                response.raise_for_status()
                obj = await response.json()
                if not isinstance(obj, dict) or not isinstance(obj.get("values"), dict):
                    raise UpdateFailed("Malformed gateway status")
                return obj
        except (ClientError, TimeoutError, ValueError) as exc:
            raise UpdateFailed(f"Local server not reachable: {type(exc).__name__}") from exc

    async def async_send_command(self, value: int) -> bool:
        return await self._post("command", {"CMD": value})

    async def async_send_setting(self, slot: int, value: float) -> bool:
        return await self._post("setting", {"slot": slot, "value": value})

    async def _post(self, path: str, body: dict) -> bool:
        try:
            async with async_get_clientsession(self.hass).post(
                f"{self.base_url}/{path}", json=body, timeout=ClientTimeout(total=5)
            ) as response:
                return response.status == 200
        except (ClientError, TimeoutError):
            return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GatewayCoordinator(hass, entry, entry.data["host"], entry.data["port"])
    await coordinator.async_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
