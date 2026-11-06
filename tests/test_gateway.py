"""Regression tests for the gateway, on loopback only. Run with: python -B -m unittest discover -s tests -v
PELTEC_PCAP = path of the private capture (optional); its secrets are never printed."""
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "centrometal_local_mqtt"))
import gateway as g  # noqa: E402

logging.disable(logging.CRITICAL)
PCAP = Path(os.environ.get("PELTEC_PCAP", "no-capture"))
PASSWORD = b"test-pass"
KEY = g.signing_key(PASSWORD)


def mstr(value):
    return len(value).to_bytes(2, "big") + value


def connect():
    return g.frame(0x10, mstr(b"MQTT") + bytes([4, 0xC2]) + (3600).to_bytes(2, "big")
                   + mstr(b"BOXID") + mstr(b"BOXID") + mstr(PASSWORD))


def subscribe():
    return g.frame(0x82, b"\x00\x01" + mstr(b"cm.srv.peltec2.BOXID") + b"\x00")


def up(obj):
    return g.pub_frame("cm.inst.peltec2.BOXID", g.sign_obj(obj, KEY))


def down(obj):
    return g.pub_frame("cm/srv/peltec2/BOXID", g.sign_obj(obj, KEY))


async def next_frame(reader, timeout=3):
    head = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
    size, factor = 0, 1
    while True:
        byte = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
        size += (byte & 127) * factor
        factor *= 128
        if byte < 128:
            break
    return head, (await asyncio.wait_for(reader.readexactly(size), timeout) if size else b"")


async def next_message(reader, timeout=3):
    head, body = await next_frame(reader, timeout)
    return g.verify_payload(g.get_publish(head, body)[1], KEY)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def config(**overrides):
    cfg = dict(module_ip="127.0.0.1", connection_mode="local", upstream_host="127.0.0.1", upstream_port=free_port(),
               listen_host="127.0.0.1", listen_port=0, api_host="127.0.0.1", api_port=0, connect_timeout=.3,
               poll_interval_seconds=60, first_poll_delay_seconds=60, cloud_retry_interval=60)
    cfg.update(overrides)
    return cfg


