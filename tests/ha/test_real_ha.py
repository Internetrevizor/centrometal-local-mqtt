"""The integration in a real Home Assistant, talking to the real app over loopback, with a module simulated at the MQTT level."""
import asyncio
import json
from unittest.mock import patch

import gateway as g
from homeassistant.config_entries import SOURCE_HASSIO, ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "centrometal_local"
KEY = g.signing_key(b"test-pass")
TOKEN = "test-token"
SWITCH = "switch.peltec_ii_lambda_boiler_switch"
UPPER = "sensor.peltec_ii_lambda_puffer_upper"


def mstr(value):
    return len(value).to_bytes(2, "big") + value


def connect():
    return g.frame(0x10, mstr(b"MQTT") + bytes([4, 0xC2]) + (3600).to_bytes(2, "big") + mstr(b"BOXID") + mstr(b"BOXID") + mstr(b"test-pass"))


def subscribe():
    return g.frame(0x82, b"\x00\x01" + mstr(b"cm.srv.peltec2.BOXID") + b"\x00")


def up(obj):
    return g.pub_frame("cm.inst.peltec2.BOXID", g.sign_obj(obj, KEY))


class App:
    """The real gateway, plus a module that answers CMD with B_CMD when it is told to."""

    def __init__(self, echo=True):
        self.gateway = g.Gateway({
            "module_ip": "127.0.0.1", "connection_mode": "local", "read_interval_seconds": 60, "first_poll_delay_seconds": 0,
            "api_token": TOKEN, "event_heartbeat_seconds": .2})
        self.echo = echo
        self.tasks = []

    async def start(self):
        self.mqtt = await asyncio.start_server(self.gateway.handle_module, "127.0.0.1", 0)
        self.api = await asyncio.start_server(lambda r, w: g.send_http_status(r, w, self.gateway), "127.0.0.1", 0)
        self.api_port = self.api.sockets[0].getsockname()[1]
        reader, self.writer = await asyncio.open_connection("127.0.0.1", self.mqtt.sockets[0].getsockname()[1])
        self.writer.write(connect() + subscribe() + up({"_sync": "id", "_token": "t", "clMsgId": 1}))
        self.writer.write(up({"B_Tak1_1": 55, "B_Tak2_1": 40, "B_CMD": 0, "K1B_onOff": 1, "B_STATE": "OFF", "PVAL_67_0": 67, "clMsgId": 2}))
        await self.writer.drain()
        self.tasks.append(asyncio.create_task(self._module(reader)))

    async def _module(self, reader):
        frames = g.MQTTFrames()
        while chunk := await reader.read(65536):
            for head, body in frames.feed(chunk):
                if head >> 4 != 3:
                    continue
                message = g.verify_payload(g.get_publish(head, body)[1], KEY)
                if self.echo and message and "CMD" in message:
                    self.writer.write(up({"B_CMD": message["CMD"], "clMsgId": 3}))
                    await self.writer.drain()

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        self.writer.close()
        self.mqtt.close()
        self.api.close()


async def until(check, seconds=5):
    for _ in range(int(seconds / .05)):
        if check():
            return
        await asyncio.sleep(.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def expected_lingering_tasks():
    return True


@pytest.fixture
async def app(socket_enabled):
    server = App()
    await server.start()
    yield server
    await server.stop()


async def add_entry(hass, app, token=TOKEN):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={"host": "127.0.0.1", "port": app.api_port, "token": token})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=False)
    return entry


