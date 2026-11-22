"""Hub tests over a real TCP connection on localhost."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio

from custom_components.nctr200 import hub as hub_module
from custom_components.nctr200 import messages as m
from custom_components.nctr200.hub import CommandTimeoutError, NctrHub, NotReadyError
from custom_components.nctr200.protocol import Frame, packet

NODE = bytes.fromhex("0011223344556677")
NOW = datetime(2026, 11, 22, 19, 47, 27)


def state_body(flags: int = 0xC1, room_current: int = 23, room_target: int = 25) -> bytes:
    body = bytearray(103)
    body[0] = flags
    body[10] = 30
    body[14] = 45
    body[15] = 60
    body[16] = room_current
    body[32] = room_target
    body[62] = 2
    return bytes(body)


class Device:
    """A fake controller on the other end of the TCP connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.buffer = bytearray()
        self.inbox: list[Frame] = []

    def _parse(self) -> None:
        # The 0001 reply declares 11 but carries 10 bytes; the controller copes.
        while len(self.buffer) >= 4:
            mid, length = struct.unpack("!HH", self.buffer[:4])
            total = 14 if (mid, length) == (m.MID_IDENTITY, 11) else 4 + length
            if len(self.buffer) < total:
                return
            raw = bytes(self.buffer[:total])
            del self.buffer[:total]
            self.inbox.append(Frame(mid, length, raw[4:6], raw[6:]))

    async def send(self, mid: int, token: bytes, body: bytes = b"") -> None:
        self.writer.write(packet(mid, token, body))
        await self.writer.drain()

    async def receive(self, timeout: float = 2.0) -> Frame:
        async with asyncio.timeout(timeout):
            while not self.inbox:
                data = await self.reader.read(4096)
                if not data:
                    raise ConnectionError("server closed")
                self.buffer.extend(data)
                self._parse()
        return self.inbox.pop(0)

    async def closed(self, timeout: float = 2.0) -> bool:
        async with asyncio.timeout(timeout):
            return await self.reader.read(4096) == b""

    async def handshake(self) -> None:
        await self.send(m.MID_IDENTITY, b"\xc3\x18", NODE)
        reply = await self.receive()
        assert reply.mid == m.MID_IDENTITY
        await self.send(m.MID_STATE, b"\x46\x6e", state_body())
        ack = await self.receive()
        assert (ack.mid, ack.token, ack.body) == (m.MID_STATE, b"\x46\x6e", b"")


@pytest_asyncio.fixture
async def hub() -> AsyncIterator[NctrHub]:
    learned: list[bytes] = []
    instance = NctrHub("127.0.0.1", 0, "127.0.0.1", now=lambda: NOW, on_node=learned.append)
    instance.learned = learned  # type: ignore[attr-defined]
    await instance.async_start()
    try:
        yield instance
    finally:
        await instance.async_stop()


async def connect(hub: NctrHub) -> Device:
    reader, writer = await asyncio.open_connection("127.0.0.1", hub.bound_port)
    return Device(reader, writer)