class ProtocolTests(unittest.TestCase):
    def test_signature_and_tamper(self):
        signed = g.sign_obj({"B_Tak1_1": 55.5, "clMsgId": 10}, KEY)
        self.assertEqual(g.verify_payload(signed, KEY)["B_Tak1_1"], 55.5)
        self.assertIsNone(g.verify_payload(signed.replace(b"55.5", b"56.5"), KEY))

    def test_fragmentation(self):
        packet = g.pub_frame("cm.inst.peltec2.X", b"{}")
        decoder = g.MQTTFrames()
        self.assertEqual(decoder.feed(packet[:2]), [])
        self.assertEqual(g.get_publish(*decoder.feed(packet[2:])[0])[1], b"{}")

    def test_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "o.json"
            path.write_text(json.dumps({"module_ip": "192.0.2.10"}))
            cfg = g.load_config(str(path))
            self.assertEqual(cfg["connection_mode"], "local")
            self.assertEqual(cfg["poll_interval_seconds"], 240)
            path.write_text(json.dumps({"connection_mode": "x"}))
            with self.assertRaises(ValueError):
                g.load_config(str(path))
            path.write_text(json.dumps({"module_ip": "192.0.2.10", "connection_mode": "cloud_proxy", "upstream_host": "localhost"}))
            with self.assertRaises(ValueError):
                g.load_config(str(path))

    def test_per_key_age_and_core_ingest(self):
        state = g.State()
        state.ingest({"B_Tak1_1": 55, "clMsgId": 1})
        state.value_times["B_Tak1_1"] -= 1000
        state.ingest({"CNT_0": 7, "clMsgId": 2})
        snap = state.snapshot()
        self.assertGreater(snap["value_age_seconds"]["B_Tak1_1"], 999)
        self.assertLess(snap["value_age_seconds"]["CNT_0"], 5)
        self.assertLess(snap["age_seconds"], 5)

    def test_real_key_with_slash_and_manual_alarm_codes_are_kept(self):
        state = g.State()
        state.ingest({"B_PTV/GRI_SEL": 0, "E8.1": "A", "W2.1": "B", "I19.1": "C", "E7X": 1, "clMsgId": 1})
        self.assertEqual(state.values["B_PTV/GRI_SEL"], 0)
        self.assertEqual(state.snapshot()["alarms"], {"E8.1": "A", "W2.1": "B", "I19.1": "C"})

    def test_settings_read_is_not_a_command(self):
        state = g.State()
        state.note_downlink({"PRD 223": "VAL", "PRD 224": "ALV", "srvMsgId": 1})
        self.assertEqual(state.last_command, "")
        state.note_downlink({"CMD": "ON", "srvMsgId": 2})
        self.assertIn("CMD", state.last_command)

    def test_values_are_capped(self):
        state = g.State()
        state.ingest({f"PVAL_1_{i}": i for i in range(800)})
        self.assertEqual(len(state.values), 750)
        self.assertEqual(len(state.value_times), 750)

    def test_journal_rotates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub", "j.jsonl")
            journal = g.Journal(path, max_bytes=200, keep=3)
            for index in range(40):
                journal.write("up", n=index)
            names = sorted(os.listdir(os.path.dirname(path)))
            self.assertIn("j.jsonl.1", names)
            self.assertNotIn("j.jsonl.3", names)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cleanup_writers = []
        self.servers = []

    async def asyncTearDown(self):
        for writer in self.cleanup_writers:
            writer.close()
        for server in self.servers:
            server.close()
        await asyncio.sleep(0)

    async def start(self, journal=None, **overrides):
        gateway = g.Gateway(config(**overrides), journal)
        server = await asyncio.start_server(gateway.handle_module, "127.0.0.1", 0)
        self.servers.append(server)
        return gateway, server.sockets[0].getsockname()[1]

    async def module(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.cleanup_writers.append(writer)
        return reader, writer

    async def handshake(self, port):
        reader, writer = await self.module(port)
        writer.write(connect() + subscribe())
        await writer.drain()
        self.assertEqual(await next_frame(reader), (0x20, b"\x00\x00"))
        self.assertEqual(await next_frame(reader), (0x90, b"\x00\x01\x00"))
        writer.write(up({"_sync": "id", "_token": "secret-token", "clMsgId": 99}))
        await writer.drain()
        ack = await next_message(reader)
        self.assertEqual((ack["_sync_ACK"], ack["clMsgId"], ack["_token"]), ("ok", 100, "secret-token"))
        return reader, writer

    async def cloud(self, port=0, handler=None):
        async def default(reader, writer):
            await reader.read(65536)
            writer.write(g.frame(0x20, b"\x00\x00"))
            await writer.drain()
            await asyncio.sleep(30)
        server = await asyncio.start_server(handler or default, "127.0.0.1", port)
        self.servers.append(server)
        return server, server.sockets[0].getsockname()[1]


class LocalTests(Base):
    async def test_refresh_then_rstat_and_nothing_else(self):
        gateway, port = await self.start(first_poll_delay_seconds=0, poll_interval_seconds=2)
        reader, _ = await self.handshake(port)
        seen = []
        while len(seen) < 4:
            message = await next_message(reader, 4)
            found = next(k for k in message if k not in ("srvMsgId", "_sign"))
            if not found.startswith("PRD"):
                seen.append(found)
        self.assertEqual(seen, ["REFRESH", "RSTAT", "REFRESH", "RSTAT"])
        self.assertEqual(gateway.state.mode, "local")
        self.assertGreaterEqual(gateway.state.status_requests_sent, 4)

    async def test_refresh_payload_matches_documented_format(self):
        _, port = await self.start(first_poll_delay_seconds=0)
        reader, _ = await self.handshake(port)
        head, body = await next_frame(reader)
        raw = g.get_publish(head, body)[1]
        self.assertTrue(raw.startswith(b'{"REFRESH":0,"srvMsgId":3,"_sign":"'), raw[:40])

    async def test_local_never_contacts_cloud(self):
        hits = []

        async def handler(reader, writer):
            hits.append(1)
            writer.close()
        _, cloud_port = await self.cloud(handler=handler)
        gateway, port = await self.start(upstream_port=cloud_port, first_poll_delay_seconds=0)
        await self.handshake(port)
        await asyncio.sleep(.5)
        self.assertEqual(hits, [])
        self.assertEqual(gateway.state.mode, "local")

    async def test_ping_alarm_ack_and_puback(self):
        gateway, port = await self.start()
        reader, writer = await self.handshake(port)
        body = mstr(b"cm.inst.peltec2.BOXID") + b"\x00\x07" + g.sign_obj({"PING": 1, "clMsgId": 102}, KEY)
        writer.write(g.frame(0x32, body))
        await writer.drain()
        self.assertEqual(await next_frame(reader), (0x40, b"\x00\x07"))
        self.assertEqual((await next_message(reader))["PING_ACK"], 1)
        writer.write(up({"E12": "A", "clMsgId": 103}) + up({"W3": "B", "clMsgId": 104}))
        await writer.drain()
        self.assertEqual((await next_message(reader))["E12_ACK"], "A")
        self.assertEqual((await next_message(reader))["W3_ACK"], "B")
        self.assertEqual(gateway.state.snapshot()["alarms"], {"E12": "A", "W3": "B"})
        writer.write(up({"Ecurie": 1, "W_nou": 2, "I19": "C", "E8.1": "D", "clMsgId": 105}))
        await writer.drain()
        self.assertEqual((await next_message(reader))["E8.1_ACK"], "D")
        with self.assertRaises(asyncio.TimeoutError):
            await next_frame(reader, .3)

    async def test_new_connection_replaces_dead_session(self):
        gateway, port = await self.start()
        old_reader, _ = await self.handshake(port)
        reader, _ = await self.handshake(port)
        self.assertEqual(await asyncio.wait_for(old_reader.read(1), 3), b"")
        self.assertTrue(gateway.state.module_connected)
        self.assertEqual(gateway.state.mode, "local")
        await asyncio.sleep(.1)
        self.assertTrue(gateway.state.module_connected, "the old session must not clear the state of the new one")

    async def test_etimedout_ends_session_without_blocking_loop(self):
        gateway = g.Gateway(config(first_poll_delay_seconds=3, poll_interval_seconds=3))
        captured = {}
        original = gateway.local

        async def spy(reader, writer, **kwargs):
            captured["reader"] = reader
            await original(reader, writer, **kwargs)
        gateway.local = spy
        server = await asyncio.start_server(gateway.handle_module, "127.0.0.1", 0)
        self.servers.append(server)
        await self.handshake(server.sockets[0].getsockname()[1])
        ticks = []

        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(.02)
        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(.1)
        captured["reader"].set_exception(TimeoutError(110, "ETIMEDOUT"))
        await asyncio.sleep(1)
        task.cancel()
        self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), .3)
        self.assertFalse(gateway.state.module_connected)

    async def test_other_ip_rejected(self):
        gateway, port = await self.start(module_ip="192.0.2.1")
        reader, writer = await self.module(port)
        writer.write(connect())
        try:
            self.assertEqual(await asyncio.wait_for(reader.read(1), 3), b"")
        except ConnectionError:
            pass  # Windows reports the unread CONNECT as a reset
        self.assertFalse(gateway.state.module_connected)

    async def test_status_api(self):
        gateway = g.Gateway(config())
        gateway.state.ingest({"B_Tak1_1": 60.0, "clMsgId": 1})
        api = await asyncio.start_server(lambda r, w: g.send_http_status(r, w, gateway), "127.0.0.1", 0)
        self.servers.append(api)
        reader, writer = await self.module(api.sockets[0].getsockname()[1])
        writer.write(b"GET /status HTTP/1.1\r\nHost: x\r\nAccept: */*\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 3)
        self.assertIn(b"200 OK", raw)
        data = json.loads(raw.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(data["values"]["B_Tak1_1"], 60.0)
        self.assertIn("B_Tak1_1", data["value_age_seconds"])


class ProxyTests(Base):
    async def test_bytes_unchanged_and_nothing_injected(self):
        received = []

        async def handler(reader, writer):
            received.append(await reader.read(65536))
            writer.write(g.frame(0x20, b"\x00\x00"))
            await writer.drain()
            while chunk := await reader.read(65536):
                received.append(chunk)
                writer.write(down({"REFRESH": 0, "srvMsgId": 25}))
                await writer.drain()
        _, cloud_port = await self.cloud(handler=handler)
        gateway, port = await self.start(connection_mode="cloud_proxy", upstream_port=cloud_port,
                                         first_poll_delay_seconds=0, poll_interval_seconds=10)
        reader, writer = await self.module(port)
        writer.write(connect())
        await writer.drain()
        self.assertEqual(await next_frame(reader), (0x20, b"\x00\x00"))
        telemetry = up({"B_Tak1_1": 61.5, "clMsgId": 6})
        writer.write(telemetry)
        await writer.drain()
        self.assertEqual((await next_message(reader))["REFRESH"], 0)
        self.assertEqual(received, [connect(), telemetry])
        self.assertEqual(gateway.state.values["B_Tak1_1"], 61.5)
        self.assertEqual(gateway.state.mode, "proxy")
        self.assertEqual(gateway.state.status_requests_sent, 0)
        with self.assertRaises(asyncio.TimeoutError):
            await next_frame(reader, .5)

    async def test_command_seen_and_secrets_not_stored(self):
        async def handler(reader, writer):
            await reader.read(65536)
            writer.write(g.frame(0x20, b"\x00\x00") + down({"CMD": "ON", "_token": "secret-token", "srvMsgId": 26}))
            await writer.drain()
            await asyncio.sleep(30)
        _, cloud_port = await self.cloud(handler=handler)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "j.jsonl")
            gateway, port = await self.start(g.Journal(path, debug=True), connection_mode="cloud_proxy", upstream_port=cloud_port)
            reader, writer = await self.module(port)
            writer.write(connect() + up({"_sync": "id", "_token": "secret-token", "clMsgId": 1}))
            await writer.drain()
            await next_frame(reader)
            await next_frame(reader)
            await asyncio.sleep(.1)
            self.assertIn('"CMD": "ON"', gateway.state.last_command)
            self.assertIn("CMD", gateway.state.downlink_keys)
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn('"CMD": "ON"', text)
            for secret in ("secret-token", PASSWORD.decode(), "BOXID", "_sign"):
                self.assertNotIn(secret, text)
            self.assertNotIn("secret-token", gateway.state.last_command)
            writer.close()
            await asyncio.sleep(.05)

    async def test_plain_proxy_closes_when_cloud_is_down(self):
        gateway, port = await self.start(connection_mode="cloud_proxy")
        reader, writer = await self.module(port)
        writer.write(connect())
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(1), 3), b"")
        self.assertEqual(gateway.state.mode, "idle")
        self.assertEqual(gateway.state.status_requests_sent, 0)

    async def test_plain_proxy_closes_when_cloud_is_silent(self):
        async def silent(reader, writer):
            await asyncio.sleep(30)
        _, cloud_port = await self.cloud(handler=silent)
        _, port = await self.start(connection_mode="cloud_proxy", upstream_port=cloud_port)
        reader, writer = await self.module(port)
        writer.write(connect())
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(1), 3), b"")




    async def test_fallback_when_cloud_is_down_then_returns(self):
        cloud_port = free_port()
        gateway, port = await self.start(connection_mode="cloud_proxy_fallback", upstream_port=cloud_port,
                                         cloud_retry_interval=.3)
        reader, _ = await self.handshake(port)
        self.assertEqual(gateway.state.mode, "local")
        await self.cloud(port=cloud_port)
        self.assertEqual(await asyncio.wait_for(reader.read(1), 4), b"")
        reader2, writer2 = await self.module(port)
        writer2.write(connect())
        await writer2.drain()
        self.assertEqual(await next_frame(reader2), (0x20, b"\x00\x00"))
        self.assertEqual(gateway.state.mode, "proxy")


    async def test_fallback_when_cloud_is_silent(self):
        async def silent(reader, writer):
            await asyncio.sleep(30)
        _, cloud_port = await self.cloud(handler=silent)
        gateway, port = await self.start(connection_mode="cloud_proxy_fallback", upstream_port=cloud_port)
        await self.handshake(port)
        self.assertEqual(gateway.state.mode, "local")


    async def test_fallback_when_connack_refused(self):
        async def refuse(reader, writer):
            await reader.read(65536)
            writer.write(g.frame(0x20, b"\x00\x05"))
            await writer.drain()
            await asyncio.sleep(30)
        _, cloud_port = await self.cloud(handler=refuse)
        gateway, port = await self.start(connection_mode="cloud_proxy_fallback", upstream_port=cloud_port)
        await self.handshake(port)
        self.assertEqual(gateway.state.mode, "local")

    async def test_dead_peer_detection_is_set_on_both_sockets(self):
        _, cloud_port = await self.cloud()
        gateway, port = await self.start(connection_mode="cloud_proxy", upstream_port=cloud_port)
        seen = []
        original = g.tune_socket

        def spy(writer):
            original(writer)
            seen.append(writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))
        g.tune_socket = spy
        try:
            reader, writer = await self.module(port)
            writer.write(connect())
            await writer.drain()
            await next_frame(reader)
        finally:
            g.tune_socket = original
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(seen))


