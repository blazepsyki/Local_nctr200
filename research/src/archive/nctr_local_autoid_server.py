#!/usr/bin/env python3
"""NCTR-200 experimental TCP/20000 server; account registration NOT required.

Evidence: user-provided NCTR-200 PCAPs captured 2026-10-15/17.
- Either 0001 starts the session (8-byte ID), or a 0000 exchange precedes 0001.
- If 0000 starts, its four-byte request does NOT contain the full ID; a
  previous capture can supply --bootstrap-node-id. Never fabricate missing ID.
- The 0001 server response has an anomalous declared length 000B but carries
  2B token + 8B time data. The final two time bytes remain unexplained.
- Code is NOT production-grade; physical boiler must remain supervised.
"""
import argparse
import datetime as dt
import os
import secrets
import socket
import struct
import threading
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def mkframe(mid, token, body=b'', declared_length=None):
    length = 2 + len(body) if declared_length is None else declared_length
    return struct.pack('!HH', mid, length) + token + body


def fmt_node(v):
    if not v or len(v) != 8:
        return '(unavailable)'
    return v[:2].hex().upper() + ':' + ('**' * 5) + ':' + v[-1:].hex().upper()


def parse_node(v):
    if not v:
        return None
    x = bytes.fromhex(v.replace(':', '').replace(' ', ''))
    if len(x) != 8:
        raise ValueError('node ID must be 8 bytes / 16 hex characters')
    return x


class Session:
    def __init__(self, conn, addr, args):
        self.conn, self.addr, self.args = conn, addr, args
        self.node = args.bootstrap_node
        self.node_learned = False
        self.got_0001 = False
        self.state_count = 0
        self.pending = {}
        self.alive = True
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def log(self, msg):
        stamp = dt.datetime.now(KST).isoformat(timespec='milliseconds')
        print(f'{stamp} {self.addr[0]}:{self.addr[1]} {msg}', flush=True)

    def send(self, payload):
        with self.lock:
            self.conn.sendall(payload)
        self.log('TX ' + payload.hex(' ').upper())

    def handle(self, mid, token, body):
        if mid == 0x0001 and len(body) == 8:
            self.log(f'RX 0001 token={token.hex().upper()} node={fmt_node(body)} len=8')
            if self.node is not None and self.node != body:
                self.log('STOP: 0001 node ID differs from optional bootstrap ID')
                self.alive = False
                return
            self.node = body
            self.node_learned = True
            if self.args.save_node_id:
                # Stored only on explicit request. Mode 0600 before writing.
                fd = os.open(self.args.save_node_id, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, 'w') as f:
                        f.write(body.hex().upper() + '\n')
                except Exception:
                    os.close(fd)
                    raise
                self.log('SAVED node ID locally (private file)')
        else:
            text = f'RX {mid:04X} token={token.hex().upper()} len={len(body)}'
            if mid != 0x0000 and len(body) <= 8:
                text += ' body=' + body.hex(' ').upper()
            elif mid == 0x0000:
                text += ' prefix=' + body.hex(' ').upper()
            self.log(text)

        if self.args.mode == 'observe':
            return
        if mid == 0x0000:
            if len(body) != 4:
                self.log('WARNING unexpected 0000 payload length')
                return
            if self.node is None:
                self.log('BLOCKED 0000: only 4-byte prefix from device; full ID is unknown.')
                self.log('Use a prior capture 0001 to set --bootstrap-node-id, or see if 0001 arrives unaided.')
                return
            self.send(mkframe(0x0000, token, self.node))
        elif mid == 0x0001:
            if len(body) != 8:
                self.log('WARNING unexpected 0001 length')
                return
            self.got_0001 = True
            t = dt.datetime.now(KST)
            # YY MM DD HH MM SS plus 2 UNKNOWN bytes from previous capture.
            payload = bytes([t.year % 100, t.month, t.day, t.hour, t.minute, t.second]) + self.args.time_tail
            self.send(mkframe(0x0001, token, payload, declared_length=11))
        elif mid == 0x01FF:
            self.state_count += 1
            self.log(f'STATE REPORT count={self.state_count} bytes={len(body)}')
            self.send(mkframe(0x01FF, token))
            if self.got_0001 and self.node_learned and self.state_count >= 1:
                self.ready.set()
        elif mid == 0x0002:
            if self.args.pair_code is None:
                self.log('PAIR-CODE REQUEST received; no code configured, no response (test only)')
            else:
                self.send(mkframe(0x0002, token, struct.pack('!H', self.args.pair_code) + b'\x07\x08'))
        elif (mid, token) in self.pending:
            self.log('COMMAND APP ACK for ' + self.pending.pop((mid, token)))
        elif mid in (0x0101, 0x0102, 0x0105, 0x0106, 0x0115) and body:
            self.log('DEVICE EVENT; acknowledging observed command type')
            self.send(mkframe(mid, token))
        else:
            self.log('UNRECOGNIZED FRAME (not automatically acknowledged)')

    def serve(self):
        buf = bytearray()
        self.conn.settimeout(1)
        try:
            while self.alive:
                try:
                    data = self.conn.recv(8192)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf.extend(data)
                while len(buf) >= 4:
                    mid, declared = struct.unpack('!HH', buf[:4])
                    if declared < 2 or declared > 2048:
                        self.log(f'BAD frame length {declared}')
                        self.alive = False
                        break
                    total = 4 + declared
                    if len(buf) < total:
                        break
                    packet = bytes(buf[:total])
                    del buf[:total]
                    self.handle(mid, packet[4:6], packet[6:])
                    if not self.alive:
                        break
        except (ConnectionError, OSError) as e:
            self.log('SOCKET ' + str(e))
        finally:
            self.ready.clear()
            self.alive = False
            try:
                self.conn.close()
            except OSError:
                pass
            self.log('DISCONNECTED')

    def temp(self, n):
        if not self.args.enable_temperature or self.args.mode != 'handshake':
            self.log('CONTROL disabled; --enable-temperature required')
            return
        if not self.alive or not self.ready.is_set():
            self.log('BLOCKED: no 0001 + 01FF session confirmation')
            return
        if not 18 <= n <= 26:
            self.log('BLOCKED: test range 18..26 C')
            return
        token = secrets.token_bytes(2)
        self.pending[(0x0102, token)] = f'temp {n}'
        self.send(mkframe(0x0102, token, bytes([1, 0, 0, 0, n, 0])))


