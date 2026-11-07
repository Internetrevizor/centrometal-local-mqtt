"""What the app remembers: readings, alarms, counters and the journal. A failure here must never touch the MQTT traffic."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any

from parameters import MAX_AGE_SECONDS, valid_raw
from protocol import ALARM_RE, COMMAND_RE, FIELD_RE, at_commands, redact

LOG = logging.getLogger("peltec-gateway")
ALARM_HISTORY = 50
JOURNAL_MAX_BYTES = 10_000_000
JOURNAL_KEEP = 3
VALUES_KEEP_SECONDS = 3600
BURST_SECONDS = 0.3  # the module sends a dump in several messages: answer once, not once per message
MAX_VALUES = 750  # settings tables can be large
COUNTERS = ("sessions_closed", "malformed_frames", "stale_messages", "invalid_messages", "rejected_values", "commands_expired",
            "identity_changes")
# After leaking AT text the module sends messages again, with the same clMsgId, up to 234 s later (night of 06-07.11.2026: 34
# messages, at most 32 numbers behind the newest). Further back than this is not a repeat: the numbering has started over.
RETRANSMIT_WINDOW = 200
MAX_CLOSE_REASONS = 20
MAX_AT_COMMANDS = 20


def atomic_write(path: str, text: str, mode: int | None = None) -> None:
    """The boiler room loses power sometimes: the file on disk is either the old one or the new one, never half of one."""
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, path)


class Journal:
    """Rotating JSON lines, redacted."""

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


class ModuleIdentity:
    """Remembers which module connected first, as a salted fingerprint.

    The password in CONNECT is also the signing key, so a signature alone does not prove who is at the other end. For now a
    different identity is only counted and shown: whether the password stays the same across reconnects has to be seen on
    the real boiler before a mismatch may refuse anything.
    """

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.salt: bytes | None = None
        self.fingerprint: str | None = None
        if path:
            with contextlib.suppress(OSError, ValueError, TypeError, KeyError, AttributeError):
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.salt = bytes.fromhex(data["salt"])
                self.fingerprint = str(data["fingerprint"])

    def check(self, client_id: bytes, password: bytes) -> bool:
        """True when this is the known module, or the first one ever seen."""
        if self.fingerprint is None or self.salt is None:
            self.salt = secrets.token_bytes(16)
            self.fingerprint = hmac.new(self.salt, client_id + b"\x00" + password, hashlib.sha256).hexdigest()
            if self.path:
                with contextlib.suppress(OSError):
                    atomic_write(self.path, json.dumps({"salt": self.salt.hex(), "fingerprint": self.fingerprint}), 0o600)
            return True
        return hmac.compare_digest(self.fingerprint, hmac.new(self.salt, client_id + b"\x00" + password, hashlib.sha256).hexdigest())


class State:
    def __init__(self, journal: "Journal | None" = None, alarm_path: str | None = None,
                 values_path: str | None = None, identity_path: str | None = None) -> None:
        self.journal = journal
        self.alarm_path = alarm_path
        self.values_path = values_path
        self.identity = ModuleIdentity(identity_path)
        self.revision = 0
        self._changed = asyncio.Event()
        self.active_alarms: dict[str, Any] = {}
        self.alarm_history: list[dict[str, Any]] = []
        self._load_alarms()
        self.seen_keys: set[str] = set()
        self.mode = "idle"
        self.module_connected = False
        self.upstream_connected = False
        self.values: dict[str, Any] = {}
        self.value_times: dict[str, float] = {}
        self.value_seen_rev: dict[str, int] = {}
        self.value_changed_rev: dict[str, int] = {}
        self.last_update_unix: float | None = None
        self.verified_messages = 0
        self.invalid_messages = 0
        self.malformed_frames = 0
        self.stale_messages = 0
        self.last_client_id: int | None = None
        self.rejected_values = 0
        self.commands_expired = 0
        self.identity_changes = 0
        self.status_requests_sent = 0
        self.last_status_request_unix: float | None = None
        self.downlink_messages = 0
        self.downlink_keys: set[str] = set()
        self.last_downlink = ""
        self.last_command = ""
        self.last_session_end = ""
        self.last_session_end_time: str | None = None
        self.sessions_closed = 0
        self.close_reasons: dict[str, int] = {}
        self.at_commands: dict[str, int] = {}
        self.last_malformed = ""
        self.rejected_ip = ""
        self._load_values()

    def mark_changed(self) -> None:
        self.revision += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait_for_change(self, since: int, timeout: float) -> None:
        """Returns as soon as there is something newer than `since`, or after the timeout."""
        if since == self.revision:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)
            await asyncio.sleep(BURST_SECONDS)

    def _load_values(self) -> None:
        """Readings survive a restart of the app with their real age, so the 8-minute rule still applies to them."""
        if not self.values_path:
            return
        try:
            with open(self.values_path, encoding="utf-8") as handle:
                data = json.load(handle)
            now = time.time()
            times = {k: t for k, t in data["times"].items()
                     if k in data["values"] and 0 <= now - t < VALUES_KEEP_SECONDS}
            self.values = {k: data["values"][k] for k in times}
            self.value_times = times
            self.seen_keys = set(times)
            self.last_update_unix = max(times.values(), default=None)
            for name in COUNTERS:
                count = data.get("counters", {}).get(name)
                if type(count) is int and count >= 0:
                    setattr(self, name, count)
            reasons = data.get("close_reasons", {})
            if isinstance(reasons, dict):
                self.close_reasons = {str(k): v for k, v in reasons.items() if type(v) is int}
            commands = data.get("at_commands", {})
            if isinstance(commands, dict):
                self.at_commands = {str(k): v for k, v in commands.items() if type(v) is int}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            pass

    def values_payload(self) -> str:
        return json.dumps({"values": self.values, "times": self.value_times,
                           "counters": {name: getattr(self, name) for name in COUNTERS},
                           "close_reasons": self.close_reasons, "at_commands": self.at_commands})

    def write_values(self, payload: str) -> None:
        if self.values_path:
            with contextlib.suppress(OSError):
                atomic_write(self.values_path, payload)

    def save_values(self) -> None:
        self.write_values(self.values_payload())

    def is_fresh(self) -> bool:
        t = self.last_update_unix
        return bool(t and time.time() - t < MAX_AGE_SECONDS)  # a reconnect of a few seconds is not an outage

    def _load_alarms(self) -> None:
        """Alarms survive a restart of the app: the boiler may not repeat them."""
        if not self.alarm_path:
            return
        try:
            with open(self.alarm_path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.active_alarms = dict(data.get("active", {}))
            self.alarm_history = list(data.get("history", []))[-ALARM_HISTORY:]
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save_alarms(self) -> None:
        if self.alarm_path:
            with contextlib.suppress(OSError):
                atomic_write(self.alarm_path, json.dumps({"active": self.active_alarms, "history": self.alarm_history}))

    def _note_alarm(self, code: str, value: Any) -> None:
        """Alarms only come from the boiler: nothing here can end one. A value starting with P means it has passed (scr34m);
        not yet seen on a PelTec II."""
        cleared = str(value).startswith("P")
        known = self.active_alarms.get(code)
        if (cleared and code not in self.active_alarms) or (not cleared and known == value):
            return
        if cleared:
            self.active_alarms.pop(code, None)
        elif not code.startswith("I"):
            self.active_alarms[code] = value
        self.alarm_history.append({"code": code, "value": value, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        self.alarm_history = self.alarm_history[-ALARM_HISTORY:]
        self._save_alarms()

    def note_module_identity(self, client_id: bytes, password: bytes) -> None:
        if not self.identity.check(client_id, password):
            self.identity_changes += 1
            LOG.warning("A module with a different identity connected from the allowed address")
            if self.journal:
                self.journal.write("identity_changed")

    def note_skipped(self, runs: int, skipped: bytes) -> tuple[str, dict[str, int]]:
        """Bytes in the module's stream that are not a frame. Only the length, the kind and the AT names are kept."""
        commands = at_commands(skipped)
        kind = "AT text" if b"AT+" in skipped else "other"
        moment = datetime.now(timezone.utc).isoformat(timespec="seconds")
        names = ", ".join(f"{name} x{count}" for name, count in commands.items())
        self.malformed_frames += runs
        self.last_malformed = f"{moment} {len(skipped)} bytes, {kind}" + (f": {names}" if names else "")
        for name, count in commands.items():
            if name in self.at_commands or len(self.at_commands) < MAX_AT_COMMANDS:
                self.at_commands[name] = self.at_commands.get(name, 0) + count
        return kind, commands

    def note_status_request(self) -> None:
        self.status_requests_sent += 1
        self.last_status_request_unix = time.time()

    def note_session_end(self) -> None:
        self.sessions_closed += 1
        self.last_session_end_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
        reason = self.last_session_end.split(":", 1)[0] or "unknown"
        if reason in self.close_reasons or len(self.close_reasons) < MAX_CLOSE_REASONS:
            self.close_reasons[reason] = self.close_reasons.get(reason, 0) + 1
        self.mark_changed()

    def _is_repeat(self, signed: dict[str, Any]) -> bool:
        client_id = signed.get("clMsgId")
        if type(client_id) is not int:
            return False
        if self.last_client_id is not None and 0 <= self.last_client_id - client_id < RETRANSMIT_WINDOW:
            return True
        self.last_client_id = client_id
        return False

    def ingest(self, signed: dict[str, Any]) -> bool:
        """False for a message the module sent again: its values are old and would overwrite newer ones."""
        self.verified_messages += 1
        if self._is_repeat(signed):
            self.stale_messages += 1
            if self.journal:
                self.journal.message("stale_message", id=signed["clMsgId"])
            return False
        for code, value in signed.items():
            if ALARM_RE.match(code):
                self._note_alarm(code, value)
        telemetry = {}
        for key, value in signed.items():
            if not FIELD_RE.fullmatch(key) or key.endswith("_ACK") or key in ("clMsgId", "srvMsgId", "PING"):
                continue
            if not isinstance(value, (str, int, float, bool)) or (isinstance(value, str) and len(value) > 160):
                continue
            if not valid_raw(key, value):
                self.rejected_values += 1
                continue
            telemetry[key] = value
        if telemetry:
            now = time.time()
            for key in telemetry.keys() - self.seen_keys:
                self.seen_keys.add(key)
                if self.journal:
                    self.journal.write("new_key", key=key, value=telemetry[key])
            revision = self.revision + 1
            for key, value in telemetry.items():
                if key not in self.values or self.values[key] != value:
                    self.value_changed_rev[key] = revision
                self.value_seen_rev[key] = revision
            self.values.update(telemetry)
            self.value_times.update(dict.fromkeys(telemetry, now))
            if len(self.values) > MAX_VALUES:
                for key in sorted(self.value_times, key=self.value_times.get)[:len(self.values) - MAX_VALUES]:
                    for table in (self.values, self.value_times, self.value_seen_rev, self.value_changed_rev):
                        table.pop(key, None)
            self.last_update_unix = now
            self.mark_changed()
        return True

    def note_downlink(self, message: dict[str, Any]) -> None:
        clean = redact(message)
        text = json.dumps(clean, ensure_ascii=False)[:240]
        self.downlink_messages += 1
        self.last_downlink = text
        if len(self.downlink_keys) < 40:
            self.downlink_keys.update(k for k in clean if k != "srvMsgId")
        if any(COMMAND_RE.match(k) for k in clean):
            self.last_command = text

    def meta(self) -> dict[str, Any]:
        """Everything but the readings."""
        t = self.last_update_unix
        now = time.time()
        stamp = lambda v: datetime.fromtimestamp(v, timezone.utc).isoformat() if v else None  # noqa: E731
        return {
            "revision": self.revision,
            "mode": self.mode,
            "module_connected": self.module_connected,
            "upstream_connected": self.upstream_connected,
            "last_update": stamp(t),
            "age_seconds": round(max(now - t, 0), 1) if t else None,
            "verified_messages": self.verified_messages,
            "invalid_messages": self.invalid_messages,
            "rejected_values": self.rejected_values,
            "commands_expired": self.commands_expired,
            "identity_changes": self.identity_changes,
            "status_requests_sent": self.status_requests_sent,
            "last_status_request": stamp(self.last_status_request_unix),
            "downlink_messages": self.downlink_messages,
            "downlink_keys": sorted(self.downlink_keys),
            "last_downlink": self.last_downlink,
            "last_command": self.last_command,
            "last_session_end": self.last_session_end,
            "rejected_ip": self.rejected_ip,
            "last_session_end_time": self.last_session_end_time,
            "sessions_closed": self.sessions_closed,
            "close_reasons": dict(self.close_reasons),
            "data_fresh": self.is_fresh(),
            "malformed_frames": self.malformed_frames,
            "stale_messages": self.stale_messages,
            "last_malformed": self.last_malformed,
            "at_commands": dict(self.at_commands),
            "alarms": dict(self.active_alarms),
            "alarm_history": self.alarm_history[-20:],
        }

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {**self.meta(), "values": self.values.copy(),
                "value_age_seconds": {k: round(max(now - v, 0), 1) for k, v in self.value_times.items()}}

    def delta(self, since: int) -> dict[str, Any]:
        """What happened after revision `since`: values that changed, and keys reported again with the same value."""
        changed = {k: self.values[k] for k, rev in self.value_changed_rev.items() if rev > since and k in self.values}
        seen = [k for k, rev in self.value_seen_rev.items() if rev > since and k not in changed]
        return {**self.meta(), "values": changed, "seen": seen}
