"""Single-device collector with explicit, bounded command handling."""

from dataclasses import asdict, dataclass
from datetime import datetime
import secrets
import ipaddress
import math
import socket
import threading
import time
import uuid

from .messages import APK_HINTS, COMMANDS, decode, novelty
from .protocol import Decoder, ProtocolError, packet
from .recording import KST, RecordingError, stamp

DEFAULT_ACK_RULES = ((0x0101, 6), (0x0102, 6), (0x0104, 6), (0x0105, 6),
                     (0x0106, 6), (0x0108, 6), (0x010A, 6), (0x0115, 6))
# Device-originated event IDs. The device retransmits an unacknowledged event every
# 3-5 s and holds back later events until it is acknowledged (2026-11-20/21 runs),
# so unlisted events in this range are acknowledged by default as well.
EVENT_ID_RANGE = range(0x0100, 0x01FF)
COMMAND_MIDS = {c.mid for c in COMMANDS.values()}
# Reports that name the active heating mode; one of them follows a power ON.
MODE_REPORT_MIDS = {0x0102, 0x0104, 0x0105, 0x0106, 0x0114}
KEEPALIVE_REPLIES = ("none", "echo", "time", "time6", "identity", "probe")
# probe: each strategy answers this many consecutive 0003s, then the next one.
KEEPALIVE_PROBE = ("time", "time6", "identity", "echo")
KEEPALIVE_PROBE_STEP = 3
# A 0003 more than this many seconds after the previous one starts a new run.
KEEPALIVE_RUN_GAP = 30.0



