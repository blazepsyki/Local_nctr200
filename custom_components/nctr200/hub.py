"""Local TCP server that the NCTR-200 controller connects to.

The controller is provisioned with this host as its server. It opens one TCP
connection, identifies itself with 0001 (or resumes without it after a server
restart), sends a 01FF state report about every 301 s and reports panel changes
as events. Every device event is acknowledged; commands are sent once and are
not retried automatically.

This module has no Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from . import messages as m
from . import provision
from .protocol import Decoder, Frame, ProtocolError, packet

_LOGGER = logging.getLogger(__name__)

ACK_TIMEOUT = 10.0
# A state report older than this makes the device unavailable (period is ~301 s).
STATE_TTL = 900.0
# Close a connection that has sent nothing at all. After a server restart the
# controller reconnects at once and may stay silent until it re-associates Wi-Fi.
SILENT_TIMEOUT = 180.0
# Close a connection that has been quiet this long after sending data.
IDLE_TIMEOUT = 900.0
WATCHDOG_INTERVAL = 30.0
# The manufacturer server answers 0001 and 0003 with YYMMDDhhmmss plus the
# milliseconds as two bytes, and declares length 11 although 10 bytes follow.
TIME_REPLY_DECLARED = 11


class NctrError(Exception):
    """Base error."""


class NotReadyError(NctrError):
    """No connected controller with a fresh state report."""


class CommandTimeoutError(NctrError):
    """The controller did not acknowledge a command."""


class ReservationUnknownError(NctrError):
    """Reservation mode was selected before its stop hours and run minutes are known."""


@dataclass
class DeviceState:
    """Latest known controller values. None means not reported yet."""

    power_on: bool | None = None
    mode: str | None = None
    overlay: str | None = None  # bath / hot_water_only, reported by events only
    flags: int | None = None
    room_current: int | None = None
    room_target: int | None = None
    ondol_target: int | None = None
    hot_water_target: int | None = None
    heating_water_current: int | None = None
    heating_water_target: int | None = None  # no report found yet; last command
    burner_level: int | None = None
    reservation: tuple[int, int] | None = None  # (stop hours, run minutes)
    schedule: str | None = None
    last_report: datetime | None = None


@dataclass
class _Pending:
    mid: int
    token: bytes
    future: asyncio.Future[None]
    power_on: bool = False


@dataclass
class Stats:
    """Counters shown in diagnostics."""

    connections: int = 0
    rejected: int = 0
    resumed: int = 0
    state_reports: int = 0
    keepalives: int = 0
    silent_closes: int = 0
    last_disconnect: str | None = None
    unknown_events: dict[str, int] = field(default_factory=dict)


class _Session:
    def __init__(
        self,
        hub: NctrHub,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        self.hub = hub
        self.reader = reader
        self.writer = writer
        self.peer = peer
        self.decoder = Decoder()
        self.identified = False
        self.resumed = False
        self.state_at: float | None = None
        self.closed = False

    def send(self, mid: int, token: bytes, body: bytes = b"", declared: int | None = None) -> None:
        if self.closed:
            raise NotReadyError("connection is closed")
        data = packet(mid, token, body, declared)
        _LOGGER.debug("tx %s", data.hex())
        self.writer.write(data)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.writer.close()

    async def run(self) -> str:
        loop = asyncio.get_running_loop()
        connected = last_rx = loop.time()
        received = False
        try:
            while not self.closed:
                deadline = (last_rx + IDLE_TIMEOUT) if received else (connected + SILENT_TIMEOUT)
                try:
                    async with asyncio.timeout(max(deadline - loop.time(), 0.05)):
                        data = await self.reader.read(8192)
                except TimeoutError:
                    return "idle_timeout" if received else "silent_timeout"
                if not data:
                    return "peer_closed"
                received = True
                last_rx = loop.time()
                _LOGGER.debug("rx %s", data.hex())
                for frame in self.decoder.feed(data):
                    self.hub.handle_frame(self, frame)
                    if self.closed:
                        break
                await self.writer.drain()
            return "closed"
        except ProtocolError as err:
            _LOGGER.warning("Closing controller connection after a framing error: %s", err)
            return "protocol_error"
        except (ConnectionError, OSError) as err:
            return f"connection_error: {err}"
        finally:
            self.close()


class NctrHub:
    """One controller, one connection at a time."""

    def __init__(
        self,
        host: str,
        port: int,
        allowed_client: str | None,
        *,
        now: Callable[[], datetime],
        node: bytes | None = None,
        on_node: Callable[[bytes], None] | None = None,
        advertised_ip: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        # None accepts any client; the listening port is the only gate then.
        self.allowed = (
            ipaddress.ip_network(allowed_client, strict=False) if allowed_client else None
        )
        self.advertised_ip = advertised_ip
        self._now = now
        self.node = node
        self._on_node = on_node
        self.state = DeviceState()
        self.stats = Stats()
        self.peer: str | None = None
        self._server: asyncio.Server | None = None
        self._session: _Session | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._listeners: list[Callable[[], None]] = []
        self._command_lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._tokens: set[bytes] = set()
        # Reservation values picked while not in reservation mode (stop h, run min).
        self._reservation_draft: list[int | None] = [None, None]
        self._ready_seen = False

    # ----- lifecycle -----

    async def async_start(self) -> None:
        """Bind the listening socket. Raises OSError when the port is taken."""
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port)
        self._spawn(self._watchdog())
        _LOGGER.info("Listening for the NCTR-200 controller on %s:%s", self.host, self.port)

    async def async_stop(self) -> None:
        if self._server is not None:
            self._server.close()
        if self._session is not None:
            self._session.close()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        self._server = None

    @property
    def bound_port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return int(self._server.sockets[0].getsockname()[1])

    def _spawn(self, coro: object) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----- listeners -----

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(callback)

        return remove

    def _notify(self) -> None:
        self._ready_seen = self.ready
        for callback in list(self._listeners):
            callback()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if self.ready != self._ready_seen:
                self._notify()

    # ----- connection state -----

    @property
    def connected(self) -> bool:
        return self._session is not None and not self._session.closed

    @property
    def ready(self) -> bool:
        session = self._session
        return (
            session is not None
            and not session.closed
            and (session.identified or session.resumed)
            and session.state_at is not None
            and asyncio.get_running_loop().time() - session.state_at <= STATE_TTL
        )

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        peer = str(peername[0]) if peername else "?"
        try:
            allowed = self.allowed is None or ipaddress.ip_address(peer) in self.allowed
        except ValueError:
            allowed = False
        if not allowed:
            self.stats.rejected += 1
            _LOGGER.warning("Rejected a connection from %s (not in %s)", peer, self.allowed)
            writer.close()
            return
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if self._session is not None and not self._session.closed:
            # The controller can drop a connection silently (Wi-Fi re-association);
            # its new connection replaces the old one.
            _LOGGER.info("New controller connection from %s replaces the old one", peer)
            self._session.close()
        session = _Session(self, reader, writer, peer)
        self._session = session
        self.peer = peer
        self.stats.connections += 1
        self._notify()
        reason = await session.run()
        if reason == "silent_timeout":
            self.stats.silent_closes += 1
        self.stats.last_disconnect = reason
        _LOGGER.info("Controller connection from %s ended: %s", peer, reason)
        if self._session is session:
            self._fail_pending()
            self._notify()

    # ----- frames -----

    def handle_frame(self, session: _Session, frame: Frame) -> None:
        mid, token, body = frame.mid, frame.token, frame.body
        if mid == m.MID_IDENTITY:
            self._on_identity(session, token, body)
        elif mid == m.MID_BOOTSTRAP:
            if len(body) == 4 and self.node is not None:
                session.send(mid, token, self.node)
        elif mid == m.MID_STATE:
            self._on_state(session, token, body)
        elif mid == m.MID_PAIR_CODE:
            _LOGGER.info("The controller asked for a pairing code; not answered")
        elif mid == m.MID_KEEPALIVE and not body:
            # Sent about every 60 min 7 s after 0001. The manufacturer server answers
            # with the time reply and the controller then sends it once. Other replies
            # make it repeat every 10 s until an event or command is exchanged.
            self.stats.keepalives += 1
            session.send(mid, token, self._time_reply(), TIME_REPLY_DECLARED)
        elif not body:
            pending = self._pending
            if pending and (mid, token) == (pending.mid, pending.token):
                if not pending.future.done():
                    pending.future.set_result(None)
            else:
                _LOGGER.debug("ACK for nothing we sent: %04X %s", mid, token.hex())
        else:
            self._on_event(session, mid, token, body)

    def _on_identity(self, session: _Session, token: bytes, body: bytes) -> None:
        if len(body) != 8:
            _LOGGER.warning("Closing a connection with an invalid identity (%d bytes)", len(body))
            session.close()
            return
        if self.node != body:
            if self.node is not None:
                _LOGGER.warning("The controller identity changed")
            self.node = body
            if self._on_node is not None:
                self._on_node(body)
        session.identified = True
        session.state_at = None
        session.send(m.MID_IDENTITY, token, self._time_reply(), TIME_REPLY_DECLARED)

    def _time_reply(self) -> bytes:
        now = self._now()
        clock = bytes((now.year % 100, now.month, now.day, now.hour, now.minute, now.second))
        return clock + (now.microsecond // 1000).to_bytes(2, "big")

    def _on_state(self, session: _Session, token: bytes, body: bytes) -> None:
        report = m.parse_state(body)
        if report is None:
            _LOGGER.warning("Ignoring a state report of %d bytes", len(body))
            return
        session.send(m.MID_STATE, token)
        if not session.identified and not session.resumed:
            # After a server restart the controller keeps its TCP session and sends
            # 01FF without a new 0001.
            session.resumed = True
            self.stats.resumed += 1
        session.state_at = asyncio.get_running_loop().time()
        self.stats.state_reports += 1
        s = self.state
        s.flags = report.flags
        s.power_on = m.power_of(report.flags)
        mode = m.mode_of(report.flags)
        if mode is not None:
            s.mode = mode
        if not s.power_on:
            s.overlay = None
        s.room_current = report.room_current
        s.room_target = report.room_target
        s.ondol_target = report.ondol_target
        s.hot_water_target = report.hot_water_target
        s.heating_water_current = report.heating_water_current
        s.burner_level = report.burner_level
        s.last_report = self._now()
        self._notify()

    def _on_event(self, session: _Session, mid: int, token: bytes, body: bytes) -> None:
        if mid in m.EVENT_IDS:
            session.send(mid, token)
        pending = self._pending
        if (
            pending is not None
            and pending.power_on
            and mid in m.MODE_REPORT_IDS
            and not pending.future.done()
        ):
            # Power on is answered by the resumed mode's report instead of an ACK.
            pending.future.set_result(None)
        s = self.state
        if len(body) == 6:
            if mid == m.MID_ROOM:
                s.room_current, s.room_target = body[2], body[4]
                self._set_mode(m.MODE_ROOM)
            elif mid == m.MID_ONDOL:
                s.heating_water_current, s.ondol_target = body[2], body[4]
                self._set_mode(m.MODE_ONDOL)
            elif mid == m.MID_AWAY:
                self._set_mode(m.MODE_AWAY)
            elif mid == m.MID_RESERVATION:
                s.reservation = (body[4], body[5])
                self._reservation_draft = [None, None]
                self._set_mode(m.MODE_RESERVATION)
            elif mid == m.MID_BATH:
                s.power_on, s.overlay = True, m.OVERLAY_BATH
            elif mid == m.MID_HOT_WATER_ONLY:
                s.power_on, s.overlay = True, m.OVERLAY_HOT_WATER_ONLY
                s.hot_water_target = body[4]
            elif mid == m.MID_HOT_WATER:
                s.hot_water_target = body[4]
            elif mid == m.MID_HEATING_WATER:
                s.heating_water_target = body[4]
            elif mid == m.MID_ROOM_TEMPERATURE:
                s.room_current = body[4]
            elif mid == m.MID_POWER and body[5] == 2:
                s.power_on, s.overlay = False, None
            else:
                self._unknown(mid, body)
                return
        elif mid == m.MID_SCHEDULE and len(body) == 49:
            s.schedule = m.slots_to_text(tuple(body[1:]))
        else:
            self._unknown(mid, body)
            return
        self._notify()

    def _set_mode(self, mode: str) -> None:
        self.state.mode = mode
        self.state.overlay = None
        self.state.power_on = True

    def _unknown(self, mid: int, body: bytes) -> None:
        key = f"{mid:04X}/{len(body)}"
        count = self.stats.unknown_events.get(key, 0) + 1
        self.stats.unknown_events[key] = count
        if count == 1:
            _LOGGER.info("Unknown controller event %04X %s (acknowledged)", mid, body.hex())

    # ----- commands -----

    def _new_token(self) -> bytes:
        if len(self._tokens) >= 0x10000:
            self._tokens.clear()
        while True:
            token = secrets.token_bytes(2)
            if token not in self._tokens:
                self._tokens.add(token)
                return token

    def _fail_pending(self) -> None:
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(NotReadyError("connection closed before the ACK"))

    async def _command(
        self,
        mid: int,
        body: bytes,
        apply: Callable[[DeviceState], None],
        *,
        power_on: bool = False,
    ) -> None:
        async with self._command_lock:
            session = self._session
            if session is None or not self.ready:
                raise NotReadyError("the controller is not connected or has not reported yet")
            token = self._new_token()
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._pending = _Pending(mid, token, future, power_on)
            try:
                session.send(mid, token, body)
                await session.writer.drain()
                async with asyncio.timeout(ACK_TIMEOUT):
                    await future
            except TimeoutError:
                raise CommandTimeoutError(
                    f"no ACK for {mid:04X} within {ACK_TIMEOUT:.0f} s"
                ) from None
            except (ConnectionError, OSError) as err:
                raise NotReadyError(f"send failed: {err}") from err
            finally:
                self._pending = None
            # The device ACKs remote commands without reporting the new value; the
            # next 01FF confirms or corrects this.
            apply(self.state)
            self._notify()

    async def async_set_room_target(self, target: int) -> None:
        def apply(s: DeviceState) -> None:
            s.room_target = target
            s.mode, s.overlay, s.power_on = m.MODE_ROOM, None, True

        await self._command(m.MID_ROOM, m.room_body(target), apply)

    async def async_set_power(self, on: bool) -> None:
        def apply(s: DeviceState) -> None:
            s.power_on = on
            if not on:
                s.overlay = None

        await self._command(m.MID_POWER, m.power_body(on), apply, power_on=on)

    async def async_set_ondol_target(self, target: int) -> None:
        def apply(s: DeviceState) -> None:
            s.ondol_target = target

        await self._command(m.MID_ONDOL, m.ondol_body(target), apply)

    async def async_select_ondol(self) -> None:
        target = self.state.ondol_target
        if target is None:
            raise NotReadyError("ondol target is not known yet")

        def apply(s: DeviceState) -> None:
            s.mode, s.overlay, s.power_on = m.MODE_ONDOL, None, True

        await self._command(m.MID_ONDOL, m.ondol_body(target), apply)

    async def async_select_room(self) -> None:
        target = self.state.room_target
        if target is None:
            raise NotReadyError("room target is not known yet")
        await self.async_set_room_target(target)

    async def async_set_hot_water_target(self, target: int) -> None:
        def apply(s: DeviceState) -> None:
            s.hot_water_target = target

        await self._command(m.MID_HOT_WATER, m.hot_water_body(target), apply)

    async def async_set_heating_water_target(self, target: int) -> None:
        def apply(s: DeviceState) -> None:
            s.heating_water_target = target

        await self._command(m.MID_HEATING_WATER, m.heating_water_body(target), apply)

    async def async_select_hot_water_only(self) -> None:
        target = self.state.hot_water_target
        if target is None:
            raise NotReadyError("hot water target is not known yet")

        def apply(s: DeviceState) -> None:
            s.overlay, s.power_on = m.OVERLAY_HOT_WATER_ONLY, True

        await self._command(m.MID_HOT_WATER_ONLY, m.hot_water_only_body(target), apply)

    async def async_select_bath(self) -> None:
        def apply(s: DeviceState) -> None:
            s.overlay, s.power_on = m.OVERLAY_BATH, True

        await self._command(m.MID_BATH, m.bath_body(), apply)

    async def async_select_away(self) -> None:
        def apply(s: DeviceState) -> None:
            s.mode, s.overlay, s.power_on = m.MODE_AWAY, None, True

        await self._command(m.MID_AWAY, m.away_body(), apply)

    async def async_set_reservation(self, stop_hours: int, run_minutes: int) -> None:
        body = m.reservation_body(stop_hours, run_minutes)

        def apply(s: DeviceState) -> None:
            s.reservation = (stop_hours, run_minutes)
            s.mode, s.overlay, s.power_on = m.MODE_RESERVATION, None, True
            self._reservation_draft = [None, None]

        await self._command(m.MID_RESERVATION, body, apply)

    def reservation_value(self, index: int) -> int | None:
        """Stop hours (0) or run minutes (1) to use: a value picked here, else the last
        one the controller reported or accepted."""
        draft = self._reservation_draft[index]
        if draft is not None:
            return draft
        reported = self.state.reservation
        return reported[index] if reported else None

    async def async_set_reservation_value(self, index: int, value: int) -> None:
        """Change stop hours (0) or run minutes (1).

        In reservation mode this sends 0108 at once. Otherwise the value is kept for
        the next time reservation mode is selected; the controller is not told.
        """
        low, high = (m.RESERVATION_STOP_RANGE, m.RESERVATION_RUN_RANGE)[index]
        if not low <= value <= high:
            raise ValueError(f"reservation value must be {low}..{high}: {value}")
        stop_hours, run_minutes = self.reservation_value(0), self.reservation_value(1)
        if index == 0:
            stop_hours = value
        else:
            run_minutes = value
        s = self.state
        in_reservation = s.mode == m.MODE_RESERVATION and s.overlay is None and s.power_on
        if in_reservation and stop_hours is not None and run_minutes is not None:
            await self.async_set_reservation(stop_hours, run_minutes)
            return
        self._reservation_draft[index] = value
        self._notify()

    def restore_reservation_value(self, index: int, value: int) -> None:
        """Take back a value picked before Home Assistant restarted."""
        if self.reservation_value(index) is None:
            self._reservation_draft[index] = value

    async def async_select_reservation(self) -> None:
        stop_hours, run_minutes = self.reservation_value(0), self.reservation_value(1)
        if stop_hours is None or run_minutes is None:
            raise ReservationUnknownError("reservation stop hours and run minutes are not set")
        await self.async_set_reservation(stop_hours, run_minutes)

    async def async_set_schedule(self, text: str) -> None:
        slots = m.parse_schedule(text)
        body = m.schedule_body(slots)

        def apply(s: DeviceState) -> None:
            s.schedule = m.slots_to_text(slots)

        await self._command(m.MID_SCHEDULE, body, apply)

    # ----- diagnostics -----

    def provision_target(self) -> dict[str, object]:
        """What the controller's Wi-Fi settings should point at."""
        ip = self.advertised_ip
        return {
            "server_ip": ip,
            "server_url": provision.server_url(ip) if ip else None,
            "port": self.bound_port or self.port,
        }

    def diagnostics(self) -> dict[str, object]:
        session = self._session
        return {
            "listening": {"host": self.host, "port": self.bound_port or self.port},
            "allowed_client": str(self.allowed) if self.allowed else "any",
            "provision": self.provision_target(),
            "connected": self.connected,
            "ready": self.ready,
            "identified": bool(session and session.identified),
            "resumed": bool(session and session.resumed),
            "node_known": self.node is not None,
            "state": {
                **{k: v for k, v in vars(self.state).items() if k != "last_report"},
                "flags_hex": None if self.state.flags is None else f"{self.state.flags:02X}",
                "last_report": self.state.last_report.isoformat()
                if self.state.last_report
                else None,
            },
            "stats": vars(self.stats),
        }
