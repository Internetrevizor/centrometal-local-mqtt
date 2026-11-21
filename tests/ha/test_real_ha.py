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


def subscribe(product="peltec2"):
    return g.frame(0x82, b"\x00\x01" + mstr(f"cm.srv.{product}.BOXID".encode()) + b"\x00")


def up(obj, product="peltec2"):
    return g.pub_frame(f"cm.inst.{product}.BOXID", g.sign_obj(obj, KEY))


class App:
    """The real gateway, plus a module that answers CMD with B_CMD when it is told to."""

    def __init__(self, echo=True, product="peltec2"):
        self.product = product
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
        self.writer.write(connect() + subscribe(self.product) + up({"_sync": "id", "_token": "t", "clMsgId": 1}, self.product))
        self.writer.write(up({"B_Tak1_1": 55, "B_Tak2_1": 40, "B_CMD": 0, "K1B_onOff": 1, "B_STATE": "OFF", "PVAL_67_0": 67,
                              "clMsgId": 2}, self.product))
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
    assert hass.states.get("switch.peltec_ii_lambda_dhw_circuit").state == "on"
    assert hass.states.get("sensor.peltec_ii_lambda_gateway_state").state == "local"
    assert hass.states.get("sensor.peltec_ii_lambda_gateway_state").attributes["at_commands"] == {}
    assert hass.states.get("sensor.peltec_ii_lambda_sessions_closed") is not None
    assert hass.states.get("button.peltec_ii_lambda_clear_alarms") is None
    assert hass.states.get("binary_sensor.peltec_ii_lambda_system_connection").state == "on"
    app.gateway.state.ingest({"B_Tak1_1": 56, "clMsgId": 5})
    await until(lambda: hass.states.get(UPPER).state == "56.0")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_waits_for_the_app(hass, socket_enabled):
    """Which boiler the app serves decides the device and the entities that write: no setup before the app answers."""
    other = App(product="cmpelet")
    await other.start()
    try:
        port = other.api_port
        other.api.close()
        await other.api.wait_closed()
        entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={"host": "127.0.0.1", "port": port, "token": TOKEN})
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert hass.states.async_entity_ids("switch") == []
    finally:
        await other.stop()


async def test_another_boiler_is_read_only_and_named_after_its_product(hass, socket_enabled):
    other = App(product="cmpelet")
    await other.start()
    try:
        await until(lambda: other.gateway.state.product == "cmpelet")
        entry = await add_entry(hass, other)
        upper = "sensor.cm_pelet_set_puffer_upper"
        await until(lambda: hass.states.get(upper) is not None and hass.states.get(upper).state == "55.0")
        for domain in ("switch", "number", "select"):
            assert hass.states.async_entity_ids(domain) == [], domain
        from homeassistant.helpers import device_registry as dr
        device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
        assert (device.name, device.model) == ("CM Pelet-set", "CM Pelet-set")
        assert hass.states.get("sensor.cm_pelet_set_gateway_state").state == "local"
        # Named after the counters of its own family, and the readings of a boiler with no switch are shown.
        other.gateway.state.ingest({"CNT_1": 125, "B_CMD": 1, "C1B_Tsob1": 21.5, "clMsgId": 7})
        dhw_only = "sensor.cm_pelet_set_working_dhw_only"
        await until(lambda: hass.states.get(dhw_only) is not None and hass.states.get(dhw_only).state == "125")
        assert hass.states.get(dhw_only).attributes["unit_of_measurement"] == "min"
        assert hass.states.get("sensor.cm_pelet_set_command_active").state == "On"
        assert hass.states.get("sensor.cm_pelet_set_circuit_1_room_measured_temperature").state == "21.5"
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await other.stop()


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


