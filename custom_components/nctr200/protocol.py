"""NCTR-200 wire framing.

Frame: ID 2B | Length 2B (token through end) | Token 2B | Body.
An ACK repeats the ID and token with an empty body.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

MAX_LENGTH = 65535


class ProtocolError(ValueError):
    """The byte stream cannot be framed."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded frame."""

    mid: int
    declared: int
    token: bytes
    body: bytes


def packet(mid: int, token: bytes, body: bytes = b"", declared: int | None = None) -> bytes:
    """Build a frame. ``declared`` overrides the length field (see the 0001 reply)."""
    if len(token) != 2 or not 0 <= mid <= 0xFFFF:
        raise ValueError("invalid message ID or token")
    length = len(body) + 2 if declared is None else declared
    if not 2 <= length <= MAX_LENGTH:
        raise ValueError("invalid declared length")
    return struct.pack("!HH", mid, length) + token + body


class Decoder:
    """Reassemble frames from TCP chunks."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[Frame]:
        """Yield every complete frame; keep a partial one for the next chunk."""
        self.buffer.extend(data)
        while len(self.buffer) >= 4:
            mid, length = struct.unpack("!HH", self.buffer[:4])
            if length < 2:
                raise ProtocolError(f"invalid declared length: {length}")
            total = 4 + length
            if len(self.buffer) < total:
                return
            raw = bytes(self.buffer[:total])
            del self.buffer[:total]
            yield Frame(mid, length, raw[4:6], raw[6:])
