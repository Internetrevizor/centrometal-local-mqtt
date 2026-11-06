"""What was asked of the boiler and what came of it. No sockets and no clock of its own: `now` always comes from outside.

A command ends in one of these states, and only the boiler's own answer can confirm it:
  confirmed     the module reported the requested value
  unchanged     a setting that the controller reported holding that value moments ago: nothing was written
  expired       no confirmation in time. The boiler may or may not have received it
  replaced      a newer request for the same thing took its place
  rate_limited  refused: too many writes to this setting in a short time
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from parameters import number, setting_key

PENDING = "pending"
CONFIRMED = "confirmed"
UNCHANGED = "unchanged"
EXPIRED = "expired"
REPLACED = "replaced"
RATE_LIMITED = "rate_limited"
FINISHED = (CONFIRMED, UNCHANGED, EXPIRED, REPLACED, RATE_LIMITED)

GIVE_UP_SECONDS = 90  # a dead link takes 60 s to notice, then the module reconnects within seconds
POWER_RETRY_SECONDS = 5
SETTING_RETRY_SECONDS = 15
SETTING_ATTEMPTS = 3
SETTING_READ_RETRY_SECONDS = 5
SETTING_TRUST_SECONDS = 120  # a value read this recently is what the controller holds: no need to write it again
WRITE_WINDOW_SECONDS = 600
WRITE_LIMIT = 6  # a person never needs more; an automation in a loop must not reach the controller's memory
SAME_VALUE = 0.05


class Command:
    def __init__(self, ident: int, kind: str, slot: int | None, value: float, now: float, deadline: float) -> None:
        self.ident = ident
        self.kind = kind
        self.slot = slot
        self.value = value
        self.created = now
        self.deadline = deadline
        self.next_send = now
        self.next_read: float | None = None
        self.attempts = 0
        self.state = PENDING
        self.finished = asyncio.Event()

    @property
    def key(self) -> str:
        return "power" if self.kind == "power" else f"setting_{self.slot}"


class CommandBook:
    def __init__(self, on_finish: Callable[[Command], None] | None = None) -> None:
        self.on_finish = on_finish
        self.give_up_seconds = GIVE_UP_SECONDS
        self.power_retry_seconds = POWER_RETRY_SECONDS
        self.setting_retry_seconds = SETTING_RETRY_SECONDS
        self.pending: dict[str, Command] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self._writes: dict[int, list[float]] = {}
        self._next_id = 1

    def _new(self, kind: str, slot: int | None, value: float, now: float) -> Command:
        command = Command(self._next_id, kind, slot, value, now, now + self.give_up_seconds)
        self._next_id += 1
        return command

    def _start(self, command: Command, now: float) -> Command:
        previous = self.pending.get(command.key)
        if previous is not None:
            self._finish(previous, REPLACED, now)
        self.pending[command.key] = command
        self._record(command, now)
        return command

    def _record(self, command: Command, now: float) -> None:
        self.results[command.key] = {
            "id": command.ident, "value": command.value, "state": command.state, "attempts": command.attempts,
            "seconds": round(now - command.created, 1), "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _finish(self, command: Command, state: str, now: float) -> None:
        command.state = state
        if self.pending.get(command.key) is command:
            del self.pending[command.key]
        self._record(command, now)
        command.finished.set()
        if self.on_finish:
            self.on_finish(command)

    def power(self, value: int, now: float) -> Command:
        current = self.pending.get("power")
        if current is not None and current.value == value:
            return current
        return self._start(self._new("power", None, value, now), now)

    def setting(self, slot: int, value: float, known: Any, known_age: float | None, now: float) -> Command:
        """Writes only what the controller does not already hold, and not over and over."""
        key = f"setting_{slot}"
        current = self.pending.get(key)
        if current is not None and current.value == value:
            return current
        command = self._new("setting", slot, value, now)
        held = number(known)
        if held is not None and known_age is not None and known_age < SETTING_TRUST_SECONDS and abs(held - value) < SAME_VALUE:
            self._finish(command, UNCHANGED, now)
            return command
        recent = [t for t in self._writes.get(slot, []) if now - t < WRITE_WINDOW_SECONDS]
        self._writes[slot] = recent
        if len(recent) >= WRITE_LIMIT:
            self._finish(command, RATE_LIMITED, now)
            return command
        return self._start(command, now)

    def delay_pending(self, until: float) -> None:
        """After a reconnect the first answer may show that a command already arrived, so it waits for that answer."""
        for command in self.pending.values():
            command.next_send = max(command.next_send, until)

    def due_sends(self, now: float) -> list[Command]:
        return [c for c in self.pending.values() if now >= c.next_send and (c.kind == "power" or c.attempts < SETTING_ATTEMPTS)]

    def mark_sent(self, command: Command, now: float) -> None:
        command.attempts += 1
        if command.kind == "power":
            command.next_send = now + self.power_retry_seconds
        else:
            command.next_send = now + self.setting_retry_seconds
            command.next_read = now + SETTING_READ_RETRY_SECONDS
            self._writes.setdefault(command.slot, []).append(now)
        self._record(command, now)

    def due_reads(self, now: float) -> list[Command]:
        """Settings still waiting for confirmation: the read request may have been lost as well."""
        due = [c for c in self.pending.values() if c.next_read is not None and now >= c.next_read]
        for command in due:
            command.next_read = now + SETTING_READ_RETRY_SECONDS
        return due

    def observe(self, message: dict[str, Any], now: float) -> None:
        """Only a signed message from the module, carrying the requested value, confirms a command."""
        for command in list(self.pending.values()):
            read_key = "B_CMD" if command.kind == "power" else setting_key(command.slot)
            seen = number(message.get(read_key))
            if seen is not None and abs(seen - command.value) < SAME_VALUE:
                self._finish(command, CONFIRMED, now)

    def expire(self, now: float) -> None:
        for command in list(self.pending.values()):
            if now >= command.deadline:
                self._finish(command, EXPIRED, now)

    def pending_power(self) -> float | None:
        command = self.pending.get("power")
        return command.value if command else None

    def summary(self) -> dict[str, dict[str, Any]]:
        return {key: dict(result) for key, result in self.results.items()}
