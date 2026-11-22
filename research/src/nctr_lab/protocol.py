"""Wire framing without assigning meanings to unknown payloads."""

from dataclasses import dataclass
import struct


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    mid: int
    declared: int
    token: bytes
    body: bytes
    offset: int
    raw: bytes


def packet(mid: int, token: bytes, body: bytes = b"", declared=None) -> bytes:
    if len(token) != 2 or not 0 <= mid <= 65535:
        raise ValueError("invalid message ID or token")
    length = len(body) + 2 if declared is None else declared
    if not 2 <= length <= 65535:
        raise ValueError("invalid declared length")
    return struct.pack("!HH", mid, length) + token + body


class Decoder:
    def __init__(self, max_length=65535, *, legacy_time_reply=False):
        self.max_length = max_length
        self.legacy_time_reply = legacy_time_reply
        self.buffer = bytearray()
        self.offset = 0

    def feed(self, data):
        # Yield complete frames before a later malformed header is encountered.
        self.buffer.extend(data)
        while len(self.buffer) >= 4:
            mid, length = struct.unpack("!HH", self.buffer[:4])
            if length < 2 or length > self.max_length:
                raise ProtocolError(f"invalid declared length: {length}")
            total = 4 + length
            if self.legacy_time_reply and mid == 0x0001 and length == 11:
                total = 14
            if len(self.buffer) < total:
                return
            raw = bytes(self.buffer[:total])
            del self.buffer[:total]
            frame = Frame(mid, length, raw[4:6], raw[6:], self.offset, raw)
            self.offset += total
            yield frame


def parse_ack_rule(text):
    try:
        mid, length = text.split(":")
        result = int(mid, 16), int(length)
    except (ValueError, TypeError) as exc:
        raise ValueError("ACK rule must be HEX_ID:BODY_LENGTH") from exc
    if not 0x0100 <= result[0] <= 0xFFFF or not 1 <= result[1] <= 65533:
        raise ValueError("ACK rule is outside the event range")
    return result