async def test_last_reported_is_the_boilers_report_not_the_apps_message(hass, app):
    """The app's heartbeat and the reports of other parameters must not pass for a report of this one."""
    entry = await add_entry(hass, app)
    await until(lambda: hass.states.get(UPPER) is not None and hass.states.get(UPPER).state == "55.0")
    await asyncio.sleep(.3)  # the first messages of the stream are behind
    before = hass.states.get(UPPER).last_reported
    lower = "sensor.peltec_ii_lambda_puffer_lower"
    app.gateway.state.ingest({"B_Tak2_1": 41, "clMsgId": 5})
    await until(lambda: hass.states.get(lower).state == "41.0")
    await asyncio.sleep(.7)  # three heartbeats of the app
    assert hass.states.get(UPPER).last_reported == before
    app.gateway.state.ingest({"B_Tak1_1": 55, "clMsgId": 6})
    await until(lambda: hass.states.get(UPPER).last_reported > before)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_what_describes_the_boiler_stays_while_a_reading_goes_old(hass, app):
    entry = await add_entry(hass, app)
    version = "sensor.peltec_ii_lambda_software_version"
    app.gateway.state.ingest({"B_VER": "v3.03dL", "clMsgId": 5})
    await until(lambda: hass.states.get(version) is not None and hass.states.get(version).state == "v3.03dL")
    coordinator = entry.runtime_data
    for key in ("B_VER", "B_Tak1_1"):
        coordinator._received[key] -= 1000
    await until(lambda: hass.states.get(UPPER).state == "unavailable")
    assert hass.states.get(version).state == "v3.03dL"
    app.gateway.state.ingest({"B_Tak1_1": 56, "clMsgId": 6})
    await until(lambda: hass.states.get(UPPER).state == "56.0")
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
    number = "number.peltec_ii_lambda_buffer_tank_temperature"
    await until(lambda: hass.states.get(number) is not None and hass.states.get(number).state not in ("unknown", "unavailable"))
    assert float(hass.states.get(number).state) == 67
    await hass.services.async_call("number", "set_value", {"entity_id": number, "value": 67}, blocking=True)
    assert app.gateway.status()["commands"]["setting_67"]["state"] == "unchanged"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_clicks_on_a_setting_send_only_the_last_value(hass, app):
    entry = await add_entry(hass, app)
    number = "number.peltec_ii_lambda_buffer_tank_temperature"
    await until(lambda: hass.states.get(number) is not None and hass.states.get(number).state not in ("unknown", "unavailable"))
    sent = []
    real = app.gateway.request_setting

    def spy(slot, value):
        sent.append((slot, value))
        return real(slot, value)

    with patch("custom_components.centrometal_local.number.WRITE_SETTLE_SECONDS", .3), \
            patch("custom_components.centrometal_local.COMMAND_WAIT_SECONDS", .2), patch.object(app.gateway, "request_setting", spy):
        await asyncio.gather(*(hass.services.async_call("number", "set_value", {"entity_id": number, "value": v}, blocking=True)
                               for v in (68, 69, 70)))
    assert sent == [(67, 70)]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_sensor_left_by_1_6_9_is_removed(hass, app):
    from homeassistant.helpers import entity_registry as er
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={"host": "127.0.0.1", "port": app.api_port, "token": TOKEN})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_PVAL_71_0", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=False)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_PVAL_71_0") is None
    assert registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_setting_71") is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_install_of_1_6_10_is_aligned_once_with_the_device_name(hass, app):
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, version=1, minor_version=1,
                            data={"host": "127.0.0.1", "port": app.api_port, "token": TOKEN})
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)},
                                                     name="PelTec II Lambda")
    registry = er.async_get(hass)
    old = registry.async_get_or_create("number", DOMAIN, f"{entry.entry_id}_setting_67", config_entry=entry, device_id=device.id,
                                       suggested_object_id="technical_room_centrometal_peltec_ii_peltec_ii_lambda_buffer_tank_temperature",
                                       original_name="Buffer tank temperature")
    kept = registry.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_B_fan01", config_entry=entry, device_id=device.id,
                                        suggested_object_id="peltec_ii_lambda_fan_speed", original_name="Fan")
    other = registry.async_get_or_create("sensor", "other", "x", suggested_object_id="technical_room_other", original_name="Other")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=False)
    assert registry.async_get(old.id).entity_id == "number.peltec_ii_lambda_buffer_tank_temperature"
    assert registry.async_get(kept.id).entity_id == "sensor.peltec_ii_lambda_fan_speed"
    assert registry.async_get(other.id).entity_id == "sensor.technical_room_other"
    assert entry.minor_version == 2
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


async def test_discovery_takes_the_address_out_of_an_old_title_without_a_reload(hass, socket_enabled):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="PelTec local (127.0.0.1)", data={"host": "dns-name", "port": 8765, "token": "tok"})
    entry.add_to_hass(hass)
    with patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload") as reload:
        await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=service_info())
    assert entry.title == "PelTec local"
    reload.assert_not_called()


async def test_discovery_keeps_a_title_the_user_chose(hass, socket_enabled):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Centrala", data={"host": "dns-name", "port": 8765, "token": "tok"})
    entry.add_to_hass(hass)
    await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=service_info())
    assert entry.title == "Centrala"


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
