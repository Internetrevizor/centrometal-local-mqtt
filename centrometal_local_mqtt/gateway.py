"""Centrometal Local MQTT Protocol: local server for the WiFi module of a PelTec II Lambda. Python standard library only.

connection_mode:
  local        the module talks only to this server; the cloud is never contacted.
  cloud_proxy  diagnostics: transparent TCP proxy to the portal; sends nothing of its own to the module.

In local mode the server sends protocol acknowledgements, the read requests REFRESH, RSTAT and PRD, and, when asked
through the local API, CMD 0/1 and PWR for the settings listed in SETTINGS, within their limits. Nothing else can be emitted.
Never logs or stores the MQTT password, _token, _sign or the installation ID.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import time
from http import HTTPStatus
from typing import Any

LOG = logging.getLogger("peltec-gateway")
SEED = b"158208e84665e0158208e84665e065e0158208e8"  # public protocol constant
MAX_PACKET = 1_048_576
MAX_BUFFER = MAX_PACKET + 5
FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ /.]{0,79}$")  # real key: B_PTV/GRI; manual codes: E8.1, W2.1
COMMAND_RE = re.compile(r"^(CMD|PWR|SCCMD)\b")  # PRD only reads a settings table
ALARM_RE = re.compile(r"^[EWI]\d+(\.\d+)?$")  # controller manual, chapter 6: errors, warnings, information
ACK_RE = re.compile(r"^[EW]\d+(\.\d+)?$")  # scr34m acknowledges errors and warnings
LOCAL_REQUESTS = {"REFRESH", "RSTAT"}
MODES = ("local", "cloud_proxy")
JOURNAL_PATH = "/share/centrometal_local_mqtt/journal.jsonl"
DATA_FRESH_SECONDS = 480
SETTINGS_READ_SECONDS = 3600
# Writable slots of a PelTec II Lambda, with the controller's own limits (min, max), read from the portal 06.11.2026.
SETTINGS = {67: (40, 85), 68: (5, 40), 70: (5, 51), 71: (3, 30), 272: (0, 1), 291: (40, 85), 292: (4, 40), 576: (5, 10)}
JOURNAL_MAX_BYTES = 10_000_000
JOURNAL_KEEP = 3
RSTAT_DELAY = 1.0  # same spacing as the integration: REFRESH, 1 s, RSTAT
CONNECT_READ_TIMEOUT = 30


def signing_key(password: bytes) -> bytes:
    """Matches scr34m's JSON signing key derivation for passwords <= 40 bytes."""
    if not password or len(password) > len(SEED):
        raise ValueError("Unsupported MQTT password length")
    return password + SEED[len(password):]


def canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_obj(obj: dict[str, Any], key: bytes) -> bytes:
    if "_sign" in obj:
        raise ValueError("_sign already set")
    raw = canonical(obj)
    data = dict(obj)
    data["_sign"] = hmac.new(key, raw, hashlib.sha1).hexdigest()
    return canonical(data)