def main():
    p = argparse.ArgumentParser(description='NCTR-200 TCP experiment: learn node ID from first 0001')
    p.add_argument('--bind', default='0.0.0.0')
    p.add_argument('--port', type=int, default=20000)
    p.add_argument('--mode', choices=('observe', 'handshake'), default='observe')
    p.add_argument('--bootstrap-node-id', help='OPTIONAL previous 8-byte ID only for 0000-prefixed sessions')
    p.add_argument('--save-node-id', help='OPTIONAL private file to save the node ID seen in 0001')
    p.add_argument('--time-tail', help='REQUIRED in handshake mode: unverified 2-byte suffix for 0001 time response, e.g. 0123')
    p.add_argument('--pair-code', type=int, help='optional demonstration response to 0002, 0-9999')
    p.add_argument('--allowed-client-ip', help='only accept the known NCTR-200 DHCP IP')
    p.add_argument('--enable-temperature', action='store_true', help='explicitly enable supervised experimental setpoint change')
    args = p.parse_args()
    try:
        args.bootstrap_node = parse_node(args.bootstrap_node_id)
        if args.mode == 'handshake' and args.time_tail is None:
            raise ValueError('handshake mode requires explicit --time-tail (2 unverified bytes)')
        args.time_tail = bytes.fromhex(args.time_tail) if args.time_tail is not None else b''
        if args.mode == 'handshake' and len(args.time_tail) != 2:
            raise ValueError('time-tail must contain exactly two bytes')
    except ValueError as e:
        p.error(str(e))
    if args.pair_code is not None and not 0 <= args.pair_code <= 9999:
        p.error('pair-code must be between 0 and 9999')
    if args.enable_temperature and args.mode != 'handshake':
        p.error('control requires --mode handshake')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((args.bind, args.port))
        s.listen(2)
        s.settimeout(1)
        print(f'LISTEN {args.bind}:{args.port} mode={args.mode}; auto-learn node ID from 0001')
        print('NOT a production boiler controller. STOP with Ctrl+C.')
        try:
            while True:
                try:
                    sock, peer = s.accept()
                except socket.timeout:
                    continue
                if args.allowed_client_ip and peer[0] != args.allowed_client_ip:
                    print('REJECT other IP ' + peer[0],flush=True)
                    sock.close()
                    continue
                sess = Session(sock, peer, args)
                sess.log('CONNECTED')
                worker = threading.Thread(target=sess.serve, daemon=True)
                worker.start()
                if args.enable_temperature:
                    print('CONTROL ARMED. Enter `temp 22` only after session READY, or `done`.', flush=True)
                    while sess.alive:
                        try: raw = input('nctr> ').strip().lower()
                        except (KeyboardInterrupt, EOFError):
                            sess.alive = False
                            break
                        if raw == 'done':
                            sess.alive = False
                            break
                        if raw.startswith('temp '):
                            try: n = int(raw.split()[1])
                            except (ValueError, IndexError):
                                print('usage: temp 22'); continue
                            if not sess.ready.is_set():
                                print('BLOCKED: wait for 0001+01FF'); continue
                            print('PHYSICAL BOILER will receive target-temperature change.')
                            if input('Confirm by typing SEND: ').strip() == 'SEND':
                                sess.temp(n)
                        else:
                            print('Commands: temp 22 | done')
                worker.join()
        except KeyboardInterrupt:
            print('\nSTOPPED')


if __name__ == '__main__':
    main()
