"""The wire format of the WiFi module: MQTT frames and signed JSON. No sockets, no clock, no state beyond a read buffer."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import struct
from typing import Any

SEED = b"158208e84665e0158208e84665e065e0158208e8"  # public protocol constant
MAX_PACKET = 1_048_576
MAX_BUFFER = MAX_PACKET + 5
MAX_MODULE_FRAME = 4096  # the largest real message is about 300 bytes
FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ /.]{0,79}$")  # real key: B_PTV/GRI; manual codes: E8.1, W2.1
COMMAND_RE = re.compile(r"^(CMD|PWR|SCCMD)\b")  # PRD only reads a settings table
ALARM_RE = re.compile(r"^[EWI]\d+(\.\d+)?$")  # controller manual, chapter 6: errors, warnings, information
ACK_RE = re.compile(r"^[EW]\d+(\.\d+)?$")  # scr34m acknowledges errors and warnings


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


def read_connect_fields(body: bytes) -> tuple[bytes, bytes]:
    """Client identifier and password of a CONNECT."""
    protocol, ptr = unpack_utf8(body, 0)
    if protocol != b"MQTT" or len(body) < ptr + 4 or body[ptr] != 4:
        raise ValueError("Only MQTT 3.1.1 supported")
    flags = body[ptr + 1]
    if flags & 1:
        raise ValueError("Invalid MQTT flags")
    ptr += 4  # version, flags, keepalive
    client_id, ptr = unpack_utf8(body, ptr)
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
    return client_id, password


def read_connect_password(body: bytes) -> bytes:
    return read_connect_fields(body)[1]


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


def module_frame_end(buf: bytearray) -> int | None:
    """End of the MQTT frame at the start of buf; None when more bytes are needed; -1 when this is not a frame."""
    head = buf[0]
    kind = head >> 4
    if not (head in (0x10, 0x40, 0x82, 0xC0, 0xE0) or (kind == 3 and (head >> 1) & 3 != 3)):
        return -1
    length, shift = 0, 0
    for idx in range(1, 4):
        if idx >= len(buf):
            return None
        length |= (buf[idx] & 127) << shift
        shift += 7
        if not buf[idx] & 128:
            break
    else:
        return -1
    if length > MAX_MODULE_FRAME:
        return -1
    if (head in (0xC0, 0xE0) and length != 0) or (head == 0x40 and length != 2) or (head == 0x82 and length < 5):
        return -1
    start, end = idx + 1, idx + 1 + length
    body = bytes(buf[start:end])
    if head == 0x10 and not body.startswith(b"\x00\x04MQTT"[:len(body)]):
        return -1
    if kind == 3:
        if length < 5:
            return -1
        if len(body) >= 2:
            size = int.from_bytes(body[:2], "big")
            if size < 3 or 2 + size > length or not body[2:5].startswith(b"cm."[:len(body[2:5])]):
                return -1
    if end > len(buf):
        return None
    try:
        if kind == 3:
            payload = get_publish(head, body)[1]
            if not payload.startswith(b"{") or not payload.endswith(b"}") or b"cm.inst." in payload:
                return -1  # cut: the declared length ran over into what came next
        elif head == 0x82:
            subscribe_packet(body)
    except ValueError:
        return -1
    return end


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


class ModuleFrames(MQTTFrames):
    """The stream from the module, which is not always clean MQTT.

    The boiler's controller drives its WiFi chip with AT commands, and these sometimes land in the TCP stream: the capture
    ends with a frame followed by AT+CWLIF repeated, right before the portal resets the connection. Read as MQTT that text
    is a frame of random length that swallows real messages. Here only what looks like a real frame is accepted, and
    everything else is skipped byte by byte, so the stream finds its way back at the next message.
    """

    def __init__(self) -> None:
        super().__init__()
        self.skipped = bytearray()
        self.runs = 0
        self._in_run = False

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buf.extend(data)
        result = []
        while self.buf:
            end = module_frame_end(self.buf)
            if end is None:
                break
            if end < 0:
                if not self._in_run:
                    self._in_run = True
                    self.runs += 1
                if len(self.skipped) < 4096:
                    self.skipped.append(self.buf[0])
                del self.buf[0]
                continue
            self._in_run = False
            result.append(extract_frame(self.buf))
        return result