async def test_entities_keep_their_ids_and_follow_the_stream(hass, app):
    entry = await add_entry(hass, app)
    assert entry.state is ConfigEntryState.LOADED
    await until(lambda: hass.states.get(UPPER) is not None and hass.states.get(UPPER).state == "55.0")
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get("switch.k1_heating_circuit_off_on").state == "on"
    assert hass.states.get("sensor.peltec_ii_lambda_gateway_state").state == "local"
    assert hass.states.get("sensor.peltec_ii_lambda_sessions_closed") is not None
    assert hass.states.get("button.peltec_ii_lambda_clear_alarms") is None
    assert hass.states.get("binary_sensor.centrometal_boiler_system_connection").state == "on"
    app.gateway.state.ingest({"B_Tak1_1": 56, "clMsgId": 5})
    await until(lambda: hass.states.get(UPPER).state == "56.0")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_last_reported_keeps_moving_while_the_value_stays_the_same(hass, app):
    """incalzire.yaml reads last_reported of the puffer sensors: an unchanged value must still be written."""
    entry = await add_entry(hass, app)
    await until(lambda: hass.states.get(UPPER) is not None and hass.states.get(UPPER).state == "55.0")
    before = hass.states.get(UPPER).last_reported
    app.gateway.state.ingest({"B_Tak1_1": 55, "clMsgId": 5})
    await until(lambda: hass.states.get(UPPER).last_reported > before)
    assert hass.states.get(UPPER).state == "55.0"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_switch_returns_once_the_boiler_confirms(hass, app):
    entry = await add_entry(hass, app)
    await until(lambda: hass.states.get(SWITCH).state == "off")
    events = []
    hass.bus.async_listen("centrometal_local_command_result", lambda event: events.append(event.data))
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await until(lambda: hass.states.get(SWITCH).state == "on")
    await until(lambda: events)
    assert events[0]["state"] == "confirmed"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_unconfirmed_command_is_not_reported_as_failed_while_the_app_keeps_trying(hass, app):
    app.echo = False
    entry = await add_entry(hass, app)
    await until(lambda: hass.states.get(SWITCH).state == "off")
    with patch("custom_components.centrometal_local.COMMAND_WAIT_SECONDS", .2):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    assert hass.states.get(SWITCH).state == "off"
    assert app.gateway.status()["pending_command"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setting_the_held_value_writes_nothing(hass, app):
    entry = await add_entry(hass, app)
    number = "number.technical_room_centrometal_peltec_ii_peltec_ii_lambda_buffer_tank_temperature"
    await until(lambda: hass.states.get(number) is not None and hass.states.get(number).state not in ("unknown", "unavailable"))
    assert float(hass.states.get(number).state) == 67
    await hass.services.async_call("number", "set_value", {"entity_id": number, "value": 67}, blocking=True)
    assert app.gateway.status()["commands"]["setting_67"]["state"] == "unchanged"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_refused_command_raises(hass, app):
    entry = await add_entry(hass, app)
    await until(lambda: hass.states.get(SWITCH).state == "off")
    with patch.object(app.gateway, "request_command", return_value=None), pytest.raises(HomeAssistantError):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_entities_stay_through_a_short_silence_of_the_app(hass, app):
    entry = await add_entry(hass, app)
    await until(lambda: hass.states.get(UPPER).state == "55.0")
    coordinator = entry.runtime_data
    coordinator._last_answer -= 100  # past the stream, inside the tolerance
    app.api.close()
    await coordinator.async_refresh()
    assert hass.states.get(UPPER).state == "55.0"
    assert await hass.config_entries.async_unload(entry.entry_id)


def service_info(host="dns-name", port=8765, token="tok"):
    return HassioServiceInfo(config={"host": host, "port": port, "token": token, "addon": "App"},
                             name="App", slug="5c24f09b_centrometal_local_mqtt", uuid="1")


async def test_discovery_updates_an_entry_made_by_an_older_version(hass, socket_enabled):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="127.0.0.1:8765", data={"host": "127.0.0.1", "port": 8765})
    entry.add_to_hass(hass)
    with patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=service_info())
    assert (result["type"], result["reason"]) == ("abort", "already_configured")
    assert entry.unique_id == DOMAIN
    assert entry.data == {"host": "dns-name", "port": 8765, "token": "tok"}
    reload.assert_called_once()


async def test_discovery_with_the_same_details_does_not_reload(hass, socket_enabled):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={"host": "dns-name", "port": 8765, "token": "tok"})
    entry.add_to_hass(hass)
    with patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload") as reload:
        await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=service_info())
    reload.assert_not_called()


async def test_discovery_on_a_fresh_install_asks_first(hass, socket_enabled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=service_info())
    assert (result["type"], result["step_id"]) == ("form", "hassio_confirm")
    with patch("custom_components.centrometal_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["data"] == {"host": "dns-name", "port": 8765, "token": "tok"}


async def test_adding_by_hand_says_the_app_is_needed(hass, socket_enabled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert (result["type"], result["reason"]) == ("abort", "app_required")


async def test_app_of_another_version_raises_an_issue_and_the_right_one_clears_it(hass, app):
    from homeassistant.helpers import issue_registry as ir
    entry = await add_entry(hass, app)
    await until(lambda: entry.runtime_data.data)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "app_version_mismatch") is None
    entry.runtime_data._apply({"values": {}, "api_version": 999})
    assert registry.async_get_issue(DOMAIN, "app_version_mismatch") is not None
    entry.runtime_data._apply({"values": {}, "api_version": 1})
    assert registry.async_get_issue(DOMAIN, "app_version_mismatch") is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_hide_the_token(hass, app):
    from custom_components.centrometal_local.diagnostics import async_get_config_entry_diagnostics
    entry = await add_entry(hass, app)
    await until(lambda: entry.runtime_data.data and entry.runtime_data.data["values"])
    text = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    assert TOKEN not in text
    assert "B_Tak1_1" in text
    assert await hass.config_entries.async_unload(entry.entry_id)
