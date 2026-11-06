"""Centrometal Local MQTT Protocol: Home Assistant side of the local server."""
from datetime import timedelta
import json
import logging

from aiohttp import ClientError, ClientTimeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "number", "sensor", "switch"]


def read_connection_file(path):
    """The address and token the app wrote at its last start, or None when there is no such file."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and data.get("host") and data.get("port") else None
    except (OSError, ValueError):
        return None


class GatewayCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, host: str, port: int):
        super().__init__(hass, LOGGER, config_entry=entry, name="Centrometal Local MQTT Protocol",
                         update_interval=timedelta(seconds=10))
        self.host, self.port, self.token = host, port, None
        self.connection_file = hass.config.path("custom_components", "centrometal_local", "gateway.json")

    async def async_load_connection(self):
        """The app can get a new address when it restarts, so this runs at start and after every failure."""
        data = await self.hass.async_add_executor_job(read_connection_file, self.connection_file)
        if data:
            self.host, self.port, self.token = data["host"], data["port"], data.get("token")

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _async_update_data(self):
        try:
            async with async_get_clientsession(self.hass).get(
                f"http://{self.host}:{self.port}/status", headers=self._headers, timeout=ClientTimeout(total=5)
            ) as response:
                response.raise_for_status()
                obj = await response.json()
                if not isinstance(obj, dict) or not isinstance(obj.get("values"), dict):
                    raise UpdateFailed("Malformed gateway status")
                return obj
        except (ClientError, TimeoutError, ValueError) as exc:
            await self.async_load_connection()
            raise UpdateFailed(f"Local server not reachable: {type(exc).__name__}") from exc

    async def async_send_command(self, value: int) -> bool:
        return await self._post("command", {"CMD": value})

    async def async_send_setting(self, slot: int, value: float) -> bool:
        return await self._post("setting", {"slot": slot, "value": value})

    async def _post(self, path: str, body: dict) -> bool:
        try:
            async with async_get_clientsession(self.hass).post(
                f"http://{self.host}:{self.port}/{path}", json=body, headers=self._headers, timeout=ClientTimeout(total=5)
            ) as response:
                return response.status == 200
        except (ClientError, TimeoutError):
            return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GatewayCoordinator(hass, entry, entry.data["host"], entry.data["port"])
    await coordinator.async_load_connection()
    await coordinator.async_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