@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 20000
    allowed_client_ip: str = "127.0.0.1"
    mode: str = "observe"
    time_tail: bytes = b""
    time_length: str = "legacy-11"
    state_length: int = 103
    state_ttl: float = 900.0
    ack_timeout: float = 10.0
    report_timeout: float = 720.0
    ack_rules: tuple = DEFAULT_ACK_RULES
    policy_note: str = "v4-observed-events"
    enable_temperature: bool = False
    temp_min: int = 18
    temp_max: int = 30
    enable_power: bool = False
    # Remote commands added 2026-11-22. Formats are in messages.COMMANDS; ranges
    # default to the NCTR-200 manual (hot water 35-60, ondol 45-85).
    enable_hot_water: bool = False
    hot_water_min: int = 35
    hot_water_max: int = 60
    enable_bath: bool = False
    enable_away: bool = False
    enable_hot_water_only: bool = False   # format inferred from the 0114 report
    enable_reservation: bool = False      # heating reservation, format from the 0108 report
    enable_heating_water: bool = False    # 0103, manufacturer app format (boiler6)
    enable_schedule: bool = False         # 0107 24-hour reservation (boiler6)
    enable_ondol: bool = False       # format inferred, not seen from the manufacturer
    ondol_min: int = 45
    ondol_max: int = 85
    pair_code: int | None = None
    expected_node: bytes | None = None
    # The device can abandon a TCP session silently (Wi-Fi re-association without
    # FIN/RST). A new connection from the allowed peer then replaces the old one.
    replace_session: bool = True
    # Acknowledge device events in EVENT_ID_RANGE even when no explicit rule matches.
    ack_unlisted_events: bool = True
    # The device sends an empty 0003 every 10 s starting 60 min 8 s after the
    # handshake and again every 60 min 8 s (field-20261122-30: 04:16:54 -> 05:17:02;
    # -31: 12:01:29 -> 13:01:37 -> 14:01:45). An echoed empty 0003 does not stop the
    # repetition (-31: 803 echoes from 14:01:45); it stopped only when the device sent
    # an event of its own (010A 13:01:48, 0102 16:15:46). Server commands (0104 at
    # 12:08) did not delay the start. The manufacturer's reply has never been captured.
    # keepalive_reply: none | echo | time | time6 | identity | probe
    #   time     0003 with the 8-byte 0001 reply body (YYMMDDhhmmss + time tail)
    #   time6    0003 with the 6-byte date/time only
    #   identity a 0001 time reply carrying the 0003 token
    #   probe    within one run of 0003s, KEEPALIVE_PROBE strategies in order
    keepalive_reply: str = "echo"
    # Novel messages with the same ID and reasons: detailed records for the first N,
    # then only counts (milestones 10/100/1000 and a final total at stop).
    novel_detail_limit: int = 5
    # Close a session that has received nothing for this many seconds (0 disables).
    idle_timeout: float = 900.0
    # Fail-safe: close a connection that has sent nothing at all within this many seconds
    # (0 disables). After a collector restart the device reconnects at once and may stay
    # silent; it usually re-associates Wi-Fi after about 130 s (field-20261121-97, 98,
    # 20261120-02) but once stayed silent for more than 7 min (field-20261122-23).
    # A connection that sends frames without 0001 is a resumed session and is kept:
    # closing one by the missing 0001 alone lost the device for minutes (field-20261122-24).
    silent_timeout: float = 180.0
    # A valid 01FF without a preceding 0001 on the connection counts as a resumed session.
    accept_resumed_session: bool = True
    # Consecutive identity timeouts before a "re-associate Wi-Fi" advisory is logged.
    silent_close_alert: int = 3

    def validate(self):
        ipaddress.IPv4Address(self.bind)
        ipaddress.IPv4Network(self.allowed_client_ip, strict=False)
        if not 0 <= self.port <= 65535 or self.mode not in ("observe", "handshake"):
            raise ValueError("invalid port or mode")
        if self.mode == "handshake" and len(self.time_tail) != 2:
            raise ValueError("handshake requires a two-byte time tail")
        if self.keepalive_reply not in KEEPALIVE_REPLIES:
            raise ValueError("invalid keepalive reply")
        if self.time_length not in ("legacy-11", "actual"):
            raise ValueError("invalid time response policy")
        if not 1 <= self.state_length <= 65533:
            raise ValueError("invalid state profile length")
        if any(not math.isfinite(v) or v <= 0 for v in
               (self.state_ttl, self.ack_timeout, self.report_timeout)):
            raise ValueError("timeouts must be positive")
        if not math.isfinite(self.silent_timeout) or self.silent_timeout < 0:
            raise ValueError("silent timeout must be zero (disabled) or positive")
        if not math.isfinite(self.idle_timeout) or self.idle_timeout < 0:
            raise ValueError("idle timeout must be zero (disabled) or positive")
        if not 0 <= self.temp_min <= self.temp_max <= 255:
            raise ValueError("invalid temperature bounds")
        if not 35 <= self.hot_water_min <= self.hot_water_max <= 60:
            raise ValueError("hot water bounds must stay within 35..60")
        if not 45 <= self.ondol_min <= self.ondol_max <= 85:
            raise ValueError("ondol bounds must stay within 45..85")
        if self.any_control() and self.mode != "handshake":
            raise ValueError("control requires handshake mode")
        if self.expected_node is not None and len(self.expected_node) != 8:
            raise ValueError("expected node must be eight bytes")
        if self.pair_code is not None and not 0 <= self.pair_code <= 9999:
            raise ValueError("pair code must be 0..9999")
        for mid, length in self.ack_rules:
            if not 0x0100 <= mid <= 65535 or not 1 <= length <= 65533:
                raise ValueError("invalid ACK rule")

    def allows(self, address):
        """allowed_client_ip is one address or a network such as 192.168.1.0/24."""
        network = ipaddress.IPv4Network(self.allowed_client_ip, strict=False)
        return ipaddress.IPv4Address(address) in network

    def any_control(self):
        return any(getattr(self, c.enable_flag) for c in COMMANDS.values())

    def bounds(self, feature):
        return {"temperature": (self.temp_min, self.temp_max),
                "hot_water": (self.hot_water_min, self.hot_water_max),
                "ondol": (self.ondol_min, self.ondol_max),
                "hot_water_only": (self.hot_water_min, self.hot_water_max),
                "power": (1, 2), "bath": (1, 1), "away": (1, 1),
                # (stop hours, run minutes): manual ranges 1-24 h and 10-90 min
                "reservation": ((1, 10), (24, 90)),
                "heating_water": (self.ondol_min, self.ondol_max),
                "schedule": (0, 1)}[feature]

    def manifest(self):
        value = asdict(self)
        value["time_tail"] = self.time_tail.hex()
        value["expected_node"] = "configured" if self.expected_node else None
        value["pair_code"] = "configured" if self.pair_code is not None else None
        return value