class CaptureTests(Base):
    def read_capture(self):
        raw = PCAP.read_bytes()
        ptr, seen, ups, downs = 24, set(), [], []
        while ptr + 16 <= len(raw):
            _, _, size, _ = struct.unpack_from("<IIII", raw, ptr)
            ptr += 16
            data = raw[ptr:ptr + size]
            ptr += size
            if data[:2] != b"\x08\x00" or data[20:][9] != 6:
                continue
            ip = data[20:]
            tcp = ip[4 * (ip[0] & 15):int.from_bytes(ip[2:4], "big")]
            source, target, seq = struct.unpack("!HHI", tcp[:8])
            payload = tcp[4 * (tcp[12] >> 4):]
            if payload and 63060 in (source, target) and (source, target, seq, payload) not in seen:
                seen.add((source, target, seq, payload))
                (ups if target == 1883 else downs).append(payload)
        return ups, downs

    async def test_local_broker_answers_like_the_real_cloud(self):
        if not PCAP.exists():
            self.skipTest("Private capture not available")
        ups, downs = self.read_capture()
        _, port = await self.start()
        reader, writer = await self.module(port)
        answers = []
        for packet in ups:
            writer.write(packet)
            await writer.drain()
            head, body = await next_frame(reader)
            answers.append(g.frame(head, body))
        self.assertEqual(answers[0], downs[0], "CONNACK")
        self.assertEqual(answers[1], downs[1], "SUBACK")
        real_topic, real = g.get_publish(*g.MQTTFrames().feed(downs[2])[0])
        mine_topic, mine = g.get_publish(*g.MQTTFrames().feed(answers[2])[0])
        self.assertEqual(real_topic, mine_topic)
        key = g.signing_key(g.read_connect_password(g.MQTTFrames().feed(ups[0])[0][1]))
        real_obj, mine_obj = json.loads(real), json.loads(mine)
        self.assertEqual(list(real_obj), list(mine_obj))
        rebuilt = {k: real_obj["srvMsgId"] if k == "srvMsgId" else v for k, v in mine_obj.items() if k != "_sign"}
        self.assertEqual(g.sign_obj(rebuilt, key), real)

    async def test_proxy_passes_real_session_unchanged(self):
        if not PCAP.exists():
            self.skipTest("Private capture not available")
        ups, downs = self.read_capture()
        received = bytearray()

        async def replay(reader, writer):
            for index, _ in enumerate(ups):
                received.extend(await reader.read(65536))
                writer.write(downs[index])
                await writer.drain()
            await asyncio.sleep(30)
        _, cloud_port = await self.cloud(handler=replay)
        gateway, port = await self.start(connection_mode="cloud_proxy", upstream_port=cloud_port)
        reader, writer = await self.module(port)
        got = bytearray()
        for index, packet in enumerate(ups):
            writer.write(packet)
            await writer.drain()
            got.extend(g.frame(*(await next_frame(reader))))
        self.assertEqual(bytes(received), b"".join(ups))
        self.assertEqual(bytes(got), b"".join(downs[:len(ups)]))
        self.assertEqual(gateway.state.invalid_messages, 0)
        self.assertEqual(gateway.state.verified_messages, 1)
        self.assertEqual(gateway.state.downlink_keys, {"_sync_ACK", "clMsgId"})


class ParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "custom_components/centrometal_local"))
        import parameters
        cls.p = parameters

    def test_display_values_match_the_cloud_integration(self):
        show = self.p.display_value
        self.assertEqual(show("B_Tak1_1", 60), 60.0)
        self.assertEqual(show("B_STATE", "D3"), "D3: Power D3")
        self.assertEqual(show("B_STATE", "OFF"), "OFF: Switched off")
        self.assertEqual(show("B_STATE", "XX"), "Unknown (XX)")
        self.assertEqual(show("B_start", 3), "Cleaning")
        self.assertEqual(show("B_fireS", 0), "Off")
        self.assertEqual(show("K1B_zahP", 1), "On")
        self.assertEqual(show("K1B_P", 0), "Off")
        self.assertEqual(show("B_razina", 2), "Full")
        self.assertEqual(show("B_specG", 0), "None")
        self.assertEqual(show("B_SUP_TYPE", 2), "Supervision + control")
        self.assertEqual(show("B_KONF", 42), "43. BUF + K1 DHW")
        self.assertEqual(show("B_sng", "36"), 36.0)
        self.assertEqual(show("B_Time", "6AED0FA0"), "2026-11-05 23:22:08")
        self.assertIsNone(show("B_Time", "nu"))

    def test_stage_groups(self):
        groups = {"S7-3": "standby", "D4": "modulation", "SP2": "ignition_startup", "OFF": "off", "S7-1": "shutdown",
                  "C0": "cleaning", "PF0": "power_failure_recovery", "S0": "ignition_startup"}
        for code, group in groups.items():
            self.assertEqual(self.p.stage_attributes(code)["Stage group"], group, code)

    def test_missing_sensor_values_are_filtered(self):
        self.assertIsNone(self.p.display_value("B_Ths1", -55))
        self.assertIsNone(self.p.display_value("B_Tdpl1", 0))
        self.assertEqual(self.p.display_value("B_Tdpl1", 180), 180.0)

    def test_freshness(self):
        self.assertTrue(self.p.is_fresh(241))
        self.assertFalse(self.p.is_fresh(481))
        self.assertFalse(self.p.is_fresh(None))

    def test_every_pump_is_a_pump(self):
        for number_ in (2, 3, 4, 5, 9):
            name, kind, _, _, entity_id = self.p.parameter_info(f"B_P{number_}")
            self.assertTrue(name.startswith(f"P{number_} ") and name.endswith("Pump"), name)
            self.assertEqual(kind, "map")
            self.assertEqual(entity_id, f"sensor.peltec_ii_lambda_p{number_}_pump")
        self.assertEqual(self.p.parameter_info("K1B_P")[0], "P1 DHW Pump")

    def test_unknown_parameter_gets_a_raw_diagnostic_sensor(self):
        name, kind, _, diagnostic, entity_id = self.p.parameter_info("B_nou/2")
        self.assertEqual((name, kind, diagnostic, entity_id), ("B_nou/2", "raw", True, "sensor.peltec_ii_lambda_b_nou_2"))
        self.assertEqual(self.p.display_value("B_nou/2", 7), "7")

    def test_protocol_and_tables_are_not_entities(self):
        for key in ("PING", "CMD", "wf_req", "PVAL_67_0", "PMIN_1", "E12", "W2.1", "I19.1"):
            self.assertFalse(self.p.is_entity(key), key)
        for key in ("SE00", "CMD_TIME", "B_dop"):
            self.assertTrue(self.p.is_entity(key), key)
            self.assertFalse(self.p.enabled_by_default(key), key)
        self.assertTrue(self.p.enabled_by_default("B_Tak1_1"))

    def test_duplicates_and_missing_hardware_have_no_entity(self):
        for key in ("B_P1", "B_Pk1_k2", "B_zahK1_K2", "B_Tkm1", "B_CMD", "K1B_onOff", "K1B_Prec", "K1B_Tsob", "K1B_Tsob1", "K1B_kor", "K1B_korN", "K1B_dayNight", "K1B_korType"):
            self.assertFalse(self.p.is_entity(key), key)
        self.assertEqual(self.p.parameter_info("K1B_Tpol1")[0], "DHW Temperature")
        self.assertEqual(self.p.parameter_info("K1B_Tpol")[0], "DHW Target Temperature")

    def test_ids_and_names_are_unique_and_english(self):
        info = [self.p.parameter_info(k) for k in self.p.PARAMETERS]
        self.assertEqual(len({x[0] for x in info}), len(info))
        self.assertEqual(len({x[4] for x in info}), len(info))
        for name, *_ in info:
            self.assertTrue(name.isascii(), name)

    def test_attributes_the_logic_reads(self):
        values = {"B_STATE": "S7-3", "B_start": 0, "B_fireS": 1, "B_Oxy1": 18.1, "B_specG": 4, "B_KONF": 42}
        self.assertEqual(self.p.extra_attributes("B_STATE", values)["Raw stage code"], "S7-3")
        self.assertEqual(self.p.extra_attributes("B_start", values)["Boiler state"], "S7-3")
        self.assertTrue(self.p.extra_attributes("B_Oxy1", values)["Flame detected"])
        self.assertIn("grate", self.p.extra_attributes("B_specG", values)["Meaning"])
        self.assertEqual(self.p.extra_attributes("B_KONF", values)["P2"], "DHW Recirculation Pump (K1)")


