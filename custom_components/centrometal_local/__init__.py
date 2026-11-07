"""Centrometal Local MQTT Protocol: Home Assistant side of the local server."""
import asyncio
from datetime import timedelta
import json
import logging
import time

from aiohttp import ClientError, ClientTimeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import COMMAND_RESULT_EVENT, DOMAIN
from .parameters import API_VERSION

LOGGER = logging.getLogger(__name__)
API_ISSUE = "app_version_mismatch"
PLATFORMS = ["binary_sensor", "number", "select", "sensor", "switch"]
APP_SILENCE_TOLERATED_SECONDS = 120  # an update of the app takes about a minute: entities stay as they are meanwhile
STREAM_SILENCE_SECONDS = 90  # the app sends a heartbeat every 30 s
STREAM_RETRY_SECONDS = 5
COMMAND_WAIT_SECONDS = 15  # usually confirmed in under a second; a reconnect is left to the app, which keeps trying
POST_ATTEMPTS = 4
POST_RETRY_SECONDS = 5
FINISHED = ("confirmed", "unchanged", "expired", "replaced", "rate_limited")


class GatewayCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        super().__init__(hass, LOGGER, config_entry=entry, name="Centrometal Local MQTT Protocol",
                         update_interval=timedelta(seconds=60))
        self.host, self.port, self.token = entry.data["host"], entry.data["port"], entry.data.get("token")
        self._last_answer = None
        self._values = {}
        self._received = {}
        self._reported_commands = None

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _apply(self, event):
        """Keeps every value and when it was last reported. A snapshot replaces all of it, a delta only adds."""
        now = time.monotonic()
        if event.get("type") == "delta":
            self._values.update(event.get("values", {}))
            self._received.update(dict.fromkeys([*event.get("values", {}), *event.get("seen", [])], now))
        else:
            self._values = dict(event["values"])
            ages = event.get("value_age_seconds", {})
            self._received = {key: now - ages.get(key, 0) for key in self._values}
            self._check_api_version(event.get("api_version"))
        self._last_answer = now
        self._report_commands(event.get("commands"))
        return self._build(event, now)

    def _check_api_version(self, version):
        """The app and the integration update separately. When they no longer understand each other, the user has to know."""
        if version == API_VERSION:
            ir.async_delete_issue(self.hass, DOMAIN, API_ISSUE)
        else:
            ir.async_create_issue(self.hass, DOMAIN, API_ISSUE, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                                  translation_key=API_ISSUE)

    def _build(self, event, now):
        return {**event, "values": self._values, "value_age_seconds": {k: round(now - t, 1) for k, t in self._received.items()}}

    def _report_commands(self, commands):
        """Each command that ended becomes an event once, so an automation or a notification can follow it."""
        if not isinstance(commands, dict):
            return
        finished = {key: result for key, result in commands.items() if result.get("state") in FINISHED}
        known = self._reported_commands
        self._reported_commands = {key: result.get("id") for key, result in finished.items()}
        if known is None:
            return  # what ended before this connection is not news
        for key, result in finished.items():
            if known.get(key) != result.get("id"):
                self.hass.bus.async_fire(COMMAND_RESULT_EVENT, {"command": key, **result})

    async def _async_update_data(self):
        """The stream brings the data. This keeps the ages current, and reads directly when the stream is down."""
        now = time.monotonic()
        if self.data is not None and self._last_answer is not None and now - self._last_answer < STREAM_SILENCE_SECONDS:
            return self._build(self.data, now)
        try:
            async with async_get_clientsession(self.hass).get(
                f"http://{self.host}:{self.port}/status", headers=self._headers, timeout=ClientTimeout(total=5)
            ) as response:
                response.raise_for_status()
                obj = await response.json()
                if not isinstance(obj, dict) or not isinstance(obj.get("values"), dict):
                    raise UpdateFailed("Malformed gateway status")
                return self._apply(obj)
        except (ClientError, TimeoutError, ValueError) as exc:
            if self.data is not None and self._last_answer is not None \
                    and time.monotonic() - self._last_answer < APP_SILENCE_TOLERATED_SECONDS:
                return self.data
            raise UpdateFailed(f"Local server not reachable: {type(exc).__name__}") from exc

    async def async_follow_changes(self) -> None:
        """Keeps a stream open to the app, which sends what changes as soon as the boiler reports it."""
        while True:
            try:
                await self._read_stream()
            except (ClientError, TimeoutError, ValueError) as exc:
                LOGGER.debug("Stream from the local server ended: %s", type(exc).__name__)
            await asyncio.sleep(STREAM_RETRY_SECONDS)

    async def _read_stream(self) -> None:
        async with async_get_clientsession(self.hass).get(
            f"http://{self.host}:{self.port}/events", headers=self._headers,
            timeout=ClientTimeout(total=None, sock_connect=5, sock_read=STREAM_SILENCE_SECONDS)
        ) as response:
            response.raise_for_status()
            async for line in response.content:
                if not line.startswith(b"data: "):
                    continue
                event = json.loads(line[6:])
                if isinstance(event, dict) and (event.get("type") == "delta" or isinstance(event.get("values"), dict)):
                    self.async_set_updated_data(self._apply(event))

    async def async_send_command(self, value: int):
        return await self._post("command", {"CMD": value, "wait": COMMAND_WAIT_SECONDS})

    async def async_send_setting(self, slot: int, value: float):
        return await self._post("setting", {"slot": slot, "value": value, "wait": COMMAND_WAIT_SECONDS})

    async def _post(self, path: str, body: dict):
        """What came of the request, or None when the app did not answer. Only a missing app is worth trying again here:
        everything else, the app already keeps trying or has already decided."""
        for attempt in range(POST_ATTEMPTS):
            try:
                async with async_get_clientsession(self.hass).post(
                    f"http://{self.host}:{self.port}/{path}", json=body, headers=self._headers,
                    timeout=ClientTimeout(total=COMMAND_WAIT_SECONDS + 5)
                ) as response:
                    answer = await response.json(content_type=None)
                    return answer if isinstance(answer, dict) else {}
            except (ClientError, TimeoutError, ValueError):
                if attempt < POST_ATTEMPTS - 1:
                    await asyncio.sleep(POST_RETRY_SECONDS)
        return None


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Up to 1.6.10 some entity_ids were copied from the cloud integration (technical_room_..., k1_...). Each one that
    does not start with the device name takes the id Home Assistant gives a new entity: device name + entity name."""
    if entry.version == 1 and entry.minor_version < 2:
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        from homeassistant.util import slugify

        entities = er.async_get(hass)
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        if device is not None:
            prefix = slugify(device.name_by_user or device.name or "")
            for item in er.async_entries_for_config_entry(entities, entry.entry_id):
                domain, object_id = item.entity_id.split(".", 1)
                name = item.original_name
                if not prefix or not name or object_id.startswith(prefix + "_"):
                    continue
                target = f"{domain}.{prefix}_{slugify(name)}"
                if entities.async_is_registered(target):
                    continue
                LOGGER.info("Renaming %s to %s", item.entity_id, target)
                entities.async_update_entity(item.entity_id, new_entity_id=target)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GatewayCoordinator(hass, entry)
    await coordinator.async_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, coordinator.async_follow_changes(), "centrometal_local follow changes")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