@dataclass
class Pending:
    command_id: str
    mid: int
    token: bytes
    requested: int
    feature: str
    sent_at: float
    acked: bool = False
    report_match: bool = False
    lcd_match: bool = False


class Session:
    def __init__(self, owner, sock, peer):
        self.owner, self.sock, self.peer = owner, sock, peer
        self.config, self.recorder = owner.config, owner.recorder
        self.id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.alive = True
        self.got_identity = False
        self.resumed = False
        self.state_at = None
        self.last_rx = None
        self.connected_at = None
        self.state_count = 0
        self.last_state = None
        self.decoder = Decoder()
        self.offsets = {"rx": 0, "tx": 0}
        self.pending = None
        self.tokens = set()
        self.thread = None
        self.keepalive_run = None   # {"started", "last", "count", "strategy"}
        self.close_reason = "peer_eof"
        self.recent = []

    def event(self, kind, **fields):
        return self.recorder.emit(kind, self.id, **fields)

    def ready(self):
        return (self.alive and (self.got_identity or self.resumed) and self.state_at is not None
                and self.owner.clock() - self.state_at <= self.config.state_ttl
                and not self.recorder.failed)

    def raw(self, direction, data, **fields):
        event = self.recorder.emit("socket_bytes", self.id, channel="raw",
                                   direction=direction, offset=self.offsets[direction],
                                   data_hex=data.hex(), **fields)
        self.offsets[direction] += len(data)
        return event["event_seq"]

    def _time_body(self):
        now = datetime.now(KST)
        return bytes((now.year % 100, now.month, now.day, now.hour,
                      now.minute, now.second)) + self.config.time_tail

    def _keepalive(self, token):
        self.owner.keepalive_seen()
        now = self.owner.clock()
        run = self.keepalive_run
        if run is None or now - run["last"] > KEEPALIVE_RUN_GAP:
            if run is not None:
                self._end_keepalive_run(None)
            run = self.keepalive_run = {"started": now, "last": now, "count": 0,
                                        "strategy": None}
        run["count"] += 1
        run["last"] = now
        strategy = self.config.keepalive_reply
        if strategy == "probe":
            step = (run["count"] - 1) // KEEPALIVE_PROBE_STEP
            strategy = KEEPALIVE_PROBE[min(step, len(KEEPALIVE_PROBE) - 1)]
        run["strategy"] = strategy
        self.event("keepalive", token=token.hex(), reply=strategy,
                   answered=strategy != "none", run_count=run["count"])
        if strategy == "echo":
            self.send(packet(0x0003, token))
        elif strategy == "time":
            self.send(packet(0x0003, token, self._time_body()))
        elif strategy == "time6":
            self.send(packet(0x0003, token, self._time_body()[:6]))
        elif strategy == "identity":
            declared = 11 if self.config.time_length == "legacy-11" else None
            self.send(packet(0x0001, token, self._time_body(), declared))

    def _end_keepalive_run(self, next_mid):
        """Log a finished run of 0003s with the reply that was in use at its end."""
        run = self.keepalive_run
        if run is None:
            return
        quiet = self.owner.clock() - run["last"]
        if next_mid is not None and quiet <= KEEPALIVE_RUN_GAP:
            # Another frame arrived inside the run; only a gap ends it.
            if next_mid == "01FF":
                return
            self.keepalive_run = None
            self.event("keepalive_run_end", count=run["count"], last_reply=run["strategy"],
                       duration=run["last"] - run["started"], ended_by=next_mid,
                       quiet=quiet)
            return
        self.keepalive_run = None
        self.event("keepalive_run_end", count=run["count"], last_reply=run["strategy"],
                   duration=run["last"] - run["started"], ended_by="gap", quiet=quiet,
                   next_mid=next_mid)

    def send(self, data, *, command_id=None):
        if not self.alive or self.recorder.failed:
            return False
        raw_seq = self.raw("tx", data, command_id=command_id, phase="attempt")
        self.event("tx_attempt", command_id=command_id, raw_seq=raw_seq,
                   mid=f"{int.from_bytes(data[:2], 'big'):04X}",
                   token=data[4:6].hex(), declared=int.from_bytes(data[2:4], "big"),
                   actual_length=len(data), data_hex=data.hex(), body_hex=data[6:].hex())
        try:
            self.sock.sendall(data)
        except OSError as exc:
            self.event("tx_failed", command_id=command_id, error=str(exc),
                       partial_send_possible=True)
            self.close("send_failed")
            return False
        self.event("tx_complete", command_id=command_id, raw_seq=raw_seq)
        return True

    def handle(self, frame):
        self.last_rx = self.owner.clock()
        self.event("frame_rx", mid=f"{frame.mid:04X}", declared=frame.declared,
                   token=frame.token.hex(), body_hex=frame.body.hex(),
                   body_length=len(frame.body), stream_offset=frame.offset)
        mid, token, body = frame.mid, frame.token, frame.body
        if mid != 0x0003:
            self._end_keepalive_run(f"{mid:04X}")
        self._interpret(mid, token, body)
        if mid == 0x0001:
            if len(body) != 8:
                self.event("identity_invalid", body_length=len(body))
                self.close("invalid_identity")
                return
            if not self.owner.accept_identity(body):
                self.event("identity_mismatch")
                self.close("identity_mismatch")
                return
            self.got_identity = True
            self.owner.identity_seen()
            self.state_at = None
            self.event("identity_learned", identity_mask=body[:2].hex() + "**********" + body[-1:].hex())
        if self.config.mode == "observe":
            return
        if mid == 0x0000:
            if len(body) == 4 and self.owner.node is not None:
                self.send(packet(mid, token, self.owner.node))
            else:
                self.event("bootstrap_unavailable", body_length=len(body))
        elif mid == 0x0001:
            now = datetime.now(KST)
            reply = bytes((now.year % 100, now.month, now.day, now.hour,
                           now.minute, now.second)) + self.config.time_tail
            declared = 11 if self.config.time_length == "legacy-11" else None
            self.send(packet(mid, token, reply, declared))
        elif mid == 0x01FF:
            self.state_count += 1
            if len(body) != self.config.state_length:
                self.state_at = None
                self.event("state_profile_mismatch", body_length=len(body),
                           expected=self.config.state_length)
                return
            changes = None
            if self.last_state is not None:
                changes = [{"offset": i, "before": before, "after": after}
                           for i, (before, after) in enumerate(zip(self.last_state, body))
                           if before != after]
            self.last_state = body
            self.state_at = self.owner.clock()
            if not self.got_identity and not self.resumed and self.config.accept_resumed_session:
                self.resumed = True
                self.event("session_resumed", reason="valid 01FF without 0001 on this connection",
                           identity_pinned=self.owner.node is not None)
                self.owner.identity_seen()
            self.event("state_report", body_length=len(body), changes=changes,
                       ready=self.ready())
            self.send(packet(mid, token))
            pending = self.pending
            offset = COMMANDS[pending.feature].state_offset if pending else None
            if offset is not None:
                pending.report_match = body[offset] == pending.requested
                self.event("command_report_candidate", channel="commands",
                           command_id=pending.command_id, reported=body[offset],
                           matches=pending.report_match, source=f"01FF[{offset}]")
                self._complete_if_observed()
        elif mid == 0x0002:
            self.event("pair_code_request", response_configured=self.config.pair_code is not None)
            if self.config.pair_code is not None and not body:
                reply = self.config.pair_code.to_bytes(2, "big") + b"\x07\x08"
                self.send(packet(mid, token, reply))
        elif mid == 0x0003 and not body:
            self._keepalive(token)
        elif not body:
            if self.pending and (mid, token) == (self.pending.mid, self.pending.token):
                self.pending.acked = True
                self.event("command_ack", channel="commands", command_id=self.pending.command_id,
                           delay=self.owner.clock() - self.pending.sent_at)
                self._complete_if_observed()
            else:
                self.event("unmatched_ack", mid=f"{mid:04X}", token=token.hex())
                self.owner.note_novel(self, mid, token, body, ["unmatched_ack"])
        else:
            # A matching token does not turn a payload-bearing event into an ACK.
            if mid == 0x0102 and len(body) == 6:
                self.event("temperature_candidate", current_candidate=body[2],
                           target=body[4], owner_field=body[1], mode_field=body[3],
                           interpretation="room current (byte2) and target (byte4), LCD-confirmed")
            pending = self.pending
            if (pending and pending.feature == "power" and pending.requested == 1
                    and pending.acked is not True and mid in MODE_REPORT_MIDS and len(body) == 6):
                # Power ON is answered by the resumed mode's report instead of an ACK
                # (field-20261122-21 01:04:49; the panel ON button behaves the same).
                pending.acked = True
                pending.report_match = True
                self.event("command_ack", channel="commands", command_id=pending.command_id,
                           delay=self.owner.clock() - pending.sent_at, implicit=True,
                           via=f"{mid:04X}")
                self.event("command_report_candidate", channel="commands",
                           command_id=pending.command_id, reported=1, matches=True,
                           source=f"{mid:04X} mode report after power on")
                self._complete_if_observed()
            elif pending and len(body) == 6 and mid == COMMANDS[pending.feature].report_mid:
                reported = COMMANDS[pending.feature].reported(body)
                pending.report_match = reported == pending.requested
                self.event("command_report_candidate", channel="commands",
                           command_id=pending.command_id, reported=reported,
                           matches=pending.report_match)
                self._complete_if_observed()
            if (mid, len(body)) in self.config.ack_rules:
                self.send(packet(mid, token))
            elif self.config.ack_unlisted_events and mid in EVENT_ID_RANGE:
                # Recorded as unknown first so the policy decision is visible in the log.
                self.event("unknown_event", mid=f"{mid:04X}", body_length=len(body),
                           acknowledged=True, policy="ack_unlisted_events")
                self.send(packet(mid, token))
            else:
                self.event("unknown_event", mid=f"{mid:04X}", body_length=len(body),
                           acknowledged=False)

    def _interpret(self, mid, token, body):
        """Decode known fields and divert anything unlike past recordings to novel.*.jsonl."""
        self.recent.append({"mid": f"{mid:04X}", "token": token.hex(), "body_hex": body.hex(),
                            "at": self.owner.clock()})
        del self.recent[:-6]
        if not body and mid != 0x0002:
            return          # an ACK; matched to a command or logged once as unmatched_ack
        values = decode(mid, body)
        if values:
            self.event("state_decoded", mid=f"{mid:04X}", values=values)
            self.owner.update_device(mid, values)
        reasons = novelty(mid, body)
        if reasons:
            self.owner.note_novel(self, mid, token, body, reasons)

    def _complete_if_observed(self):
        pending = self.pending
        if pending and pending.report_match and pending.lcd_match:
            self.event("command_observed_match", channel="commands",
                       command_id=pending.command_id, acked=pending.acked,
                       scope="operator LCD and candidate event agree; not a safety guarantee")
            self.pending = None

    def _new_token(self):
        if len(self.tokens) >= 65536:
            raise ValueError("token space exhausted; start a new session")
        start = int.from_bytes(secrets.token_bytes(2), "big")
        token = next(((start + i) % 65536).to_bytes(2, "big")
                     for i in range(65536)
                     if ((start + i) % 65536).to_bytes(2, "big") not in self.tokens)
        self.tokens.add(token)
        return token

    def tick(self):
        pending = self.pending
        if not pending:
            return
        elapsed = self.owner.clock() - pending.sent_at
        if pending.acked is False and elapsed >= self.config.ack_timeout:
            self.event("command_ack_timeout", channel="commands", command_id=pending.command_id)
            # Keep the command reserved while waiting for a report; never retry automatically.
            pending.acked = None
        if elapsed >= self.config.report_timeout:
            self.event("command_confirmation_timeout", channel="commands",
                       command_id=pending.command_id, report_match=pending.report_match,
                       lcd_match=pending.lcd_match)
            self.pending = None

    def command(self, feature, value, *, confirmed=False):
        with self.lock:
            self.tick()
            if not confirmed:
                raise ValueError("explicit SEND confirmation is required")
            if not self.ready():
                raise ValueError("current session has no fresh valid state")
            if self.pending:
                if self.pending.acked is not True:
                    raise ValueError("the previous command is not acknowledged yet; "
                                     "wait for its ACK or cancel it first")
                # The device never reports a remotely set value itself, so an
                # acknowledged command would otherwise block for report_timeout.
                self.event("command_superseded", channel="commands",
                           command_id=self.pending.command_id, by_feature=feature,
                           report_match=self.pending.report_match,
                           lcd_match=self.pending.lcd_match)
                self.pending = None
            spec = COMMANDS.get(feature)
            if spec is None:
                raise ValueError("unsupported command feature")
            if not getattr(self.config, spec.enable_flag):
                raise ValueError(f"{feature} control is disabled")
            low, high = self.config.bounds(feature)
            if feature == "reservation":
                if (not isinstance(value, tuple) or len(value) != 2
                        or not all(isinstance(v, int) and lo <= v <= hi
                                   for v, lo, hi in zip(value, low, high))):
                    raise ValueError("reservation needs stop hours 1..24 and run minutes 10..90")
            elif feature == "schedule":
                if (not isinstance(value, tuple) or len(value) != 48
                        or any(v not in (0, 1) for v in value)):
                    raise ValueError("schedule needs 48 half-hour slots of 0/1")
            elif not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{feature} value is outside the configured range {low}..{high}")
            mid, body = spec.mid, spec.body(value)
            token = self._new_token()
            command_id = uuid.uuid4().hex
            self.event("command_requested", channel="commands", command_id=command_id,
                       feature=feature, requested=value, token=token.hex(), mid=f"{mid:04X}",
                       body_hex=body.hex(), format_source=spec.format_source)
            self.pending = Pending(command_id, mid, token, value, feature, self.owner.clock())
            if not self.send(packet(mid, token, body), command_id=command_id):
                self.pending = None
                raise OSError("send failed; partial transmission is possible")
            return command_id

    def confirm_lcd(self, command_id, value):
        with self.lock:
            self.tick()
            if not self.pending or self.pending.command_id != command_id:
                raise ValueError("command is no longer pending on this connection")
            self.pending.lcd_match = value == self.pending.requested
            self.event("lcd_observation", channel="commands", command_id=command_id,
                       value=value, matches=self.pending.lcd_match, source="operator")
            self._complete_if_observed()

    def cancel(self, reason="operator"):
        with self.lock:
            if self.pending:
                self.event("command_cancelled", channel="commands",
                           command_id=self.pending.command_id, reason=reason,
                           transmitted_command_not_reverted=True)
                self.pending = None

    def close(self, reason="operator"):
        with self.lock:
            if not self.alive:
                return
            self.alive = False
            self.close_reason = reason
            try:
                if not self.recorder.failed:
                    self.cancel(reason)
            finally:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()

    def _silent_expired(self):
        if (not self.config.silent_timeout or self.last_rx is not None
                or self.connected_at is None):
            return False
        return self.owner.clock() - self.connected_at >= self.config.silent_timeout

    def _idle_expired(self):
        if not self.config.idle_timeout or self.connected_at is None:
            return False
        last = self.last_rx if self.last_rx is not None else self.connected_at
        return self.owner.clock() - last >= self.config.idle_timeout

    def serve(self):
        try:
            self._serve()
        except (RecordingError, OSError) as exc:
            self.owner.abort(str(exc))

    def _serve(self):
        try:
            self.sock.settimeout(0.25)
            self.connected_at = self.owner.clock()
            self.event("connected", peer=list(self.peer))
            while self.alive and not self.owner.stopping.is_set():
                try:
                    data = self.sock.recv(8192)
                except socket.timeout:
                    with self.lock:
                        self.tick()
                        if self._silent_expired():
                            self.event("silent_timeout", seconds=self.config.silent_timeout)
                            self.close("silent_timeout")
                            self.owner.silent_closed(self)
                        elif self._idle_expired():
                            self.event("idle_timeout", idle_seconds=self.config.idle_timeout,
                                       received_any=self.last_rx is not None)
                            self.close("idle_timeout")
                    continue
                if not data:
                    break
                with self.lock:
                    self.raw("rx", data)
                    try:
                        for frame in self.decoder.feed(data):
                            self.handle(frame)
                            if not self.alive:
                                break
                    except ProtocolError:
                        # Revoke readiness before releasing the command lock.
                        self.close("protocol_error")
                        raise
                    self.tick()
        except ProtocolError as exc:
            self.close_reason = "protocol_error"
            self.event("protocol_error", error=str(exc), residual_hex=bytes(self.decoder.buffer).hex())
        except RecordingError as exc:
            self.owner.abort(str(exc))
        except OSError as exc:
            # A socket closed by this process (stop, replacement, idle timeout) makes
            # recv() fail; that is the recorded close reason, not a connection error.
            if self.alive:
                self.close_reason = "connection_error"
                if not self.recorder.failed:
                    self.event("connection_error", error=str(exc))
        finally:
            try:
                self.close(self.close_reason)
                if not self.recorder.failed:
                    self.event("disconnected", reason=self.close_reason,
                               residual_hex=bytes(self.decoder.buffer).hex(),
                               rx_bytes=self.offsets["rx"], tx_attempted_bytes=self.offsets["tx"])
            except RecordingError as exc:
                self.owner.abort(str(exc))