def verify_payload(payload: bytes, key: bytes) -> dict[str, Any] | None:
    """HMAC covers exact serialized JSON before terminal `_sign` field."""
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or not isinstance(data.get("_sign"), str):
            return None
        cut = payload.rfind(b',"_sign"')
        if cut < 1 or not payload.endswith(b"}"):
            return None
        unsigned = payload[:cut] + b"}"
        digest = hmac.new(key, unsigned, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(digest, data["_sign"]):
            return None
        return data
    except (ValueError, UnicodeError, TypeError):
        return None


def varint(number: int) -> bytes:
    if not 0 <= number <= 268435455:
        raise ValueError("MQTT remaining length outside protocol range")
    out = bytearray()
    while True:
        digit = number % 128
        number //= 128
        out.append(digit | (128 if number else 0))
        if not number:
            return bytes(out)


def frame(header: int, payload: bytes = b"") -> bytes:
    return bytes([header]) + varint(len(payload)) + payload


def extract_frame(buffer: bytearray) -> tuple[int, bytes] | None:
    if len(buffer) < 2:
        return None
    length, shift = 0, 0
    for idx in range(1, min(len(buffer), 5)):
        digit = buffer[idx]
        length |= (digit & 127) << shift
        if not digit & 128:
            if length > MAX_PACKET:
                raise ValueError("MQTT packet too large")
            end = idx + 1 + length
            if end > len(buffer):
                return None
            head, body = buffer[0], bytes(buffer[idx + 1:end])
            del buffer[:end]
            return head, body
        shift += 7
    if len(buffer) >= 5:
        raise ValueError("Malformed MQTT remaining length")
    return None


def unpack_utf8(buf: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(buf):
        raise ValueError("Truncated MQTT string")
    n = struct.unpack_from("!H", buf, offset)[0]
    end = offset + 2 + n
    if end > len(buf):
        raise ValueError("Truncated MQTT string bytes")
    return buf[offset + 2:end], end


def read_connect_password(body: bytes) -> bytes:
    protocol, ptr = unpack_utf8(body, 0)
    if protocol != b"MQTT" or len(body) < ptr + 4 or body[ptr] != 4:
        raise ValueError("Only MQTT 3.1.1 supported")
    flags = body[ptr + 1]
    if flags & 1:
        raise ValueError("Invalid MQTT flags")
    ptr += 4  # version, flags, keepalive
    _, ptr = unpack_utf8(body, ptr)  # client identifier
    if flags & 4:
        _, ptr = unpack_utf8(body, ptr)  # will topic
        _, ptr = unpack_utf8(body, ptr)  # will payload
    if flags & 128:
        _, ptr = unpack_utf8(body, ptr)  # username
    if not flags & 64:
        raise ValueError("MQTT password required")
    password, ptr = unpack_utf8(body, ptr)
    if ptr != len(body):
        raise ValueError("Unexpected trailing MQTT connect data")
    return password


def get_publish(head: int, body: bytes) -> tuple[str, bytes]:
    topic, off = unpack_utf8(body, 0)
    qos = (head >> 1) & 3
    if qos == 3:
        raise ValueError("Invalid PUBLISH QoS")
    if qos:
        if off + 2 > len(body):
            raise ValueError("Truncated publish packet ID")
        off += 2
    return topic.decode("utf-8"), body[off:]


def subscribe_packet(body: bytes) -> tuple[int, list[str]]:
    if len(body) < 2:
        raise ValueError("Truncated SUBSCRIBE")
    packet_id = struct.unpack_from("!H", body, 0)[0]
    ptr = 2
    topics = []
    while ptr < len(body):
        name, ptr = unpack_utf8(body, ptr)
        if ptr >= len(body):
            raise ValueError("Truncated SUBSCRIBE QoS")
        ptr += 1
        topics.append(name.decode("utf-8"))
    if not topics:
        raise ValueError("Empty SUBSCRIBE")
    return packet_id, topics


def pub_frame(topic: str, payload: bytes) -> bytes:
    tb = topic.encode("utf-8")
    if len(tb) > 65535:
        raise ValueError("MQTT topic too long")
    return frame(0x30, len(tb).to_bytes(2, "big") + tb + payload)


def tune_socket(writer: asyncio.StreamWriter) -> None:
    """Notice a dead peer in about a minute: the WiFi module MQTT keepalive is 3600 s, far too long to rely on."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3), ("TCP_USER_TIMEOUT", 60_000)):
        option = getattr(socket, name, None)
        if option is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, option, value)


def redact(message: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in message.items() if k not in ("_sign", "_token")}


class MQTTFrames:
    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buf.extend(data)
        if len(self.buf) > MAX_BUFFER:
            raise ValueError("MQTT buffer limit exceeded")
        result = []
        while (item := extract_frame(self.buf)) is not None:
            result.append(item)
        return result


class Journal:
    """Rotating JSON lines, redacted. A failure here must never touch the MQTT traffic."""

    def __init__(self, path: str | None, max_bytes: int = JOURNAL_MAX_BYTES, keep: int = JOURNAL_KEEP,
                 debug: bool = False) -> None:
        self.path = path
        self.debug = debug
        self.max_bytes = max_bytes
        self.keep = keep
        self._warned = False

    def message(self, event: str, **fields: Any) -> None:
        """Every MQTT message: only with debug_journal, to spare the disk in normal operation."""
        if self.debug:
            self.write(event, **fields)

    def write(self, event: str, **fields: Any) -> None:
        if not self.path:
            return
        line = {"t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "e": event, **fields}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            if os.path.getsize(self.path) > self.max_bytes:
                for index in range(self.keep - 1, 0, -1):
                    older = f"{self.path}.{index}"
                    if os.path.exists(older):
                        os.replace(older, f"{self.path}.{index + 1}")
                os.replace(self.path, f"{self.path}.1")
                with contextlib.suppress(OSError):
                    os.remove(f"{self.path}.{self.keep}")
        except OSError as exc:
            if not self._warned:
                self._warned = True
                LOG.warning("Journal unavailable: %s", type(exc).__name__)


class State:
    def __init__(self, journal: "Journal | None" = None) -> None:
        self.journal = journal
        self.seen_keys: set[str] = set()
        self.mode = "idle"
        self.module_connected = False
        self.upstream_connected = False
        self.values: dict[str, Any] = {}
        self.value_times: dict[str, float] = {}
        self.last_update_unix: float | None = None
        self.verified_messages = 0
        self.invalid_messages = 0
        self.malformed_frames = 0
        self.status_requests_sent = 0
        self.last_status_request_unix: float | None = None
        self.downlink_messages = 0
        self.downlink_keys: set[str] = set()
        self.last_downlink = ""
        self.last_command = ""
        self.last_session_end = ""

    def ingest(self, signed: dict[str, Any]) -> None:
        self.verified_messages += 1
        telemetry = {
            k: v for k, v in signed.items()
            if FIELD_RE.fullmatch(k) and not k.endswith("_ACK")
            and k not in ("clMsgId", "srvMsgId", "PING")
            and isinstance(v, (str, int, float, bool))
            and (not isinstance(v, str) or len(v) <= 160)
        }
        if telemetry:
            now = time.time()
            for key in telemetry.keys() - self.seen_keys:
                self.seen_keys.add(key)
                if self.journal:
                    self.journal.write("new_key", key=key, value=telemetry[key])
            self.values.update(telemetry)
            self.value_times.update(dict.fromkeys(telemetry, now))
            if len(self.values) > 750:  # settings tables can be large
                for key in sorted(self.value_times, key=self.value_times.get)[:len(self.values) - 750]:
                    self.values.pop(key, None)
                    self.value_times.pop(key, None)
            self.last_update_unix = now

    def note_downlink(self, message: dict[str, Any]) -> None:
        clean = redact(message)
        text = json.dumps(clean, ensure_ascii=False)[:240]
        self.downlink_messages += 1
        self.last_downlink = text
        if len(self.downlink_keys) < 40:
            self.downlink_keys.update(k for k in clean if k != "srvMsgId")
        if any(COMMAND_RE.match(k) for k in clean):
            self.last_command = text

    def snapshot(self) -> dict[str, Any]:
        t = self.last_update_unix
        now = time.time()
        stamp = lambda v: datetime.fromtimestamp(v, timezone.utc).isoformat() if v else None  # noqa: E731
        return {
            "mode": self.mode,
            "module_connected": self.module_connected,
            "upstream_connected": self.upstream_connected,
            "last_update": stamp(t),
            "age_seconds": round(now - t, 1) if t else None,
            "verified_messages": self.verified_messages,
            "invalid_messages": self.invalid_messages,
            "status_requests_sent": self.status_requests_sent,
            "last_status_request": stamp(self.last_status_request_unix),
            "downlink_messages": self.downlink_messages,
            "downlink_keys": sorted(self.downlink_keys),
            "last_downlink": self.last_downlink,
            "last_command": self.last_command,
            "last_session_end": self.last_session_end,
            "data_fresh": bool(t and now - t < DATA_FRESH_SECONDS),  # a reconnect of a few seconds is not an outage
            "malformed_frames": self.malformed_frames,
            "alarms": {k: v for k, v in self.values.items() if ALARM_RE.match(k)},
            "values": self.values.copy(),
            "value_age_seconds": {k: round(now - v, 1) for k, v in self.value_times.items()},
        }


class Session:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.done = asyncio.Event()
        self.key: bytes | None = None


class MQTTObserver:
    """Passive copy of one direction of the proxied stream. Whatever happens here, the bytes still flow."""

    def __init__(self, state: State, journal: Journal, session: Session, module_to_cloud: bool) -> None:
        self.state = state
        self.journal = journal
        self.session = session
        self.module_to_cloud = module_to_cloud
        self.frames = MQTTFrames()

    def feed(self, chunk: bytes) -> None:
        try:
            for header, body in self.frames.feed(chunk):
                kind = header >> 4
                if kind == 1 and self.module_to_cloud:
                    self.session.key = signing_key(read_connect_password(body))
                elif kind == 3:
                    self._publish(header, body)
        except Exception as exc:  # pylint: disable=broad-except
            self.frames = MQTTFrames()
            LOG.warning("Cannot decode a monitoring frame, forwarding unaffected: %s", type(exc).__name__)

    def _publish(self, header: int, body: bytes) -> None:
        topic, payload = get_publish(header, body)
        expected = "cm.inst.peltec2." if self.module_to_cloud else ("cm/srv/peltec2/", "cm.srv.peltec2.")
        if not topic.startswith(expected):
            self.journal.message("other_topic", up=self.module_to_cloud)
            return
        key = self.session.key
        verified = verify_payload(payload, key) if key else None
        try:
            message = json.loads(payload)
        except ValueError:
            self.journal.message("non_json", up=self.module_to_cloud, length=len(payload))
            return
        if not isinstance(message, dict):
            return
        self.journal.message("up" if self.module_to_cloud else "down", valid=verified is not None, msg=redact(message))
        if self.module_to_cloud:
            if verified is None:
                self.state.invalid_messages += 1
            else:
                self.state.ingest(verified)
        else:
            self.state.note_downlink(message)


class CloudUnavailable(Exception):
    pass


async def send_http_status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, gateway: "Gateway") -> None:
    try:
        async with asyncio.timeout(3):
            line = await reader.readline()
            if len(line) > 4096:
                raise ValueError("Request too long")
            method, path, _ = line.decode("ascii").split(" ", 2)
            length = 0
            while (header := await reader.readline()) not in (b"\r\n", b"\n", b""):
                if header.lower().startswith(b"content-length:"):
                    length = int(header.split(b":", 1)[1])
            if length > 64:
                raise ValueError("Body too long")
            body = await reader.readexactly(length) if length else b""
        status, data = HTTPStatus.NOT_FOUND, b"{}"
        if method == "GET" and path == "/status":
            status, data = HTTPStatus.OK, json.dumps(gateway.state.snapshot()).encode()
        elif method == "GET" and path == "/health":
            status, data = HTTPStatus.OK, b'{"status":"ok"}'
        elif method == "POST" and path == "/command":
            value = json.loads(body).get("CMD") if body else None
            accepted = gateway.command(value)
            status = HTTPStatus.OK if accepted else HTTPStatus.CONFLICT
            data = json.dumps({"accepted": accepted}).encode()
        elif method == "POST" and path == "/setting":
            request = json.loads(body) if body else {}
            accepted = gateway.setting(request.get("slot"), request.get("value"))
            status = HTTPStatus.OK if accepted else HTTPStatus.CONFLICT
            data = json.dumps({"accepted": accepted}).encode()
        writer.write((f"HTTP/1.1 {status.value} {status.phrase}\r\n"
                      "Content-Type: application/json\r\nCache-Control: no-store\r\n"
                      f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
        await writer.drain()
    except (ValueError, UnicodeError, OSError, AttributeError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, obs: MQTTObserver) -> None:
    while chunk := await src.read(65536):
        obs.feed(chunk)
        dst.write(chunk)  # UNMODIFIED; no local injection in proxy mode
        await dst.drain()


class Gateway:
    def __init__(self, config: dict[str, Any], journal: Journal | None = None) -> None:
        self.config = config
        self.journal = journal or Journal(None)
        self.state = State(self.journal)
        self._current: Session | None = None
        self._command = None
        self._setting = None

    def setting(self, slot: Any, value: Any) -> bool:
        """PWR for one known slot, inside its limits, only in a synchronised local session."""
        if type(slot) is not int or slot not in SETTINGS or self._setting is None:
            return False
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        low, high = SETTINGS[slot]
        if not low <= value <= high:
            return False
        self._setting(slot, int(value) if float(value).is_integer() else round(float(value), 1))
        return True

    def command(self, value: Any) -> bool:
        """CMD 0/1 towards the module. Only in a synchronised local session, so never next to the cloud."""
        if type(value) is not int or value not in (0, 1) or self._command is None:
            return False
        self._command(value)
        return True

    def _event(self, event: str, **fields: Any) -> None:
        LOG.info("%s %s", event, fields or "")
        self.journal.write(event, **fields)

    async def _read_connect(self, reader: asyncio.StreamReader) -> bytes:
        """Everything received up to and including the first complete frame, which must be CONNECT."""
        data = bytearray()
        async with asyncio.timeout(CONNECT_READ_TIMEOUT):
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    raise ConnectionResetError("WiFi module closed before CONNECT")
                data.extend(chunk)
                frames = MQTTFrames().feed(bytes(data))
                if frames:
                    if frames[0][0] >> 4 != 1:
                        raise ValueError("First MQTT frame is not CONNECT")
                    return bytes(data)

    async def _open_cloud(self, initial: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
        """Connect, pass the WiFi module CONNECT and wait for the answer. A TCP accept alone proves nothing."""
        timeout = self.config["connect_timeout"]
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.config["upstream_host"], self.config["upstream_port"]), timeout)
            tune_socket(writer)
            peer = writer.get_extra_info("peername")[0]
            own = writer.get_extra_info("sockname")[0]
            if peer == own and not ipaddress.ip_address(peer).is_loopback:
                raise CloudUnavailable("upstream resolves to this host")
            writer.write(initial)
            await writer.drain()
            first = await asyncio.wait_for(reader.read(65536), timeout)
            if not first:
                raise CloudUnavailable("closed without an answer")
            if first[0] == 0x20 and len(first) >= 4 and first[3] != 0:
                raise CloudUnavailable(f"CONNACK refused, code {first[3]}")
            return reader, writer, first
        except (OSError, asyncio.TimeoutError, CloudUnavailable) as exc:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            raise CloudUnavailable(str(exc) or type(exc).__name__) from exc

    async def online(self, session: Session, module_reader: asyncio.StreamReader, module_writer: asyncio.StreamWriter,
                     cloud_reader: asyncio.StreamReader, cloud_writer: asyncio.StreamWriter,
                     initial: bytes, first: bytes) -> None:
        self.state.mode = "proxy"
        self.state.upstream_connected = True
        self._event("proxy_session")
        up = MQTTObserver(self.state, self.journal, session, True)
        down = MQTTObserver(self.state, self.journal, session, False)
        up.feed(initial)
        down.feed(first)
        module_writer.write(first)
        await module_writer.drain()
        tasks = [asyncio.create_task(pump(module_reader, cloud_writer, up)),
                 asyncio.create_task(pump(cloud_reader, module_writer, down))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            self.state.last_session_end = "module closed" if tasks[0] in done else "cloud closed"
            for task in done:
                if task.exception():
                    self.state.last_session_end = type(task.exception()).__name__
                    LOG.warning("Proxy stream closed: %s", self.state.last_session_end)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.state.upstream_connected = False
            cloud_writer.close()
            with contextlib.suppress(Exception):
                await cloud_writer.wait_closed()

    async def local(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    initial: bytes = b"") -> None:
        """Minimal broker for one allowlisted WiFi module: protocol acknowledgements and REFRESH/RSTAT only."""
        self.state.mode = "local"
        self.state.upstream_connected = False
        self._command = None
        self._setting = None
        pending_reads: list[int] = []
        next_settings: float | None = None
        self._event("local_session")
        frames = MQTTFrames()
        key: bytes | None = None
        subscribed = False
        topic_server: str | None = None
        srv_msg_id = 1
        sync_ok = False
        next_refresh: float | None = None
        rstat_at: float | None = None
        poll_interval = self.config["poll_interval_seconds"]
        pending = initial

        def publish(obj: dict[str, Any]) -> None:
            nonlocal srv_msg_id
            assert key is not None and topic_server is not None
            srv_msg_id += 1
            message = {**obj, "srvMsgId": srv_msg_id}
            writer.write(pub_frame(topic_server, sign_obj(message, key)))
            self.journal.message("local_down", msg=redact(message))

        def request(name: str, value: Any) -> None:
            if name not in LOCAL_REQUESTS:
                raise ValueError("Only read requests are allowed")
            publish({name: value})
            self.state.status_requests_sent += 1
            self.state.last_status_request_unix = time.time()

        def send_command(value: int) -> None:
            nonlocal next_refresh
            publish({"CMD": value})
            self.state.last_command = json.dumps({"CMD": value})
            self._event("command", value=value)
            next_refresh = time.monotonic()  # as the portal does: the answer shows the new state at once

        def send_setting(slot: int, value: float) -> None:
            publish({f"PWR {slot}": value})
            self._event("setting", slot=slot, value=value)
            pending_reads.append(slot)  # read it back, so what is shown is what the controller holds

        while True:
            if pending:
                chunk, pending = pending, b""
            else:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), 0.5)
                except asyncio.TimeoutError:
                    # Since Python 3.11 a socket ETIMEDOUT is the same exception as the wait above.
                    if reader.exception() is not None:
                        raise reader.exception() from None
                    chunk = None
            if chunk == b"":
                self.state.last_session_end = "WiFi module closed"
                return
            if chunk:
                try:
                    decoded = frames.feed(chunk)
                except ValueError:
                    # The module sometimes sends cut frames (seen in the capture and the proxy logs). The portal then
                    # drops the connection; here they are skipped, because every message is checked by its signature.
                    frames = MQTTFrames()
                    decoded = []
                    self.state.malformed_frames += 1
                for head, body in decoded:
                    kind = head >> 4
                    if kind == 1:
                        if key is not None:
                            raise ValueError("Duplicate MQTT CONNECT")
                        key = signing_key(read_connect_password(body))
                        writer.write(frame(0x20, b"\x00\x00"))
                    elif kind == 8 and key is not None:
                        packet_id, topics = subscribe_packet(body)
                        sub = next((t for t in topics if t.startswith("cm.srv.peltec2.")), None)
                        if sub:
                            # Captured: the WiFi module subscribes with dots, the broker publishes with slashes.
                            topic_server = "cm/srv/peltec2/" + sub.rsplit(".", 1)[-1]
                            subscribed = True
                        writer.write(frame(0x90, packet_id.to_bytes(2, "big") + bytes(
                            0 if t.startswith("cm.srv.peltec2.") else 0x80 for t in topics)))
                    elif kind == 12 and key is not None:
                        writer.write(frame(0xD0))
                    elif kind == 14:
                        self.state.last_session_end = "MQTT DISCONNECT"
                        return
                    elif kind == 3 and key is not None:
                        topic, payload = get_publish(head, body)
                        if (head >> 1) & 3 == 1:
                            _, off = unpack_utf8(body, 0)
                            writer.write(frame(0x40, body[off:off + 2]))
                        if not topic.startswith("cm.inst.peltec2."):
                            continue
                        message = verify_payload(payload, key)
                        if message is None:
                            self.state.invalid_messages += 1
                            self.journal.message("up", valid=False, length=len(payload))
                            continue
                        self.journal.message("up", valid=True, msg=redact(message))
                        self.state.ingest(message)
                        if "_sync" in message and subscribed and topic_server:
                            client_id = message.get("clMsgId")
                            token = message.get("_token")
                            if type(client_id) is int and isinstance(token, str) and len(token) <= 80:
                                publish({"_sync_ACK": "ok", "clMsgId": client_id + 1, "_token": token})
                                sync_ok = True
                                self._command = send_command
                                self._setting = send_setting
                                next_settings = time.monotonic() + RSTAT_DELAY + 2
                                next_refresh = time.monotonic() + self.config["first_poll_delay_seconds"]
                                self._event("local_sync_ack")
                        elif sync_ok and topic_server:
                            for name, value in message.items():
                                if name == "PING" or ACK_RE.match(name):
                                    publish({f"{name}_ACK": value})
                await writer.drain()

            now = time.monotonic()
            if next_refresh is not None and now >= next_refresh:
                request("REFRESH", 0)
                rstat_at = now + RSTAT_DELAY
                next_refresh = now + poll_interval
                await writer.drain()
            if next_settings is not None and now >= next_settings:
                pending_reads.extend(slot for slot in SETTINGS if slot not in pending_reads)
                next_settings = now + SETTINGS_READ_SECONDS
            if pending_reads:  # one per loop: the portal never sends a large burst either
                publish({f"PRD {pending_reads.pop(0)}": "VAL"})
                await writer.drain()
            if rstat_at is not None and now >= rstat_at:
                request("RSTAT", "ALL")
                rstat_at = None
                await writer.drain()

    async def _serve(self, session: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        mode = self.config["connection_mode"]
        if mode == "local":
            await self.local(reader, writer)
            return
        initial = await self._read_connect(reader)
        try:
            cloud_reader, cloud_writer, first = await self._open_cloud(initial)
        except CloudUnavailable as exc:
            self.state.last_session_end = f"cloud: {exc}"
            self._event("cloud_unavailable", reason=str(exc))
            return
        await self.online(session, reader, writer, cloud_reader, cloud_writer, initial, first)

    async def handle_module(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if (peer[0] if peer else "") != self.config["module_ip"]:
            LOG.debug("Rejected connection from another IP")  # also the Supervisor watchdog
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        tune_socket(writer)
        session = Session(writer)
        previous, self._current = self._current, session
        if previous is not None:
            # A WiFi module that rebooted leaves no FIN behind; the newest connection is always the real one.
            self._event("session_replaced")
            transport = previous.writer.transport
            if transport is not None:
                transport.abort()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(previous.done.wait(), 5)
        if self._current is not session:
            writer.close()
            session.done.set()
            return
        self.state.module_connected = True
        try:
            await self._serve(session, reader, writer)
        except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
            self.state.last_session_end = f"{type(exc).__name__}: {exc}"[:80]
            LOG.warning("WiFi module session ended: %s", self.state.last_session_end)
        finally:
            if self._current is session:
                self._current = None
                self._command = None
                self._setting = None
                self.state.module_connected = False
                self.state.upstream_connected = False
                self.state.mode = "idle"
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            session.done.set()
            self._event("session_closed", reason=self.state.last_session_end)

    async def run(self) -> None:
        server = await asyncio.start_server(self.handle_module, self.config["listen_host"], self.config["listen_port"])
        api = await asyncio.start_server(lambda r, w: send_http_status(r, w, self),
                                         self.config["api_host"], self.config["api_port"])
        LOG.info("Gateway listening on TCP %s, mode %s; diagnostic API on %s:%s", self.config["listen_port"],
                 self.config["connection_mode"], self.config["api_host"], self.config["api_port"])
        async with server, api:
            await asyncio.gather(server.serve_forever(), api.serve_forever())


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    result = {
        "connection_mode": cfg.get("connection_mode", "local"),
        "module_ip": cfg.get("module_ip", ""),
        "upstream_host": cfg.get("upstream_host", "portal.centrometal.hr"),
        "upstream_port": 1883,
        "listen_host": "0.0.0.0",
        "listen_port": 1883,
        "api_host": "127.0.0.1",
        "api_port": 8765,
        "connect_timeout": 5.0,
        "poll_interval_seconds": int(cfg.get("poll_interval_seconds", 240)),
        "first_poll_delay_seconds": 2.0,
        "debug_journal": bool(cfg.get("debug_journal", False)),
    }
    if result["connection_mode"] not in MODES:
        raise ValueError("Invalid connection_mode")
    ipaddress.ip_address(result["module_ip"])
    if not 10 <= result["poll_interval_seconds"] <= 3600:
        raise ValueError("Invalid poll interval")
    if result["connection_mode"] != "local" and result["upstream_host"] in ("127.0.0.1", "localhost", result["listen_host"]):
        raise ValueError("Upstream must not point back to this gateway")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/data/options.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    asyncio.run(Gateway(config, Journal(JOURNAL_PATH, debug=config["debug_journal"])).run())


if __name__ == "__main__":
    main()