class CommandTests(Base):
    async def api(self, gateway, raw):
        server = await asyncio.start_server(lambda r, w: g.send_http_status(r, w, gateway), "127.0.0.1", 0)
        self.servers.append(server)
        reader, writer = await self.module(server.sockets[0].getsockname()[1])
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 3)

    def post(self, body):
        return b"POST /command HTTP/1.1\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)

    async def test_command_reaches_the_module_signed_then_refresh(self):
        gateway, port = await self.start()
        reader, _ = await self.handshake(port)
        self.assertIn(b"200 OK", await self.api(gateway, self.post(b'{"CMD":1}')))
        head, body = await next_frame(reader)
        raw = g.get_publish(head, body)[1]
        self.assertTrue(raw.startswith(b'{"CMD":1,"srvMsgId":3,"_sign":"'), raw[:40])
        self.assertIsNotNone(g.verify_payload(raw, KEY))
        self.assertIn("REFRESH", await next_message(reader))
        self.assertIn('"CMD": 1', gateway.state.last_command)

    async def test_only_0_and_1_are_accepted(self):
        gateway, port = await self.start()
        await self.handshake(port)
        for body in (b'{"CMD":2}', b'{"CMD":"1"}', b'{"CMD":true}', b'{"PWR 1":1}', b'[]', b'nu'):
            self.assertFalse(b"200 OK" in await self.api(gateway, self.post(body)), body)

    async def test_refused_without_a_synchronised_session(self):
        gateway, _ = await self.start()
        self.assertIn(b"409", await self.api(gateway, self.post(b'{"CMD":1}')))

    async def test_refused_in_proxy_mode(self):
        _, cloud_port = await self.cloud()
        gateway, port = await self.start(connection_mode="cloud_proxy", upstream_port=cloud_port)
        reader, writer = await self.module(port)
        writer.write(connect())
        await writer.drain()
        await next_frame(reader)
        self.assertIn(b"409", await self.api(gateway, self.post(b'{"CMD":1}')))

    async def test_refused_after_the_session_ends(self):
        gateway, port = await self.start()
        _, writer = await self.handshake(port)
        writer.close()
        await asyncio.sleep(.1)
        self.assertIn(b"409", await self.api(gateway, self.post(b'{"CMD":1}')))