async def wait_for(condition, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_handshake_time_reply_and_state(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.send(m.MID_IDENTITY, b"\xc3\x18", NODE)
    reply = await device.receive()
    # Length field 11 although 10 bytes follow, exactly as the manufacturer sends it.
    assert struct.unpack("!H", packet(1, b"\0\0", b"", 11)[2:4])[0] == 11
    assert reply.declared == 11
    assert reply.token == b"\xc3\x18"
    assert reply.body == bytes((26, 11, 22, 19, 47, 27, 0x01, 0x23))[: len(reply.body)]
    assert hub.learned == [NODE]  # type: ignore[attr-defined]
    assert not hub.ready
    await device.send(m.MID_STATE, b"\x46\x6e", state_body())
    ack = await device.receive()
    assert (ack.mid, ack.body) == (m.MID_STATE, b"")
    assert hub.ready
    s = hub.state
    assert (s.power_on, s.mode, s.room_current, s.room_target) == (True, "room", 23, 25)
    assert (s.ondol_target, s.hot_water_target, s.heating_water_current) == (45, 60, 30)


async def test_bootstrap_answered_with_known_node(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.handshake()
    await device.send(m.MID_BOOTSTRAP, b"\x00\x01", bytes.fromhex("31010201"))
    reply = await device.receive()
    assert (reply.mid, reply.body) == (m.MID_BOOTSTRAP, NODE)


async def test_resumed_session_without_identity(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.send(m.MID_STATE, b"\x01\x02", state_body(flags=0xD0))
    await device.receive()
    assert hub.ready
    assert hub.stats.resumed == 1
    assert hub.state.mode == "ondol"


async def test_events_are_acknowledged_and_update_state(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.handshake()
    events = [
        (m.MID_ROOM_TEMPERATURE, bytes((1, 0, 0, 0, 22, 0))),
        (m.MID_ONDOL, bytes((0, 0, 0x1E, 0, 0x2D, 0))),
        (m.MID_RESERVATION, bytes((1, 0, 0, 0, 3, 20))),
        (m.MID_BATH, bytes((0, 0, 0, 0, 0x3C, 0))),
        (0x0133, b"\x09"),  # unknown event in the event range: still acknowledged
    ]
    for i, (mid, body) in enumerate(events):
        token = bytes((0x10, i))
        await device.send(mid, token, body)
        ack = await device.receive()
        assert (ack.mid, ack.token, ack.body) == (mid, token, b"")
    s = hub.state
    assert s.room_current == 22
    assert s.heating_water_current == 30
    assert s.reservation == (3, 20)
    assert (s.mode, s.overlay) == ("reservation", "bath")
    assert hub.stats.unknown_events == {"0133/1": 1}
    await device.send(m.MID_POWER, b"\x20\x00", bytes((1, 0, 0, 0, 0, 2)))
    await device.receive()
    assert (s.power_on, s.overlay) == (False, None)


async def test_keepalive_is_echoed(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.handshake()
    await device.send(m.MID_KEEPALIVE, b"\xae\x43")
    echo = await device.receive()
    assert (echo.mid, echo.token, echo.body) == (m.MID_KEEPALIVE, b"\xae\x43", b"")
    assert hub.stats.keepalives == 1


async def test_command_ack_applies_value(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.handshake()
    task = asyncio.create_task(hub.async_set_ondol_target(50))
    command = await device.receive()
    assert (command.mid, command.body) == (m.MID_ONDOL, bytes((0, 0, 0, 0, 50, 0)))
    assert hub.state.ondol_target == 45
    await device.send(command.mid, command.token)
    await task
    assert hub.state.ondol_target == 50


async def test_power_on_is_confirmed_by_mode_report(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.handshake()
    task = asyncio.create_task(hub.async_set_power(True))
    command = await device.receive()
    assert command.body == bytes((1, 0, 0, 0, 0, 1))
    await device.send(m.MID_ONDOL, b"\x55\x55", bytes((0, 0, 0x1E, 0, 0x2D, 0)))
    await task
    assert hub.state.power_on is True
    assert hub.state.mode == "ondol"


async def test_command_without_ack_times_out(hub: NctrHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_module, "ACK_TIMEOUT", 0.2)
    device = await connect(hub)
    await device.handshake()
    with pytest.raises(CommandTimeoutError):
        await hub.async_set_hot_water_target(50)
    assert hub.state.hot_water_target == 60
    # A late ACK is ignored, and the next command still works.
    late = await device.receive()
    await device.send(late.mid, late.token)
    task = asyncio.create_task(hub.async_select_away())
    command = await device.receive()
    assert command.mid == m.MID_AWAY
    await device.send(command.mid, command.token)
    await task
    assert hub.state.mode == "away"


async def test_commands_refused_until_ready(hub: NctrHub) -> None:
    with pytest.raises(NotReadyError):
        await hub.async_set_room_target(22)
    device = await connect(hub)
    await device.send(m.MID_IDENTITY, b"\x00\x01", NODE)
    await device.receive()
    with pytest.raises(NotReadyError):
        await hub.async_set_room_target(22)


async def test_out_of_range_value_is_rejected_before_sending(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.handshake()
    with pytest.raises(ValueError, match=r"10..45"):
        await hub.async_set_room_target(50)
    with pytest.raises(ValueError, match="30-minute"):
        await hub.async_set_schedule("00:00-09:15")


async def test_schedule_and_reservation_bodies(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.handshake()
    task = asyncio.create_task(hub.async_set_schedule("00:00-09:00,18:00-24:00"))
    command = await device.receive()
    assert command.mid == m.MID_SCHEDULE
    assert command.body == bytes((1, *([1] * 18), *([0] * 18), *([1] * 12)))
    await device.send(command.mid, command.token)
    await task
    assert hub.state.schedule == "00:00-09:00,18:00-24:00"
    task = asyncio.create_task(hub.async_set_reservation(3, 20))
    command = await device.receive()
    assert command.body == bytes((1, 0, 0, 0, 3, 20))
    await device.send(command.mid, command.token)
    await task
    assert (hub.state.mode, hub.state.reservation) == ("reservation", (3, 20))


async def test_disconnect_fails_pending_command(hub: NctrHub) -> None:
    device = await connect(hub)
    await device.handshake()
    task = asyncio.create_task(hub.async_select_bath())
    await device.receive()
    device.writer.close()
    with pytest.raises(NotReadyError):
        await task
    await wait_for(lambda: not hub.connected)
    assert not hub.ready


async def test_new_connection_replaces_old(hub: NctrHub) -> None:
    first = await connect(hub)
    await first.handshake()
    second = await connect(hub)
    assert await first.closed()
    await second.handshake()
    assert hub.ready
    assert hub.stats.connections == 2


async def test_silent_connection_is_closed(hub: NctrHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_module, "SILENT_TIMEOUT", 0.2)
    device = await connect(hub)
    assert await device.closed()
    await wait_for(lambda: hub.stats.silent_closes == 1)


async def test_client_outside_allowed_network_is_rejected() -> None:
    hub = NctrHub("127.0.0.1", 0, "10.0.0.0/8", now=lambda: NOW)
    await hub.async_start()
    try:
        device = await connect(hub)
        assert await device.closed()
        assert hub.stats.rejected == 1
        assert not hub.connected
    finally:
        await hub.async_stop()


async def test_state_expires(hub: NctrHub, monkeypatch: pytest.MonkeyPatch) -> None:
    device = await connect(hub)
    await device.handshake()
    monkeypatch.setattr(hub_module, "STATE_TTL", 0.05)
    await asyncio.sleep(0.1)
    assert not hub.ready
