"""One connection of the WiFi module in local mode, as a state machine: bytes and time in, bytes out. No sockets, no sleep.

Timings measured on a night of real traffic (05-06.11.2026, 16 sessions): `_sync` arrives 0.5 to 0.9 s after the connection
and only once per session; PING comes every 60 s; REFRESH is answered in about 0.3 s. So a session without `_sync` after
30 s, or silent for 3 minutes, will not recover by itself. Closing it makes the module reconnect within seconds.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from commands import Command, CommandBook
from parameters import LIMITS, setting_key
from protocol import ACK_RE, ModuleFrames, frame, get_publish, pub_frame, read_connect_fields, redact, sign_obj, signing_key, subscribe_packet, unpack_utf8, verify_payload
from store import Journal, State

SYNC_TIMEOUT_SECONDS = 30
SILENCE_TIMEOUT_SECONDS = 180
LOCAL_REQUESTS = {"REFRESH", "RSTAT"}
SETTINGS_READ_SECONDS = 3600
COUNTERS_READ_SECONDS = 60  # counters move slowly: RSTAT does not follow a short read interval
RSTAT_DELAY = 1.0  # same spacing as the integration: REFRESH, 1 s, RSTAT
READ_SPACING_SECONDS = 0.5  # settings are read one at a time: the portal never sends a large burst either


class LocalSession:
    """Minimal broker for one allowlisted WiFi module: protocol acknowledgements, reads, and the commands in the book."""

    def __init__(self, state: State, book: CommandBook, journal: Journal, config: dict[str, Any], now: float,
                 event: Callable[..., None]) -> None:
        self.state = state
        self.book = book
        self.journal = journal
        self.event = event
        self.read_interval = config["read_interval_seconds"]
        self.first_poll_delay = config["first_poll_delay_seconds"]
        self.frames = ModuleFrames()
        self.out = bytearray()
        self.key: bytes | None = None
        self.topic_server: str | None = None
        self.subscribed = False
        self.synced = False
        self.close_reason: str | None = None
        self.started = now
        self.last_heard = now
        self.srv_msg_id = 1
        self.next_refresh: float | None = None
        self.rstat_at: float | None = None
        self.next_counters = 0.0
        self.next_settings: float | None = None
        self.next_read = 0.0
        self.pending_reads: list[int] = []

    def take_output(self) -> bytes:
        data = bytes(self.out)
        self.out.clear()
        return data

    def _publish(self, obj: dict[str, Any]) -> None:
        assert self.key is not None and self.topic_server is not None
        self.srv_msg_id += 1
        message = {**obj, "srvMsgId": self.srv_msg_id}
        self.out += pub_frame(self.topic_server, sign_obj(message, self.key))
        self.journal.message("local_down", msg=redact(message))

    def _request(self, name: str, value: Any) -> None:
        if name not in LOCAL_REQUESTS:
            raise ValueError("Only read requests are allowed")
        self._publish({name: value})
        self.state.note_status_request()

    def feed(self, data: bytes, now: float) -> None:
        for head, body in self.frames.feed(data):
            self._frame(head, body, now)
        if self.frames.runs:
            # The portal drops the connection on these; here they are skipped and never answered. Every message is still
            # checked by its signature. Only the length, the kind and the AT names are kept: the bytes could hold a token.
            kind, commands = self.state.note_skipped(self.frames.runs, bytes(self.frames.skipped))
            self.event("skipped_bytes", count=len(self.frames.skipped), kind=kind, commands=commands)
            self.frames.runs = 0
            self.frames.skipped.clear()

    def _frame(self, head: int, body: bytes, now: float) -> None:
        kind = head >> 4
        if kind == 1:
            if self.key is not None:
                raise ValueError("Duplicate MQTT CONNECT")
            client_id, password = read_connect_fields(body)
            self.key = signing_key(password)
            self.state.note_module_identity(client_id, password)
            self.out += frame(0x20, b"\x00\x00")
        elif kind == 8 and self.key is not None:
            packet_id, topics = subscribe_packet(body)
            sub = next((t for t in topics if t.startswith("cm.srv.peltec2.")), None)
            if sub:
                # Captured: the WiFi module subscribes with dots, the broker publishes with slashes.
                self.topic_server = "cm/srv/peltec2/" + sub.rsplit(".", 1)[-1]
                self.subscribed = True
            self.out += frame(0x90, packet_id.to_bytes(2, "big") + bytes(
                0 if t.startswith("cm.srv.peltec2.") else 0x80 for t in topics))
        elif kind == 12 and self.key is not None:
            self.last_heard = now
            self.out += frame(0xD0)
        elif kind == 14:
            self.close_reason = "MQTT DISCONNECT"
        elif kind == 3 and self.key is not None:
            self._publish_from_module(head, body, now)

    def _publish_from_module(self, head: int, body: bytes, now: float) -> None:
        topic, payload = get_publish(head, body)
        if (head >> 1) & 3 == 1:
            _, off = unpack_utf8(body, 0)
            self.out += frame(0x40, body[off:off + 2])
        if not topic.startswith("cm.inst.peltec2."):
            return
        message = verify_payload(payload, self.key)
        if message is None:
            self.state.invalid_messages += 1
            self.journal.message("up", valid=False, length=len(payload))
            return
        self.last_heard = now
        self.journal.message("up", valid=True, msg=redact(message))
        self.state.ingest(message)
        self.book.observe(message, now)
        if "_sync" in message and self.subscribed and self.topic_server:
            client_id = message.get("clMsgId")
            token = message.get("_token")
            if type(client_id) is int and isinstance(token, str) and len(token) <= 80:
                self._publish({"_sync_ACK": "ok", "clMsgId": client_id + 1, "_token": token})
                self.synced = True
                self.next_settings = now + RSTAT_DELAY + 2
                self.next_refresh = now + self.first_poll_delay
                self.book.delay_pending(self.next_refresh + 2)
                self.event("local_sync_ack")
        elif self.synced and self.topic_server:
            for name, value in message.items():
                if name == "PING" or ACK_RE.match(name):
                    self._publish({f"{name}_ACK": value})

    def _send(self, command: Command, now: float) -> None:
        if command.kind == "power":
            self._publish({"CMD": int(command.value)})
            self.state.last_command = json.dumps({"CMD": int(command.value)})
            self.event("command", value=command.value, attempt=command.attempts + 1)
            self.next_refresh = now  # as the portal does: the answer shows the new state at once
        else:
            name = f"PWR {command.slot}"
            self._publish({name: command.value})
            self.state.last_command = json.dumps({name: command.value})
            self.event("setting", slot=command.slot, value=command.value, attempt=command.attempts + 1)
            self._read_back(command.slot, now)
        self.book.mark_sent(command, now)

    def _read_back(self, slot: int, now: float) -> None:
        """So what is shown is what the controller holds. K1 reports its state in the regular answer."""
        if setting_key(slot).startswith("PVAL_"):
            if slot in self.pending_reads:
                self.pending_reads.remove(slot)
            self.pending_reads.insert(0, slot)
        else:
            self.next_refresh = now

    def tick(self, now: float) -> None:
        """Everything that depends on time. Safe to call at any moment, as often as needed."""
        self.book.expire(now)
        if self.close_reason:
            return
        if not self.synced:
            if now - self.started >= SYNC_TIMEOUT_SECONDS:
                self.close_reason = f"no _sync within {SYNC_TIMEOUT_SECONDS} s"
            return
        if now - self.last_heard >= SILENCE_TIMEOUT_SECONDS:
            self.close_reason = f"module silent for {SILENCE_TIMEOUT_SECONDS} s"
            return
        for command in self.book.due_sends(now):
            self._send(command, now)
        for command in self.book.due_reads(now):
            self._read_back(command.slot, now)
        if self.next_refresh is not None and now >= self.next_refresh:
            self._request("REFRESH", 0)
            if now >= self.next_counters:
                self.rstat_at = now + RSTAT_DELAY
                self.next_counters = now + COUNTERS_READ_SECONDS
            self.next_refresh = now + self.read_interval
        if self.next_settings is not None and now >= self.next_settings:
            self.pending_reads.extend(slot for slot in LIMITS if slot not in self.pending_reads)
            self.next_settings = now + SETTINGS_READ_SECONDS
        if self.pending_reads and now >= self.next_read:
            self._publish({f"PRD {self.pending_reads.pop(0)}": "VAL"})
            self.next_read = now + READ_SPACING_SECONDS
        if self.rstat_at is not None and now >= self.rstat_at:
            self._request("RSTAT", "ALL")
            self.rstat_at = None