class LabServer:
    def __init__(self, config, recorder, *, clock=time.monotonic):
        config.validate()
        self.config, self.recorder, self.clock = config, recorder, clock
        self.lock = threading.RLock()
        self.node = config.expected_node
        self.current = None
        self.listener = None
        self.thread = None
        self.stopping = threading.Event()
        self.failure = None
        self.sessions = []
        self.device = {}          # latest decoded value per field, kept across sessions
        self.novel_count = 0
        self.novel_repeats = {}     # (mid, reasons) -> occurrences
        self.keepalives = 0
        self.silent_closes = 0      # consecutive identity timeouts
        self.last_marker = None

    def identity_seen(self):
        with self.lock:
            self.silent_closes = 0

    def silent_closed(self, session):
        with self.lock:
            self.silent_closes += 1
            count = self.silent_closes
        if count >= self.config.silent_close_alert:
            session.event("silent_reconnect_loop", consecutive=count,
                          advice="re-associate the controller Wi-Fi (restart the hotspot/AP)")

    def update_device(self, mid, values):
        with self.lock:
            for name, item in values.items():
                self.device[name] = {**item, "source": f"{mid:04X}", "at": stamp()}

    def keepalive_seen(self):
        with self.lock:
            self.keepalives += 1

    def note_novel(self, session, mid, token, body, reasons):
        """Separate log (novel.*.jsonl) for messages unlike anything recorded so far."""
        key = (f"{mid:04X}", tuple(reasons))
        with self.lock:
            self.novel_count += 1
            marker = self.last_marker
            count = self.novel_repeats.get(key, 0) + 1
            self.novel_repeats[key] = count
        if count > self.config.novel_detail_limit:
            if count in (10, 100, 1000) or count % 10000 == 0:
                self.recorder.emit("novel_message_repeated", session.id, channel="novel",
                                   mid=key[0], reasons=list(reasons), repeat_total=count,
                                   detail_limit=self.config.novel_detail_limit)
            return
        self.recorder.emit("novel_message", session.id, channel="novel",
                           mid=f"{mid:04X}", token=token.hex(), body_hex=body.hex(),
                           body_length=len(body), reasons=reasons,
                           last_marker=marker, recent_frames=list(session.recent),
                           hint=APK_HINTS.get(mid),
                           pending_command=session.pending.command_id if session.pending else None)
        session.event("novel_message_logged", mid=f"{mid:04X}", reasons=reasons)

    def accept_identity(self, node):
        with self.lock:
            if self.node is None:
                self.node = node
            return self.node == node

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.config.bind, self.config.port))
            sock.listen(2)
            sock.settimeout(0.25)
            self.listener = sock
            self.recorder.emit("listening", address=list(sock.getsockname()))
            self.thread = threading.Thread(target=self.accept_loop, daemon=True)
            self.thread.start()
        except BaseException:
            sock.close()
            raise

    def accept_loop(self):
        try:
            while not self.stopping.is_set():
                try:
                    conn, peer = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if not self.stopping.is_set():
                        raise
                    break
                with self.lock:
                    current = self.current
                # Close the replaced session without holding self.lock: its thread may be
                # waiting for self.lock while holding the session lock.
                if (not self.stopping.is_set() and self.config.allows(peer[0])
                        and current and current.alive and self.config.replace_session):
                    idle = None if current.last_rx is None else self.clock() - current.last_rx
                    self.recorder.emit("session_replaced", current.id,
                                       new_peer=list(peer),
                                       old_received_any=current.last_rx is not None,
                                       old_idle_seconds=idle,
                                       old_identity=current.got_identity)
                    current.close("replaced_by_new_connection")
                    if current.thread is not None:
                        current.thread.join(timeout=1)
                with self.lock:
                    if self.stopping.is_set():
                        conn.close()
                        break
                    self.sessions = [s for s in self.sessions
                                     if s.thread is not None and s.thread.is_alive()]
                    current = self.current
                    reason = ("client_ip" if not self.config.allows(peer[0])
                              else "concurrent_session" if current and current.alive
                              else "session_shutdown_backlog" if len(self.sessions) >= 4 else None)
                    if reason:
                        conn.close()
                        self.recorder.emit("connection_rejected", peer=list(peer), reason=reason)
                        continue
                    session = Session(self, conn, peer)
                    self.current = session
                    self.sessions.append(session)
                    session.thread = threading.Thread(target=session.serve, daemon=True)
                    session.thread.start()
        except (RecordingError, OSError) as exc:
            self.abort(str(exc))

    def session(self):
        with self.lock:
            return self.current

    def mark(self, text):
        if not text.strip() or len(text) > 2000:
            raise ValueError("marker must contain 1..2000 characters")
        session = self.session()
        action_id = uuid.uuid4().hex
        event = self.recorder.emit("operator_marker", session.id if session else None,
                                   action_id=action_id, text=text, source="operator")
        with self.lock:
            self.last_marker = {"text": text, "at": event["at"], "action_id": action_id}
        return action_id

    def device_summary(self):
        with self.lock:
            return {name: (item.get("label") or item["value"]) for name, item in self.device.items()}

    def status(self):
        session = self.session()
        extra = {"device": self.device_summary(), "novel_messages": self.novel_count,
                 "silent_closes": self.silent_closes, "keepalives": self.keepalives}
        if not session:
            return {"connected": False, "ready": False, "storage_failed": self.recorder.failed,
                    **extra}
        with session.lock:
            pending = session.pending
            return {"session_id": session.id, "connected": session.alive,
                    "ready": session.ready(), "identity": session.got_identity,
                    "resumed": session.resumed,
                    "state_reports": session.state_count,
                    "state_age": None if session.state_at is None else self.clock() - session.state_at,
                    "pending_command": pending.command_id if pending else None,
                    "storage_failed": self.recorder.failed, **extra}

    def abort(self, reason):
        self.failure = reason
        self.stopping.set()
        if self.listener:
            self.listener.close()
        session = self.session()
        if session:
            session.close("storage_or_server_failure")

    def _novel_totals(self):
        with self.lock:
            items = [(k, n) for k, n in self.novel_repeats.items()
                     if n > self.config.novel_detail_limit]
        for (mid, reasons), total in items:
            self.recorder.emit("novel_message_repeated", None, channel="novel", mid=mid,
                               reasons=list(reasons), repeat_total=total, final=True,
                               detail_limit=self.config.novel_detail_limit)

    def stop(self):
        try:
            if not self.recorder.failed and not self.recorder.closed:
                self._novel_totals()
        except RecordingError as exc:
            self.failure = str(exc)
        self.stopping.set()
        if self.listener:
            self.listener.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        with self.lock:
            sessions = list(self.sessions)
        for session in sessions:
            try:
                session.close("server_stop")
            except RecordingError as exc:
                self.failure = str(exc)
        for session in sessions:
            if session.thread and session.thread is not threading.current_thread():
                session.thread.join(timeout=2)
