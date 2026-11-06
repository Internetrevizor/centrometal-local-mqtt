"""Centrometal Local MQTT Protocol: local server for the WiFi module of a PelTec II Lambda. Python standard library only.

connection_mode:
  local        the module talks only to this server; the cloud is never contacted.
  cloud_proxy  bypass: transparent TCP proxy to the portal, which keeps working; local readings are a copy.
               Sends nothing of its own to the module. Portal down = session closed, as with no proxy at all.

In local mode the server sends protocol acknowledgements, the read requests REFRESH, RSTAT and PRD, and, when asked
through the local API, CMD 0/1 and PWR for the settings listed in parameters.LIMITS, within their limits. Nothing else can
be emitted. Never logs or stores the MQTT password, _token, _sign or the installation ID.

This file holds the sockets and the API. The protocol is in protocol.py, one module connection in session.py, the commands
in commands.py and what is remembered in store.py: none of those touch a socket.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import time
import urllib.request
from http import HTTPStatus
from typing import Any

from commands import EXPIRED, FINISHED, RATE_LIMITED, Command, CommandBook
from parameters import LIMITS, setting_key
from protocol import (  # noqa: F401  (also the public names of this module, used by the tests)
    MQTTFrames, ModuleFrames, frame, get_publish, pub_frame, read_connect_password, redact, sign_obj, signing_key,
    varint, verify_payload,
)
from session import LocalSession
from store import Journal, State, atomic_write

LOG = logging.getLogger("peltec-gateway")
MODES = ("local", "cloud_proxy")
SETTINGS = LIMITS
JOURNAL_PATH = "/share/centrometal_local_mqtt/journal.jsonl"
DISCOVERY_SERVICE = "centrometal_local"
LONG_POLL_SECONDS = 25
EVENT_HEARTBEAT_SECONDS = 30
VALUES_SAVE_SECONDS = 300
READ_INTERVAL_SECONDS = 60
CONNECT_READ_TIMEOUT = 30
MAX_CONNECT_BYTES = 65536
SESSION_TICK_SECONDS = 0.5
MAX_COMMAND_WAIT_SECONDS = 20


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
        self.frames = self._new_frames()

    def _new_frames(self) -> MQTTFrames:
        return ModuleFrames() if self.module_to_cloud else MQTTFrames()  # only the module mixes AT text into its stream

    def feed(self, chunk: bytes) -> None:
        try:
            for header, body in self.frames.feed(chunk):
                kind = header >> 4
                if kind == 1 and self.module_to_cloud:
                    self.session.key = signing_key(read_connect_password(body))
                elif kind == 3:
                    self._publish(header, body)
            if isinstance(self.frames, ModuleFrames) and self.frames.runs:
                self.state.malformed_frames += self.frames.runs
                self.frames.runs = 0
                self.frames.skipped.clear()
        except Exception as exc:  # pylint: disable=broad-except
            self.frames = self._new_frames()
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


def _answer(status: HTTPStatus, data: bytes) -> bytes:
    return (f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            "Content-Type: application/json\r\nCache-Control: no-store\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data


def _sse(event: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"


async def stream_events(writer: asyncio.StreamWriter, gateway: "Gateway") -> None:
    """All the readings once, then only what changes, with a heartbeat that says the app is alive."""
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
    sent = gateway.state.revision
    writer.write(_sse({**gateway.status(), "type": "snapshot"}))
    await asyncio.wait_for(writer.drain(), 10)
    while not writer.is_closing():
        await gateway.state.wait_for_change(sent, gateway.config.get("event_heartbeat_seconds", EVENT_HEARTBEAT_SECONDS))
        event = gateway.status_delta(sent)
        sent = event["revision"]
        writer.write(_sse(event))
        await asyncio.wait_for(writer.drain(), 10)


async def send_http_status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, gateway: "Gateway") -> None:
    try:
        async with asyncio.timeout(3):
            line = await reader.readline()
            if len(line) > 4096:
                raise ValueError("Request too long")
            method, path, _ = line.decode("ascii").split(" ", 2)
            length = 0
            authorization = b""
            while (header := await reader.readline()) not in (b"\r\n", b"\n", b""):
                if header.lower().startswith(b"content-length:"):
                    length = int(header.split(b":", 1)[1])
                elif header.lower().startswith(b"authorization:"):
                    authorization = header.split(b":", 1)[1].strip()
            if length > 64:
                raise ValueError("Body too long")
            body = await reader.readexactly(length) if length else b""
        status, data = HTTPStatus.NOT_FOUND, b"{}"
        token = gateway.config.get("api_token")
        allowed = not token or hmac.compare_digest(authorization, f"Bearer {token}".encode())
        if path != "/health" and not allowed:
            status = HTTPStatus.UNAUTHORIZED
        elif method == "GET" and path == "/events":
            await stream_events(writer, gateway)
            return
        elif method == "GET" and (path == "/status" or path.startswith("/status?since=")):
            if "?since=" in path:
                await gateway.state.wait_for_change(int(path.split("=", 1)[1]), gateway.config.get("long_poll_seconds", LONG_POLL_SECONDS))
            status, data = HTTPStatus.OK, json.dumps(gateway.status()).encode()
        elif method == "GET" and path == "/health":
            status, data = HTTPStatus.OK, b'{"status":"ok"}'
        elif method == "POST" and path == "/command":
            request = json.loads(body) if body else {}
            status, data = await gateway.api_result(gateway.request_command(request.get("CMD")), request.get("wait"))
        elif method == "POST" and path == "/setting":
            request = json.loads(body) if body else {}
            status, data = await gateway.api_result(gateway.request_setting(request.get("slot"), request.get("value")), request.get("wait"))
        elif method == "POST" and path == "/alarms/clear":
            status, data = HTTPStatus.OK, json.dumps({"cleared": gateway.state.clear_alarms()}).encode()
        writer.write(_answer(status, data))
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
        self.state = State(self.journal, config.get("alarm_path"), config.get("values_path"), config.get("identity_path"))
        self.commands = CommandBook(self._command_finished)
        self._current: Session | None = None
        self._local: LocalSession | None = None
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        commands = self._command_status()  # first: a command that expires now belongs in the counters below
        return {**self.state.snapshot(), **commands}

    def status_delta(self, since: int) -> dict[str, Any]:
        commands = self._command_status()
        return {**self.state.delta(since), **commands, "type": "delta"}

    def _command_status(self) -> dict[str, Any]:
        self.commands.expire(time.monotonic())
        return {"connection_mode": self.config["connection_mode"],
                "read_interval_seconds": self.config["read_interval_seconds"],
                "pending_command": self.commands.pending_power(),
                "commands": self.commands.summary()}

    def _command_finished(self, command: Command) -> None:
        if command.state == EXPIRED:
            self.state.commands_expired += 1
        self._event("command_result", command=command.key, value=command.value, state=command.state, attempts=command.attempts)
        self.state.mark_changed()

    def _may_accept(self) -> bool:
        """Never next to the cloud. In local mode a short reconnect does not refuse a command, it waits for the module."""
        if self.config["connection_mode"] != "local":
            return False
        return (self._local is not None and self._local.synced) or self.state.is_fresh()

    def _kick(self) -> None:
        """A new command goes out now, not at the next tick of the session."""
        if self._local is not None and self._current is not None:
            self._local.tick(time.monotonic())
            data = self._local.take_output()
            if data:
                self._current.writer.write(data)

    def request_command(self, value: Any) -> Command | None:
        """CMD 0/1 towards the module, repeated until the module echoes it in B_CMD or the time runs out."""
        if type(value) is not int or value not in (0, 1) or not self._may_accept():
            return None
        command = self.commands.power(value, time.monotonic())
        self._kick()
        return command

    def request_setting(self, slot: Any, value: Any) -> Command | None:
        """PWR for one known slot, inside its limits."""
        if type(slot) is not int or slot not in LIMITS or isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        low, high = LIMITS[slot]
        if not low <= value <= high or not self._may_accept():
            return None
        value = int(value) if float(value).is_integer() else round(float(value), 1)
        key = setting_key(slot)
        read_at = self.state.value_times.get(key)
        age = time.time() - read_at if read_at else None
        command = self.commands.setting(slot, value, self.state.values.get(key), age, time.monotonic())
        self._kick()
        return command

    def command(self, value: Any) -> bool:
        return self.request_command(value) is not None

    def setting(self, slot: Any, value: Any) -> bool:
        command = self.request_setting(slot, value)
        return command is not None and command.state != RATE_LIMITED

    async def api_result(self, command: Command | None, wait: Any) -> tuple[HTTPStatus, bytes]:
        """200 once the request is taken. With `wait`, the answer also tells what came of it in that time."""
        if command is None:
            return HTTPStatus.CONFLICT, json.dumps({"accepted": False, "state": "refused"}).encode()
        if isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait > 0 and command.state not in FINISHED:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(command.finished.wait(), min(wait, MAX_COMMAND_WAIT_SECONDS))
        accepted = command.state != RATE_LIMITED
        return (HTTPStatus.OK if accepted else HTTPStatus.CONFLICT,
                json.dumps({"accepted": accepted, "id": command.ident, "state": command.state}).encode())

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
                if len(data) > MAX_CONNECT_BYTES:
                    raise ValueError("No CONNECT from the WiFi module")
                frames = ModuleFrames().feed(bytes(data))
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

    async def local(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """The sockets of one local session. What to say and when is decided in LocalSession."""
        self.state.mode = "local"
        self.state.upstream_connected = False
        self._event("local_session")
        session = LocalSession(self.state, self.commands, self.journal, self.config, time.monotonic(), self._event)
        self._local = session
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), SESSION_TICK_SECONDS)
            except asyncio.TimeoutError:
                # Since Python 3.11 a socket ETIMEDOUT is the same exception as the wait above.
                if reader.exception() is not None:
                    raise reader.exception() from None
                chunk = None
            if chunk == b"":
                self.state.last_session_end = "WiFi module closed"
                return
            now = time.monotonic()
            if chunk:
                session.feed(chunk, now)
            session.tick(now)
            data = session.take_output()
            if data:
                writer.write(data)
                await writer.drain()
            if session.close_reason:
                self.state.last_session_end = session.close_reason
                return

    async def _serve(self, session: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.config["connection_mode"] == "local":
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
            if peer and not peer[0].startswith("172.30."):  # the internal network of the Supervisor
                self.state.rejected_ip = peer[0]
                LOG.warning("Rejected a connection from %s: not module_ip", peer[0])
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
            self._local = None
            transport = previous.writer.transport
            if transport is not None:
                transport.abort()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(previous.done.wait(), 5)
            self.state.last_session_end = "replaced by a new connection"
        if self._current is not session:
            writer.close()
            session.done.set()
            return
        self.state.module_connected = True
        self.state.mark_changed()
        try:
            await self._serve(session, reader, writer)
        except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
            self.state.last_session_end = f"{type(exc).__name__}: {exc}"[:80]
            LOG.warning("WiFi module session ended: %s", self.state.last_session_end)
        finally:
            if self._current is session:
                self._current = None
                self._local = None
                self.state.module_connected = False
                self.state.upstream_connected = False
                self.state.mode = "idle"
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            session.done.set()
            self.state.note_session_end()
            self._event("session_closed", reason=self.state.last_session_end)

    async def _save_values_regularly(self) -> None:
        while True:
            await asyncio.sleep(VALUES_SAVE_SECONDS)
            await asyncio.to_thread(self.state.write_values, self.state.values_payload())

    async def _announce(self) -> None:
        """Tells Home Assistant where the app is. If this never works, the connection file still does the job."""
        announced = self.config.get("announce")
        if not announced:
            return
        for delay in (0, 10, 30, 60):
            await asyncio.sleep(delay)
            if await asyncio.to_thread(send_discovery, **announced):
                self._event("discovery_sent", host=announced["host"])
                return
        self._event("discovery_failed")

    async def run(self) -> None:
        server = await asyncio.start_server(self.handle_module, self.config["listen_host"], self.config["listen_port"])
        api = await asyncio.start_server(lambda r, w: send_http_status(r, w, self),
                                         self.config["api_host"], self.config["api_port"])
        LOG.info("Gateway listening on TCP %s, mode %s; diagnostic API on %s:%s", self.config["listen_port"],
                 self.config["connection_mode"], self.config["api_host"], self.config["api_port"])
        # The app runs as PID 1, where SIGTERM has no default action: without this handler the process ignores the stop
        # request and Docker has to kill it after its timeout.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.request_stop)
        background = [asyncio.create_task(self._save_values_regularly()), asyncio.create_task(self._announce())]
        await self._stop.wait()
        LOG.info("Stopping")
        for task in background:
            task.cancel()
        self.state.save_values()
        server.close()
        api.close()
        current = self._current
        if current is not None and current.writer.transport is not None:
            current.writer.transport.abort()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(current.done.wait(), 2)


def same_content(first: str, second: str) -> bool:
    with open(first, "rb") as one, open(second, "rb") as other:
        return one.read() == other.read()


def install_integration(source: str = "/app/integration/centrometal_local",
                        target: str = "/homeassistant/custom_components/centrometal_local") -> bool:
    """Puts the Home Assistant integration in place, so the app is the only thing to install. True when files changed."""
    try:
        if not os.path.isdir(source) or not os.path.isdir(os.path.dirname(os.path.dirname(target))):
            return False
        changed = False
        for folder, _, names in os.walk(source):
            destination = os.path.join(target, os.path.relpath(folder, source))
            for name in names:
                old, new = os.path.join(folder, name), os.path.join(destination, name)
                if os.path.exists(new) and same_content(old, new):
                    continue
                os.makedirs(destination, exist_ok=True)
                shutil.copyfile(old, new)
                changed = True
        if changed:
            LOG.warning("Integration installed or updated: restart Home Assistant, then add Centrometal Local MQTT Protocol")
        return changed
    except OSError as exc:
        LOG.warning("Could not install the integration: %s", type(exc).__name__)
        return False


def api_token(path: str = "/data/api_token") -> str:
    """A random token, kept across restarts in the app's private storage."""
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    atomic_write(path, token)
    return token