class SettingTests(CommandTests):
    def post_setting(self, body):
        return b"POST /setting HTTP/1.1\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)

    async def test_settings_are_read_after_sync_one_at_a_time(self):
        gateway, port = await self.start(first_poll_delay_seconds=60)
        reader, _ = await self.handshake(port)
        seen = []
        for _ in range(len(g.SETTINGS)):
            seen.append(next(k for k in await next_message(reader, 6) if k.startswith("PRD")))
        self.assertEqual(sorted(seen), sorted(f"PRD {slot}" for slot in g.SETTINGS))

    async def test_setting_is_written_signed_and_read_back(self):
        gateway, port = await self.start(first_poll_delay_seconds=60)
        reader, _ = await self.handshake(port)
        self.assertIn(b"200 OK", await self.api(gateway, self.post_setting(b'{"slot":67,"value":66.0}')))
        raw = g.get_publish(*(await next_frame(reader)))[1]
        self.assertTrue(raw.startswith(b'{"PWR 67":66,"srvMsgId":'), raw[:40])
        self.assertEqual((await next_message(reader, 6)).get("PRD 67"), "VAL")

    async def test_settings_outside_limits_or_unknown_are_refused(self):
        gateway, port = await self.start(first_poll_delay_seconds=60)
        await self.handshake(port)
        for body in (b'{"slot":67,"value":86}', b'{"slot":67,"value":39}', b'{"slot":1,"value":50}', b'{"slot":67,"value":"66"}',
                     b'{"slot":67,"value":true}', b'{"slot":"67","value":66}', b'{}'):
            self.assertFalse(b"200 OK" in await self.api(gateway, self.post_setting(body)), body)

    async def test_k1_circuit_is_written_as_0_or_1(self):
        gateway, port = await self.start(first_poll_delay_seconds=60)
        reader, _ = await self.handshake(port)
        self.assertIn(b"200 OK", await self.api(gateway, self.post_setting(b'{"slot":272,"value":0}')))
        raw = g.get_publish(*(await next_frame(reader)))[1]
        self.assertTrue(raw.startswith(b'{"PWR 272":0,"srvMsgId":'), raw[:40])
        self.assertFalse(b"200 OK" in await self.api(gateway, self.post_setting(b'{"slot":272,"value":2}')))

    async def test_refused_without_a_session(self):
        gateway, _ = await self.start()
        self.assertIn(b"409", await self.api(gateway, self.post_setting(b'{"slot":67,"value":66}')))


