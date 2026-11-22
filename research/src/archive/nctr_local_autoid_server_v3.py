#!/usr/bin/env python3
"""NCTR-200 WiFi local TCP/20000 experimental server, revision 3.

Differences from prior prototype:
- Interactive prompt is available even before any client connection.
- `status` always reports whether a live socket and a validated session exist.
- `temp N` is blocked until THIS connection has reported both 0001 and 01FF;
  nothing is queued for later delivery.
- Experimental setpoint range extended to 30 C (physical controller must permit it).
- Interpret device-originated 0102 six-byte reports as room/current/target,
  display these separately and include the latest in status.
- Graceful thread/socket shutdown; BrokenPipeError suppressed if tee exits.
- Node ID is learned from incoming 0001; no manufacturer account registration.

This is research software for a supervised, locally owned physical boiler.
It does NOT bypass the boiler's hardware safety controls and is NOT ready
for unsupervised or production Home Assistant use.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import os
import secrets
import socket
import struct
import sys
import threading
import time
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_OUTPUT_LOCK = threading.RLock()
_OUTPUT_BROKEN = False


def emit(msg: str = "") -> None:
    """Handle tee closing before a worker finishes printing."""
    global _OUTPUT_BROKEN
    if _OUTPUT_BROKEN:
        return
    with _OUTPUT_LOCK:
        try:
            print(msg, flush=True)
        except BrokenPipeError:
            _OUTPUT_BROKEN = True
            # Further print/input prompts should not throw in worker threads.
            try:
                sys.stdout = open(os.devnull, "w", encoding="utf-8")
            except OSError:
                pass


def packet(mid: int, token: bytes, body: bytes = b"", declared=None) -> bytes:
    if len(token) != 2:
        raise ValueError("token must be 2 bytes")
    length = len(body) + 2 if declared is None else declared
    return struct.pack("!HH", mid, length) + token + body


def masked_node(node: bytes | None) -> str:
    if node is None or len(node) != 8:
        return "(not yet learned)"
    # 8 bytes / 16 hex chars. Never print or log the unmasked identifier.
    return node[:2].hex().upper() + ":**********:" + node[-1:].hex().upper()


def parsed_id(value: str | None) -> bytes | None:
    if not value:
        return None
    ident = bytes.fromhex(value.replace(":", "").replace(" ", ""))
    if len(ident) != 8:
        raise ValueError("node ID must consist of 8 bytes (16 hex digits)")
    return ident


class Session:
    def __init__(self, sock: socket.socket, peer, opts):
        self.sock = sock
        self.peer = peer
        self.opts = opts
        self.alive = True
        self.learned = False
        self.node = opts.bootstrap_node
        self.got_0001 = False
        self.state_reports = 0
        self.pending: dict[tuple[int, bytes], str] = {}
        self.ready = threading.Event()
        self.last_rx = None
        self.sent_count = 0
        self.rx_count = 0
        self.last_temperature_report = None  # (room, current, target, mode, minor)
        self.tx_lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def log(self, msg):
        now = datetime.now(KST).isoformat(timespec="milliseconds")
        emit(f"{now} {self.peer[0]}:{self.peer[1]} {msg}")

    def send(self, payload: bytes) -> bool:
        if not self.alive:
            return False
        try:
            with self.tx_lock:
                self.sock.sendall(payload)
            self.sent_count += 1
            self.log("TX " + payload.hex(" ").upper())
            return True
        except OSError as exc:
            self.log("SEND FAILED " + str(exc))
            self.close()
            return False

    def _persist_node(self):
        target = self.opts.save_node_id
        if not target or not self.node:
            return
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as dest:
            os.fchmod(dest.fileno(), 0o600)
            dest.write(self.node.hex().upper() + "\n")
        self.log("SAVED device ID to private file")

    def handle(self, mid: int, token: bytes, body: bytes):
        self.last_rx = time.monotonic()
        self.rx_count += 1
        if mid == 0x0001:
            if len(body) != 8:
                self.log(f"RX 0001 unexpected node length={len(body)}")
                return
            self.log(f"RX 0001 token={token.hex().upper()} node={masked_node(body)} len=8")
            if self.node is not None and self.node != body:
                self.log("NODE MISMATCH. Disconnecting unapproved device")
                self.close()
                return
            if not self.learned:
                self.node = body
                self.learned = True
                self._persist_node()
        else:
            self.log(f"RX {mid:04X} token={token.hex().upper()} len={len(body)}"
                     + (" body=" + body.hex(" ").upper() if len(body) <= 8 else ""))

        if self.opts.mode == "observe":
            return

        if mid == 0x0000:
            if len(body) != 4:
                self.log("UNEXPECTED 0000 payload length; no reply")
            elif self.node is None:
                self.log("0000 needs 8-byte node ID, but request contains only 4 bytes."
                         " Use --bootstrap-node-id from old PCAP if needed.")
            else:
                self.send(packet(0x0000, token, self.node))

        elif mid == 0x0001:
            self.got_0001 = True
            t = datetime.now(KST)
            # Captures suggest YY MM DD HH MM SS + two still-unidentified bytes.
            # Note: length field 0x000B describes 11 bytes, but the capture has
            # only 10 bytes following the first four header bytes.
            body_out = bytes((t.year % 100, t.month, t.day, t.hour,
                              t.minute, t.second)) + self.opts.time_tail
            self.send(packet(0x0001, token, body_out, declared=11))

        elif mid == 0x01FF:
            self.state_reports += 1
            self.log(f"STATE REPORT count={self.state_reports} bytes={len(body)}")
            self.send(packet(0x01FF, token))
            if self.got_0001 and self.learned:
                if not self.ready.is_set():
                    self.log("SESSION READY: 0001 + 01FF completed")
                self.ready.set()

        elif mid == 0x0002:
            if self.opts.pair_code is None:
                self.log("PAIR CODE requested; no response in this trial")
            else:
                out = struct.pack("!H", self.opts.pair_code) + b"\x07\x08"
                self.send(packet(0x0002, token, out))

        elif (mid, token) in self.pending:
            self.log("COMMAND RESPONSE " + self.pending.pop((mid, token)))

        elif mid in (0x0101, 0x0102, 0x0105, 0x0106, 0x0115) and body:
            if mid == 0x0102 and len(body) == 6:
                room, owner, current, mode, target, minor = body
                # Static APK analysis of MessageInner: fields at bytes 2/4.
                # Not an authoritative physical sensor reading; verify LCD.
                self.last_temperature_report = (room, current, target, mode, minor)
                self.log(f"DEVICE 0102: room={room} owner={owner} "
                         f"current={current}C target={target}C mode={mode} minor={minor}")
            self.log("DEVICE-INITIATED EVENT: ACK")
            self.send(packet(mid, token))

        else:
            self.log("UNRECOGNIZED frame: no auto ACK")

    def serve(self):
        buf = bytearray()
        try:
            self.sock.settimeout(0.6)
            while self.alive:
                try:
                    data = self.sock.recv(8192)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf.extend(data)
                while self.alive and len(buf) >= 4:
                    mid, declared = struct.unpack("!HH", buf[:4])
                    if declared < 2 or declared > 2048:
                        self.log(f"BAD declared frame length {declared}; closing")
                        self.close()
                        break
                    total = 4 + declared
                    if len(buf) < total:
                        break
                    raw = bytes(buf[:total]); del buf[:total]
                    self.handle(mid, raw[4:6], raw[6:])
        except (ConnectionError, OSError) as exc:
            if self.alive:
                self.log("CONNECTION ERROR " + str(exc))
        finally:
            self.close()
            self.log("DISCONNECTED")

    def close(self):
        self.alive = False
        self.ready.clear()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def send_temp(self, setpoint: int):
        # No bypass/force mode and no queuing. Reject stale or incomplete sessions.
        if not self.opts.enable_temperature:
            return "Control disabled (--enable-temperature was not supplied)"
        if not self.alive:
            return "No active TCP connection"
        if not self.ready.is_set():
            return "BLOCKED: this connection has not completed 0001 + 01FF"
        if not 18 <= setpoint <= 30:
            return "BLOCKED: experimental setpoint range is 18..30 C"
        token = secrets.token_bytes(2)
        self.pending[(0x0102, token)] = f"0102 temp={setpoint} C"
        ok = self.send(packet(0x0102, token, bytes((1, 0, 0, 0, setpoint, 0))))
        if not ok:
            self.pending.pop((0x0102, token), None)
            return "send failed"
        return "Sent one 0102 command; check device LCD and following state report"


class Server:
    def __init__(self, opts):
        self.opts = opts
        self.running = threading.Event()
        self.running.set()
        self.lock = threading.RLock()
        self.current: Session | None = None
        self.listener = None
        self.accept_thread = None

    def accept_loop(self):
        while self.running.is_set():
            try:
                conn, peer = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.opts.allowed_client_ip and peer[0] != self.opts.allowed_client_ip:
                emit(f"REJECT unexpected client {peer[0]}")
                conn.close()
                continue
            with self.lock:
                prev = self.current
                if prev is not None and prev.alive:
                    emit("REJECT concurrent connection: one test device at a time")
                    conn.close()
                    continue
                sess = Session(conn, peer, self.opts)
                self.current = sess
                sess.thread = threading.Thread(target=sess.serve, name="nctr-session", daemon=True)
                sess.log("CONNECTED")
                sess.thread.start()

    def session(self):
        with self.lock:
            return self.current

    def status(self):
        s = self.session()
        if s is None:
            emit("STATUS: no TCP connection has arrived since server start")
            return
        age = "never" if s.last_rx is None else f"{time.monotonic() - s.last_rx:.1f}s ago"
        emit("STATUS: " + ("ESTABLISHED" if s.alive else "DISCONNECTED")
             + f" peer={s.peer[0]}:{s.peer[1]}"
             + f" node={masked_node(s.node)}"
             + f" 0001={s.got_0001} 01FF={s.state_reports}"
             + f" ready={s.ready.is_set()} last_rx={age}"
             + f" rx={s.rx_count} tx={s.sent_count}")
        if s.last_temperature_report is not None:
            room, current, target, mode, minor = s.last_temperature_report
            emit(f"LATEST DEVICE 0102 room={room} current={current}C target={target}C "
                 f"mode={mode} minor={minor} (verify on physical LCD)")

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.opts.bind, self.opts.port))
        sock.listen(2)
        sock.settimeout(0.5)
        self.listener = sock
        self.accept_thread = threading.Thread(target=self.accept_loop,
                                              name="nctr-accept", daemon=True)
        self.accept_thread.start()
        emit(f"LISTEN {self.opts.bind}:{self.opts.port} mode={self.opts.mode}")
        emit("Commands: status | wait 30 | temp 27 | temp 30 | help | quit")
        emit("A TCP connection + 0001 + 01FF are required before temperature control.")

    def stop(self):
        self.running.clear()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        s = self.session()
        if s is not None:
            s.close()
        if self.accept_thread:
            self.accept_thread.join(timeout=1.5)
        if s and s.thread:
            s.thread.join(timeout=1.5)

    def shell(self):
        while self.running.is_set():
            try:
                command = input("nctr> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                emit("\nStopping server")
                break
            if command in ("quit", "exit", "done"):
                break
            if command in ("help", "?"):
                emit("status: show live connection and session state")
                emit("wait 30: check for a ready session for up to 30 seconds")
                emit("temp 27 / temp 30: send ONE supervised setpoint change if ready")
                emit("quit: close socket and exit")
            elif command == "status":
                self.status()
            elif command.startswith("wait "):
                try:
                    duration = int(command.split()[1])
                    if duration < 1 or duration > 120:
                        raise ValueError()
                except (ValueError, IndexError):
                    emit("Usage: wait 30 (1..120 seconds)")
                    continue
                end = time.monotonic() + duration
                while time.monotonic() < end:
                    s = self.session()
                    if s and s.alive and s.ready.is_set():
                        emit("READY: current session completed 0001 and 01FF")
                        break
                    time.sleep(0.25)
                else:
                    emit("TIMEOUT: session is not ready")
                self.status()
            elif command.startswith("temp "):
                try:
                    value = int(command.split()[1])
                except (ValueError, IndexError):
                    emit("Usage: temp 22")
                    continue
                s = self.session()
                if s is None or not s.alive:
                    emit("BLOCKED: no active client TCP connection; a command cannot be delivered")
                elif not s.ready.is_set():
                    emit("BLOCKED: wait for RX 0001 and RX 01FF on THIS connection")
                    self.status()
                elif not self.opts.enable_temperature:
                    emit("BLOCKED: --enable-temperature not set")
                elif not 18 <= value <= 30:
                    emit("BLOCKED: prototype test range 18..30 C")
                else:
                    emit(f"PHYSICAL BOILER setpoint would be changed to {value} C.")
                    emit("Confirm {value}C is supported by the physical controller.".format(value=value))
                    emit("Ensure controller is in NORMAL operating mode, not AP setup mode.")
                    try:
                        confirmation = input("Type SEND to transmit once: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        confirmation = ""
                    if confirmation == "SEND":
                        emit(s.send_temp(value))
                    else:
                        emit("Cancelled, no command sent")
            elif command:
                emit("Unknown command. Use: status | wait 30 | temp 27 | temp 30 | quit")


def main():
    p = argparse.ArgumentParser(description="NCTR-200 TCP local test server with live diagnostics")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=20000)
    p.add_argument("--mode", choices=("observe", "handshake"), default="observe")
    p.add_argument("--time-tail", help="Required for handshake: 2 unverified bytes, e.g. 0123")
    p.add_argument("--bootstrap-node-id", help="Optional 8-byte ID for 0000-first sessions")
    p.add_argument("--save-node-id", help="Optional local private file path")
    p.add_argument("--allowed-client-ip", help="Restrict connections to an expected client IPv4")
    p.add_argument("--pair-code", type=int, default=None,
                   help="Optional test reply to 0002; not actual vendor registration")
    p.add_argument("--enable-temperature", action="store_true")
    opts = p.parse_args()
    try:
        opts.bootstrap_node = parsed_id(opts.bootstrap_node_id)
        opts.time_tail = bytes.fromhex(opts.time_tail) if opts.time_tail else b""
    except ValueError as exc:
        p.error(str(exc))
    if opts.mode == "handshake" and len(opts.time_tail) != 2:
        p.error("handshake mode requires --time-tail with exactly 2 bytes")
    if opts.pair_code is not None and not 0 <= opts.pair_code <= 9999:
        p.error("pair code must be 0..9999")
    if opts.enable_temperature and opts.mode != "handshake":
        p.error("--enable-temperature requires --mode handshake")
    server = Server(opts)
    try:
        server.start()
        server.shell()
    except KeyboardInterrupt:
        emit("\nStopping server")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