def own_address() -> str:
    """This container's address on the internal network; the target is not actually contacted."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("172.30.32.1", 80))
        return sock.getsockname()[0]


def write_connection_file(port: int, token: str, address: str,
                          target: str = "/homeassistant/custom_components/centrometal_local/gateway.json") -> None:
    """Tells the integration how to reach this app, so nothing has to be typed. Written again at every start."""
    try:
        if not os.path.isdir(os.path.dirname(target)):
            return
        atomic_write(target, json.dumps({"host": address, "port": port, "token": token}), 0o600)
    except OSError as exc:
        LOG.warning("Could not write the connection file: %s", type(exc).__name__)


def send_discovery(url: str, supervisor_token: str, host: str, port: int, token: str, timeout: float = 10) -> bool:
    """POST /discovery to the Supervisor, which passes the details to the integration. Sending it again changes nothing."""
    body = json.dumps({"service": DISCOVERY_SERVICE, "config": {"host": host, "port": port, "token": token}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {supervisor_token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except OSError as exc:  # URLError and HTTPError are OSError
        LOG.warning("Discovery message not delivered: %s", type(exc).__name__)
        return False


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
        "api_host": "0.0.0.0",
        "api_port": 8765,
        "connect_timeout": 5.0,
        "read_interval_seconds": int(cfg.get("read_interval_seconds", READ_INTERVAL_SECONDS)),
        "first_poll_delay_seconds": 2.0,
        "debug_journal": bool(cfg.get("debug_journal", False)),
        "alarm_path": "/data/alarms.json",
        "values_path": "/data/values.json",
        "identity_path": "/data/module_identity.json",
    }
    if result["connection_mode"] not in MODES:
        raise ValueError("Invalid connection_mode")
    ipaddress.ip_address(result["module_ip"])
    if not 10 <= result["read_interval_seconds"] <= 240:  # above 240 s readings would pass for stale between two reads
        raise ValueError("Invalid read interval")
    if result["connection_mode"] != "local" and result["upstream_host"] in ("127.0.0.1", "localhost", result["listen_host"]):
        raise ValueError("Upstream must not point back to this gateway")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/data/options.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    config["api_token"] = api_token()
    install_integration()
    write_connection_file(config["api_port"], config["api_token"], own_address())
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if supervisor_token:
        config["announce"] = {"url": "http://supervisor/discovery", "supervisor_token": supervisor_token,
                              "host": socket.gethostname(), "port": config["api_port"], "token": config["api_token"]}
    asyncio.run(Gateway(config, Journal(JOURNAL_PATH, debug=config["debug_journal"])).run())


if __name__ == "__main__":
    main()