class MalformedTests(Base):
    async def test_cut_frame_does_not_end_the_local_session(self):
        gateway, port = await self.start(first_poll_delay_seconds=60)
        reader, writer = await self.handshake(port)
        writer.write(bytes([0x30]) + b"\xff\xff\xff\xff\xff" + b"x" * 8)
        await writer.drain()
        await asyncio.sleep(.1)
        self.assertEqual(gateway.state.malformed_frames, 1)
        self.assertTrue(gateway.state.module_connected)
        writer.write(up({"PING": 7, "clMsgId": 200}))
        await writer.drain()
        for _ in range(12):
            message = await next_message(reader, 4)
            if "PING_ACK" in message:
                break
        self.assertEqual(message["PING_ACK"], 7)

    async def test_publish_before_connect_is_ignored(self):
        gateway, port = await self.start(first_poll_delay_seconds=60)
        reader, writer = await self.module(port)
        writer.write(up({"CNT_0": 1, "clMsgId": 1}) + connect() + subscribe())
        await writer.drain()
        self.assertEqual(await next_frame(reader), (0x20, b"\x00\x00"))
        self.assertEqual(gateway.state.values, {})

    async def test_data_stays_fresh_during_a_short_reconnect(self):
        state = g.State()
        state.ingest({"B_Tak1_1": 60, "clMsgId": 1})
        state.module_connected = False
        self.assertTrue(state.snapshot()["data_fresh"])


class StopTests(Base):
    async def test_stop_request_ends_the_app_and_the_session(self):
        port = free_port()
        api_port = free_port()
        gateway = g.Gateway(config(listen_port=port, api_port=api_port))
        task = asyncio.create_task(gateway.run())
        await asyncio.sleep(.1)
        reader, writer = await self.module(port)
        writer.write(connect())
        await writer.drain()
        await next_frame(reader)
        gateway.request_stop()
        await asyncio.wait_for(task, 4)
        self.assertEqual(await asyncio.wait_for(reader.read(1), 3), b"")
        self.assertFalse(gateway.state.module_connected)


class InstallTests(unittest.TestCase):
    def test_bundled_integration_matches_the_source(self):
        source = ROOT / "custom_components/centrometal_local"
        bundled = ROOT / "centrometal_local_mqtt/integration/centrometal_local"
        names = sorted(p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        self.assertEqual(names, sorted(p.relative_to(bundled).as_posix() for p in bundled.rglob("*") if p.is_file() and "__pycache__" not in p.parts))
        for name in names:
            self.assertEqual((source / name).read_bytes(), (bundled / name).read_bytes(), name)

    def test_install_copies_once_and_keeps_going_without_config(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "src"
            (source / "brand").mkdir(parents=True)
            (source / "a.py").write_text("1")
            (source / "brand" / "i.png").write_text("2")
            config = Path(folder) / "config"
            (config / "custom_components").mkdir(parents=True)
            target = config / "custom_components" / "x"
            self.assertTrue(g.install_integration(str(source), str(target)))
            self.assertEqual((target / "brand" / "i.png").read_text(), "2")
            self.assertFalse(g.install_integration(str(source), str(target)))
            (source / "a.py").write_text("3")
            self.assertTrue(g.install_integration(str(source), str(target)))
            self.assertFalse(g.install_integration(str(source), str(Path(folder) / "nu" / "exista" / "x")))


class ApiSecurityTests(Base):
    async def call(self, gateway, raw):
        server = await asyncio.start_server(lambda r, w: g.send_http_status(r, w, gateway), "127.0.0.1", 0)
        self.servers.append(server)
        reader, writer = await self.module(server.sockets[0].getsockname()[1])
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 3)

    async def test_token_is_required_except_for_health(self):
        gateway = g.Gateway(config(api_token="sigur"))
        self.assertIn(b"401", await self.call(gateway, b"GET /status HTTP/1.1\r\n\r\n"))
        self.assertIn(b"401", await self.call(gateway, b"GET /status HTTP/1.1\r\nAuthorization: Bearer gresit\r\n\r\n"))
        self.assertIn(b"200 OK", await self.call(gateway, b"GET /status HTTP/1.1\r\nAuthorization: Bearer sigur\r\n\r\n"))
        self.assertIn(b"200 OK", await self.call(gateway, b"GET /health HTTP/1.1\r\n\r\n"))

    async def test_command_needs_the_token(self):
        gateway, port = await self.start(api_token="sigur")
        await self.handshake(port)
        body = b'{"CMD":1}'
        raw = b"POST /command HTTP/1.1\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)
        self.assertIn(b"401", await self.call(gateway, raw))
        self.assertEqual(gateway.state.last_command, "")

    def test_token_is_kept_across_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "token")
            first = g.api_token(path)
            self.assertGreater(len(first), 30)
            self.assertEqual(g.api_token(path), first)

    def test_connection_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "gateway.json")
            g.write_connection_file(8765, "sigur", "172.30.33.5", target)
            self.assertEqual(json.loads(Path(target).read_text()), {"host": "172.30.33.5", "port": 8765, "token": "sigur"})
            g.write_connection_file(8765, "x", "1.1.1.1", os.path.join(folder, "nu", "exista.json"))

    async def test_rejected_address_is_shown(self):
        gateway, port = await self.start(module_ip="192.0.2.1")
        reader, writer = await self.module(port)
        writer.write(connect())
        try:
            await asyncio.wait_for(reader.read(1), 3)
        except ConnectionError:
            pass
        self.assertEqual(gateway.state.snapshot()["rejected_ip"], "127.0.0.1")


class JournalModeTests(Base):
    async def test_normal_mode_keeps_only_new_keys_and_events(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "j.jsonl")
            gateway, port = await self.start(g.Journal(path))
            reader, writer = await self.handshake(port)
            writer.write(up({"B_Tak1_1": 60, "clMsgId": 1}) + up({"B_Tak1_1": 61, "clMsgId": 2}))
            await writer.drain()
            await asyncio.sleep(.1)
            lines = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([x["key"] for x in lines if x["e"] == "new_key"], ["B_Tak1_1"])
            self.assertFalse([x for x in lines if x["e"] in ("up", "down", "local_down")])
            writer.close()
            await asyncio.sleep(.05)


if __name__ == "__main__":
    unittest.main()
