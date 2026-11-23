"""All traffic is synthetic and restricted to in-process or loopback sockets."""

import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nctr_lab.analysis import analyze, attach, export_metadata, replay, verify
from nctr_lab.doctor import check_environment, query_a
from nctr_lab.protocol import Decoder, ProtocolError, packet, parse_ack_rule
from nctr_lab.pcap_import import ingest_rows, import_capture
from nctr_lab.recording import Recorder, RecordingError, load_events
from nctr_lab.server import Config, LabServer, Session
from nctr_lab_cli import bundle, check_bundle, main, parser

NODE = bytes.fromhex("1020304050607080")
TOKEN = b"\x12\x34"


def frame(mid, body=b"", token=TOKEN):
    return list(Decoder().feed(packet(mid, token, body)))[0]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendall(self, data):
        if self.closed:
            raise OSError("closed")
        self.sent.append(data)

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.config = Config(mode="handshake", time_tail=b"\x01\x23")
        self.recorder = Recorder(self.root / "run", self.config.manifest(),
                                 reserve_bytes=0, clock=self.clock)
        self.addCleanup(self.finish)
        self.owner = LabServer(self.config, self.recorder, clock=self.clock)
        self.sock = FakeSocket()
        self.session = Session(self.owner, self.sock, ("127.0.0.1", 12345))
        self.owner.current = self.session

    def finish(self):
        self.session.close()
        self.recorder.close()

    def ready(self):
        self.session.handle(frame(0x0001, NODE))
        self.session.handle(frame(0x01FF, bytes(103)))
        self.assertTrue(self.session.ready())

    def events(self, kind=None):
        events = load_events(self.recorder.path)
        return [e for e in events if kind is None or e["event_type"] == kind]


class ProtocolTests(unittest.TestCase):
    def test_every_split_boundary(self):
        wire = packet(1, TOKEN, NODE) + packet(0x01FF, TOKEN, bytes(103))
        expected = list(Decoder().feed(wire))
        for split in range(len(wire) + 1):
            decoder = Decoder()
            result = list(decoder.feed(wire[:split])) + list(decoder.feed(wire[split:]))
            self.assertEqual(result, expected)
            self.assertFalse(decoder.buffer)

    def test_bytewise_and_merged_frames(self):
        wire = packet(0x010C, TOKEN, bytes(range(49))) + packet(0x7777, TOKEN, bytes(8192))
        decoder = Decoder()
        result = []
        for value in wire:
            result.extend(decoder.feed(bytes((value,))))
        self.assertEqual([len(f.body) for f in result], [49, 8192])
        self.assertEqual(result[1].offset, len(result[0].raw))

    def test_invalid_length_preserves_residual(self):
        for length in (0, 1):
            decoder = Decoder()
            wire = struct.pack("!HH", 0x7777, length)
            with self.assertRaises(ProtocolError):
                list(decoder.feed(wire))
            self.assertEqual(bytes(decoder.buffer), wire)

    def test_valid_prefix_before_bad_header_is_yielded(self):
        decoder = Decoder()
        iterator = decoder.feed(packet(0x0102, TOKEN) + b"\x00\x01\x00\x01")
        self.assertEqual(next(iterator).mid, 0x0102)
        with self.assertRaises(ProtocolError):
            next(iterator)

    def test_incomplete_is_not_emitted(self):
        decoder = Decoder()
        wire = packet(1, TOKEN, NODE)
        self.assertEqual(list(decoder.feed(wire[:-1])), [])
        self.assertEqual(bytes(decoder.buffer), wire[:-1])

    def test_response_length_policy_does_not_corrupt_generic_codec(self):
        legacy = packet(1, TOKEN, bytes(8), declared=11)
        self.assertEqual(len(legacy), 14)
        self.assertEqual(list(Decoder().feed(legacy)), [])
        actual = packet(1, TOKEN, bytes(8))
        self.assertEqual(list(Decoder().feed(actual))[0].declared, 10)

    def test_ack_policy_validation(self):
        self.assertEqual(parse_ack_rule("0107:49"), (0x0107, 49))
        for value in ("0001:8", "xyz:6", "0107:0", "0107:65534", "0102"):
            with self.assertRaises(ValueError):
                parse_ack_rule(value)

    def test_packet_rejects_invalid_headers(self):
        for args in ((1, b"x"), (65536, TOKEN)):
            with self.assertRaises(ValueError):
                packet(*args)


class SessionTests(Fixture):
    def test_identity_and_state_order_and_invalid_length(self):
        self.config.accept_resumed_session = False
        self.session.handle(frame(0x01FF, bytes(103)))
        self.assertFalse(self.session.ready())
        self.session.handle(frame(1, NODE))
        self.assertFalse(self.session.ready())
        self.session.handle(frame(0x01FF, bytes(102)))
        self.assertFalse(self.session.ready())
        self.assertTrue(self.events("state_profile_mismatch"))
        self.session.handle(frame(0x01FF, bytes(103)))
        self.assertTrue(self.session.ready())
        self.session.handle(frame(0x01FF, bytes(104)))
        self.assertFalse(self.session.ready())

    def test_identity_mismatch_is_pinned_across_connections(self):
        self.ready()
        other = Session(self.owner, FakeSocket(), ("127.0.0.1", 12346))
        other.handle(frame(1, bytes(8)))
        self.assertFalse(other.alive)
        self.assertTrue(self.events("identity_mismatch"))

    def test_invalid_identity_closes(self):
        self.session.handle(frame(1, NODE[:-1]))
        self.assertFalse(self.session.alive)

    def test_legacy_and_actual_time_response(self):
        self.session.handle(frame(1, NODE))
        self.assertEqual(self.sock.sent[-1][2:4], b"\x00\x0b")
        self.assertEqual(len(self.sock.sent[-1]), 14)
        self.config.time_length = "actual"
        self.session.handle(frame(1, NODE))
        self.assertEqual(self.sock.sent[-1][2:4], b"\x00\x0a")

    def test_bootstrap_only_uses_known_id(self):
        self.session.handle(frame(0, bytes(4)))
        self.assertEqual(self.sock.sent, [])
        self.owner.node = NODE
        self.session.handle(frame(0, bytes(4)))
        self.assertEqual(self.sock.sent[-1], packet(0, TOKEN, NODE))

    def test_observe_does_not_transmit_or_become_ready(self):
        self.config.mode = "observe"
        self.session.handle(frame(1, NODE))
        self.session.handle(frame(0x01FF, bytes(103)))
        self.assertEqual(self.sock.sent, [])
        self.assertFalse(self.session.ready())

    def test_unknown_and_large_events_preserved_without_auto_ack(self):
        body = bytes(range(256)) * 16
        self.session.handle(frame(0x7777, body))
        self.assertEqual(self.sock.sent, [])
        self.assertEqual(self.events("frame_rx")[-1]["body_hex"], body.hex())
        self.assertTrue(self.events("unknown_event"))

    def test_unlisted_device_event_is_acknowledged_and_marked(self):
        self.session.handle(frame(0x010C, bytes(6)))
        self.assertEqual(self.sock.sent, [packet(0x010C, TOKEN)])
        unknown = self.events("unknown_event")[-1]
        self.assertTrue(unknown["acknowledged"])
        self.assertEqual(unknown["policy"], "ack_unlisted_events")

    def test_unlisted_ack_can_be_disabled(self):
        self.config.ack_unlisted_events = False
        self.session.handle(frame(0x010C, bytes(6)))
        self.assertEqual(self.sock.sent, [])
        self.assertFalse(self.events("unknown_event")[-1]["acknowledged"])

    def test_observed_events_have_explicit_rules(self):
        for mid in (0x0104, 0x0108, 0x010A):
            self.session.handle(frame(mid, bytes(6)))
        self.assertEqual(len(self.sock.sent), 3)
        self.assertFalse(self.events("unknown_event"))

    def test_explicit_event_policy(self):
        self.config.ack_rules += ((0x0107, 49),)
        self.session.handle(frame(0x0107, bytes(49)))
        self.assertEqual(self.sock.sent, [packet(0x0107, TOKEN)])

    def test_pair_code_does_not_mean_authorization(self):
        self.config.pair_code = 3483
        self.session.handle(frame(2))
        self.assertEqual(self.sock.sent[-1], packet(2, TOKEN, b"\x0d\x9b\x07\x08"))
        self.assertFalse(self.session.ready())

    def test_command_gates(self):
        with self.assertRaises(ValueError):
            self.session.command("temperature", 23, confirmed=True)
        self.ready()
        original = len(self.sock.sent)
        with self.assertRaises(ValueError):
            self.session.command("temperature", 23, confirmed=True)
        self.config.enable_temperature = True
        for value, confirmed in ((23, False), (17, True), (31, True)):
            with self.assertRaises(ValueError):
                self.session.command("temperature", value, confirmed=confirmed)
        self.assertEqual(len(self.sock.sent), original)

    def test_payload_with_matching_token_is_not_ack(self):
        self.ready()
        self.config.enable_temperature = True
        command = self.session.command("temperature", 23, confirmed=True)
        pending = self.session.pending
        self.session.handle(frame(0x0102, bytes((1, 1, 21, 0, 23, 0)), pending.token))
        self.assertFalse(pending.acked)
        self.assertTrue(pending.report_match)
        self.assertFalse(self.events("command_ack"))
        self.session.handle(frame(0x0102, token=pending.token))
        self.assertTrue(pending.acked)
        self.assertIsNotNone(self.session.pending)
        self.session.confirm_lcd(command, 23)
        self.assertIsNone(self.session.pending)
        self.assertTrue(self.events("command_observed_match"))

    def test_ack_does_not_overwrite_report_or_prove_application(self):
        self.ready()
        self.config.enable_temperature = True
        command = self.session.command("temperature", 23, confirmed=True)
        pending = self.session.pending
        self.session.handle(frame(0x0102, token=pending.token))
        self.session.confirm_lcd(command, 23)
        self.session.handle(frame(0x0102, bytes((1, 0, 21, 0, 21, 0))))
        self.assertIsNotNone(self.session.pending)
        self.assertFalse(self.events("command_observed_match"))

    def test_timeout_only_once_no_retry_and_late_ack(self):
        self.ready()
        self.config.enable_temperature = True
        self.session.command("temperature", 23, confirmed=True)
        token = self.session.pending.token
        sent = len(self.sock.sent)
        self.clock.now += 11
        self.session.tick()
        self.session.tick()
        self.assertEqual(len(self.events("command_ack_timeout")), 1)
        self.clock.now += 720
        self.session.tick()
        self.assertIsNone(self.session.pending)
        self.session.handle(frame(0x0102, token=token))
        self.assertTrue(self.events("unmatched_ack"))
        self.assertEqual(len(self.sock.sent), sent)

    def test_staleness_and_reconnection_clear_readiness(self):
        self.ready()
        self.clock.now += 901
        self.assertFalse(self.session.ready())
        other = Session(self.owner, FakeSocket(), ("127.0.0.1", 22))
        self.assertFalse(other.ready())
        self.assertIsNone(other.pending)

    def test_power_encoding_and_cancel(self):
        self.ready()
        self.config.enable_power = True
        command = self.session.command("power", 2, confirmed=True)
        self.assertEqual(self.sock.sent[-1][6:], bytes((1, 0, 0, 0, 0, 2)))
        with self.assertRaises(ValueError):
            self.session.command("power", 1, confirmed=True)
        token = self.session.pending.token
        self.session.cancel()
        self.assertIsNone(self.session.pending)
        self.session.handle(frame(0x0101, token=token))
        self.assertFalse(self.events("command_observed_match"))
        self.assertEqual(self.events("command_cancelled")[-1]["command_id"], command)

    def test_storage_failure_blocks_before_command_bytes(self):
        self.ready()
        self.config.enable_temperature = True
        sent = len(self.sock.sent)
        with patch("nctr_lab.recording.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(RecordingError):
                self.session.command("temperature", 23, confirmed=True)
        self.assertEqual(len(self.sock.sent), sent)
        self.assertTrue(self.recorder.failed)
        self.assertFalse(self.session.ready())

    def test_token_not_reused_after_cancel(self):
        self.ready()
        self.config.enable_temperature = True
        with patch("nctr_lab.server.secrets.token_bytes", return_value=TOKEN):
            self.session.command("temperature", 23, confirmed=True)
            first = self.session.pending.token
            self.session.cancel()
            self.session.command("temperature", 24, confirmed=True)
            self.assertNotEqual(first, self.session.pending.token)


REAL_STATE = bytes.fromhex(
    "d00000000000000000001f1e24002d3c160000000000000000000000000000001a0000000000000000"
    "000000000000002d3c000000000000000000000000020000007c0000a10000a700000000000000000000"
    "0000000000010001000000000000000000000000")


def novel_rows(path):
    rows = []
    for file in sorted(Path(path).glob("novel.*.jsonl")):
        rows += [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
    return rows


class DecodeAndNovelTests(Fixture):
    def test_known_messages_decode_into_device_state(self):
        self.ready()
        self.session.handle(frame(0x01FF, REAL_STATE))
        self.session.handle(frame(0x0102, bytes.fromhex("010117001c00")))
        self.session.handle(frame(0x0104, bytes.fromhex("00001e002e00")))
        self.session.handle(frame(0x0115, bytes.fromhex("000000003700")))
        self.session.handle(frame(0x0108, bytes.fromhex("01000000030f")))
        device = self.owner.status()["device"]
        self.assertEqual(device["room_current"], 23)
        self.assertEqual(device["room_target"], 28)
        self.assertEqual(device["ondol_target"], 46)
        self.assertEqual(device["hot_water_target"], 55)
        self.assertEqual(device["reservation_stop_hours"], 3)
        self.assertEqual(device["reservation_run_minutes"], 15)
        self.assertEqual(device["status_flags"], 0xD0)
        self.assertEqual(self.owner.device["room_current"]["confidence"], "confirmed")
        self.assertTrue(self.events("state_decoded"))

    def test_recorded_formats_are_not_novel(self):
        self.session.handle(frame(1, NODE))
        for mid, body in ((0x01FF, REAL_STATE), (0x0102, "010116001a00"), (0x0104, "00001e002d00"),
                          (0x0105, "000000003c00"), (0x0106, "010100000000"),
                          (0x0108, "010000000314"), (0x0115, "000000003c00"),
                          (0x0101, "010000000002")):
            self.session.handle(frame(mid, body if isinstance(body, bytes) else bytes.fromhex(body)))
        self.recorder.close()
        self.assertEqual(novel_rows(self.recorder.path), [])
        self.assertEqual(self.owner.novel_count, 0)

    def test_unlike_messages_go_to_separate_log_with_context(self):
        self.owner.mark("목욕 버튼 2회")
        self.session.handle(frame(0x010C, bytes(6)))                       # unknown ID
        self.session.handle(frame(0x0102, bytes.fromhex("020116001a00")))  # fixed byte differs
        self.session.handle(frame(0x0115, bytes.fromhex("000000004600")))  # 70 is out of range
        changed = bytearray(REAL_STATE)
        changed[0], changed[40] = 0x22, 0x99
        self.session.handle(frame(0x01FF, bytes(changed)))
        self.session.handle(frame(0x0133, b""))                            # unmatched ACK
        self.recorder.close()
        rows = novel_rows(self.recorder.path)
        reasons = [r["reasons"] for r in rows]
        self.assertEqual(reasons[0], ["unknown_id"])
        self.assertIn("fixed_bytes_differ:0", reasons[1])
        self.assertIn("out_of_range:hot_water_target=70", reasons[2])
        self.assertIn("state_constant_changed:40", reasons[3])
        self.assertIn("new_status_flags:22", reasons[3])
        self.assertEqual(reasons[4], ["unmatched_ack"])
        self.assertEqual(rows[0]["last_marker"]["text"], "목욕 버튼 2회")
        self.assertIsNone(rows[0]["hint"])
        self.assertEqual(rows[1]["recent_frames"][-1]["mid"], "0102")
        self.assertEqual(self.owner.status()["novel_messages"], 5)
        verify(self.recorder.path)          # novel.*.jsonl is part of the hashed inventory
        report = analyze(self.recorder.path)
        self.assertEqual(sum(g["count"] for g in report["novel_messages"]), 5)

    def test_room_report_origin_distinguishes_remote_from_panel(self):
        self.session.handle(frame(0x0102, bytes.fromhex("010016001a00")))   # after remote command
        self.assertEqual(self.owner.status()["device"]["origin"], "remote")
        self.session.handle(frame(0x0102, bytes.fromhex("010116001a00")))   # dial on the panel
        self.assertEqual(self.owner.status()["device"]["origin"], "panel")
        self.recorder.close()
        self.assertEqual(novel_rows(self.recorder.path), [])

    def test_power_on_is_confirmed_by_mode_report_instead_of_ack(self):
        self.config.enable_power = True
        self.ready()
        off = self.session.command("power", 2, confirmed=True)
        self.session.handle(frame(0x0101, b"", token=self.sock.sent[-1][4:6]))     # OFF: plain ACK
        on = self.session.command("power", 1, confirmed=True)
        self.session.handle(frame(0x0104, bytes.fromhex("00001e002d00")))          # ON: mode report
        acks = [e for e in self.events("command_ack") if e["command_id"] == on]
        self.assertEqual((acks[-1]["implicit"], acks[-1]["via"]), (True, "0104"))
        self.clock.now += 11
        self.session.tick()
        self.assertFalse([e for e in self.events("command_ack_timeout") if e["command_id"] == on])
        self.session.command("ondol" if self.config.enable_ondol else "power", 2, confirmed=True)
        self.assertTrue([e for e in self.events("command_superseded") if e["command_id"] == on])
        self.assertTrue([e for e in self.events("command_superseded") if e["command_id"] == off])

    def test_hot_water_only_report_and_unverified_command(self):
        self.config.enable_hot_water_only = True
        self.ready()
        self.session.handle(frame(0x0114, bytes.fromhex("000000003c00")))
        self.assertEqual(self.owner.status()["device"]["reported_function"], "hot_water_only")
        self.session.command("hot_water_only", 60, confirmed=True)
        self.assertEqual(self.sock.sent[-1][:2].hex() + self.sock.sent[-1][6:].hex(),
                         "0114" + "000000003c00")
        self.assertEqual([r for r in novel_rows(self.recorder.path) if r["mid"] == "0114"], [])

    def test_reservation_command_uses_the_0108_report_layout(self):
        self.config.enable_reservation = True
        self.ready()
        for bad in ((0, 20), (3, 5), (25, 20), 3):
            with self.assertRaises(ValueError):
                self.session.command("reservation", bad, confirmed=True)
        command_id = self.session.command("reservation", (3, 20), confirmed=True)
        self.assertEqual(self.sock.sent[-1][:2].hex() + self.sock.sent[-1][6:].hex(),
                         "0108" + "010000000314")
        self.session.handle(frame(0x0108, b"", token=self.sock.sent[-1][4:6]))
        self.session.handle(frame(0x0108, bytes.fromhex("010000000314")))
        report = [e for e in self.events("command_report_candidate") if e["command_id"] == command_id]
        self.assertTrue(report[-1]["matches"])
        self.session.confirm_lcd(command_id, (3, 20))
        self.assertIsNone(self.session.pending)

    def test_status_byte_is_split_into_mode_operating_and_phase(self):
        expected = {0xD0: ("ondol", "on", "active"), 0xC1: ("room_heating", "on", "active"),
                    0xC2: ("reservation", "on", "active"),
                    0x82: ("reservation", "on", "inactive"), 0x84: ("away", "on", "inactive"),
                    0x10: ("ondol", "off", "inactive")}
        for flags, labels in expected.items():
            state = bytearray(REAL_STATE)
            state[0] = flags
            self.session.handle(frame(0x01FF, bytes(state)))
            device = self.owner.status()["device"]
            self.assertEqual((device["status_mode"], device["status_operating"],
                              device["status_active_phase"]), labels, hex(flags))
        self.assertFalse([r for r in novel_rows(self.recorder.path)
                          if any(x.startswith("new_status_flags") for x in r["reasons"])])

    def test_manufacturer_heating_water_and_schedule_formats(self):
        from nctr_lab.messages import parse_schedule, slots_to_text, decode
        self.config.enable_heating_water = True
        self.config.enable_schedule = True
        self.ready()
        with self.assertRaises(ValueError):
            self.session.command("heating_water", 44, confirmed=True)        # below 45
        self.session.command("heating_water", 47, confirmed=True)            # 1-degree steps
        self.assertEqual(self.sock.sent[-1][6:].hex(), "000000002f00")
        self.session.handle(frame(0x0103, b"", token=self.sock.sent[-1][4:6]))
        self.session.command("heating_water", 50, confirmed=True)
        self.assertEqual(self.sock.sent[-1][:2].hex() + self.sock.sent[-1][6:].hex(),
                         "0103" + "000000003200")
        self.session.handle(frame(0x0103, b"", token=self.sock.sent[-1][4:6]))
        slots = parse_schedule("00:00-09:00,18:00-24:00")
        self.session.command("schedule", slots, confirmed=True)
        boiler6 = ("01010101010101010101010101010101010101000000000000000000000000000000"
                   "000000010101010101010101010101")
        self.assertEqual(self.sock.sent[-1][:2].hex() + self.sock.sent[-1][6:].hex(),
                         "0107" + boiler6)
        self.assertEqual(slots_to_text(bytes.fromhex(boiler6)[1:]), "00:00-09:00,18:00-24:00")
        self.assertEqual(decode(0x0107, bytes.fromhex(boiler6))["schedule_24h"]["value"],
                         "00:00-09:00,18:00-24:00")
        for bad in ("09:00-08:00", "00:15-01:00", "00:00-25:00"):
            with self.assertRaises(ValueError):
                parse_schedule(bad)

    def test_keepalive_0003_is_echoed_and_not_novel(self):
        self.ready()
        self.session.handle(frame(0x0003, b"", token=b"\x4d\x48"))
        self.assertEqual(self.sock.sent[-1], packet(0x0003, b"\x4d\x48"))
        self.assertEqual(self.owner.status()["keepalives"], 1)
        self.assertFalse([r for r in novel_rows(self.recorder.path) if r["mid"] == "0003"])
        self.config.keepalive_reply = "none"
        sent = len(self.sock.sent)
        self.session.handle(frame(0x0003, b"", token=b"\x00\x01"))
        self.assertEqual(len(self.sock.sent), sent)

    def test_keepalive_probe_cycles_replies_and_logs_run_end(self):
        self.ready()
        self.config.keepalive_reply = "probe"
        sent = []
        for i in range(14):
            self.clock.now += 10
            self.session.handle(frame(0x0003, b"", token=i.to_bytes(2, "big")))
            sent.append(self.sock.sent[-1])
        tok = lambda i: i.to_bytes(2, "big")
        self.assertEqual(sent[0][:6], bytes.fromhex("0003000a") + tok(0))
        self.assertEqual(sent[0][12:], b"\x01\x23")
        self.assertEqual(sent[3][:6], bytes.fromhex("00030008") + tok(3))
        self.assertEqual(len(sent[3]), 12)
        self.assertEqual(sent[6][:6], bytes.fromhex("0001000b") + tok(6))
        self.assertEqual(sent[9], packet(0x0003, tok(9)))
        self.assertEqual(sent[13], packet(0x0003, tok(13)))
        self.assertEqual([e["reply"] for e in self.events("keepalive")][-14:],
                         ["time"] * 3 + ["time6"] * 3 + ["identity"] * 3 + ["echo"] * 5)
        # 01FF inside the run does not end it; a device event does.
        self.clock.now += 3
        self.session.handle(frame(0x01FF, bytes(103)))
        self.assertFalse(self.events("keepalive_run_end"))
        self.clock.now += 1
        self.session.handle(frame(0x010A, bytes.fromhex("010000001700")))
        end = self.events("keepalive_run_end")[-1]
        self.assertEqual((end["count"], end["last_reply"], end["ended_by"]), (14, "echo", "010A"))
        # A new run after a long gap starts the probe again; the gap ends it.
        self.clock.now += 3600
        self.session.handle(frame(0x0003, b"", token=b"\x10\x00"))
        self.clock.now += 120
        self.session.handle(frame(0x01FF, bytes(103)))
        end = self.events("keepalive_run_end")[-1]
        self.assertEqual((end["count"], end["last_reply"], end["ended_by"], end["next_mid"]),
                         (1, "time", "gap", "01FF"))

    def test_repeated_novel_messages_are_counted_not_flooded(self):
        for i in range(25):
            self.session.handle(frame(0x0133, b"", token=i.to_bytes(2, "big")))
        rows = [r for r in novel_rows(self.recorder.path) if r["mid"] == "0133"]
        detailed = [r for r in rows if r["event_type"] == "novel_message"]
        repeated = [r for r in rows if r["event_type"] == "novel_message_repeated"]
        self.assertEqual(len(detailed), 5)
        self.assertEqual([r["repeat_total"] for r in repeated], [10])
        self.assertEqual(self.owner.status()["novel_messages"], 25)
        self.owner.stop()
        self.recorder.close()
        report = analyze(self.recorder.path)
        group = [g for g in report["novel_messages"] if g["mid"] == "0133"][0]
        self.assertEqual(group["count"], 25)

    def test_apk_hint_is_attached_to_unreceived_ids(self):
        self.session.handle(frame(0x0109, bytes(6)))
        self.recorder.close()
        self.assertEqual(novel_rows(self.recorder.path)[0]["hint"], "APK: error report")

    def test_new_remote_commands_use_manufacturer_formats(self):
        for flag in ("enable_hot_water", "enable_bath", "enable_away"):
            setattr(self.config, flag, True)
        self.ready()
        expected = (("hot_water", 55, 0x0115, "000000003700"),
                    ("bath", 1, 0x0105, "000000003c00"),
                    ("away", 1, 0x0106, "010200000000"))
        for feature, value, mid, body in expected:
            command_id = self.session.command(feature, value, confirmed=True)
            sent = self.sock.sent[-1]
            self.assertEqual(sent[:2], mid.to_bytes(2, "big"))
            self.assertEqual(sent[6:].hex(), body)
            self.session.handle(frame(mid, b"", token=sent[4:6]))          # device ACK
            report = {"hot_water": "000000003700", "bath": "000000003c00",
                      "away": "010100000000"}[feature]
            self.session.handle(frame(mid, bytes.fromhex(report), token=b"\x55\x55"))
            self.session.confirm_lcd(command_id, value)
            self.assertIsNone(self.session.pending)

    def test_acknowledged_command_is_superseded_by_the_next(self):
        self.config.enable_ondol = True
        self.ready()
        first = self.session.command("ondol", 50, confirmed=True)
        with self.assertRaises(ValueError):                  # not acknowledged yet
            self.session.command("ondol", 45, confirmed=True)
        self.session.handle(frame(0x0104, b"", token=self.sock.sent[-1][4:6]))
        second = self.session.command("ondol", 45, confirmed=True)   # no cancel needed
        self.assertEqual(self.sock.sent[-1][6:].hex(), "000000002d00")
        superseded = [e for e in self.events("command_superseded") if e["command_id"] == first]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(self.session.pending.command_id, second)

    def test_state_report_confirms_remote_value(self):
        self.config.enable_ondol = True
        self.ready()
        command_id = self.session.command("ondol", 50, confirmed=True)
        self.session.handle(frame(0x0104, b"", token=self.sock.sent[-1][4:6]))
        state = bytearray(REAL_STATE)
        state[14] = 50
        self.session.handle(frame(0x01FF, bytes(state)))
        report = [e for e in self.events("command_report_candidate") if e["command_id"] == command_id]
        self.assertEqual((report[-1]["reported"], report[-1]["matches"], report[-1]["source"]),
                         (50, True, "01FF[14]"))
        self.session.confirm_lcd(command_id, 50)
        self.assertIsNone(self.session.pending)
        self.assertTrue(self.events("command_observed_match"))

    def test_new_commands_are_gated_and_bounded(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.session.command("hot_water", 50, confirmed=True)   # not enabled
        self.config.enable_hot_water = True
        with self.assertRaises(ValueError):
            self.session.command("hot_water", 61, confirmed=True)   # above manual range
        self.config.enable_ondol = True
        with self.assertRaises(ValueError):
            self.session.command("ondol", 40, confirmed=True)
        self.assertEqual(self.sock.sent[-1][:2], (0x01FF).to_bytes(2, "big"))
        with self.assertRaises(ValueError):
            Config(mode="handshake", time_tail=b"\x01\x23", hot_water_max=70).validate()


class ProvisionTests(unittest.TestCase):
    def test_payload_matches_manufacturer_app_layout(self):
        from nctr_lab.provision import ProvisionPlan
        plan = ProvisionPlan("HomeAP", "secret12", "121.141.70.84", "http://igs4.krb.co.kr")
        self.assertEqual(plan.payload(), (
            b"WizFi250AirCmd:AT+WSET=0,HomeAP\rAT+WSEC=0,,secret12\rAT+WNET=1\r"
            b"AT+SCON=S,TCN,121.141.70.84,20000,,1\rAT+ABCD=S,TCN,http://igs4.krb.co.kr,20000,,1\r"
            b"AT+FSOCK=6,15\rAT+MPROF=S\r"))
        self.assertNotIn("secret12", plan.masked_text())
        lf = ProvisionPlan("HomeAP", "pw", "192.168.1.8", "http://x", line_ending="lf").payload()
        self.assertNotIn(b"\r", lf)

    def test_unsafe_values_are_rejected(self):
        from nctr_lab.provision import ProvisionPlan, single_command
        for ssid, password, ip in (("a,b", "pw", "192.168.1.8"), ("ap", "p\rw", "192.168.1.8"),
                                   ("집", "pw", "192.168.1.8"), ("ap", "pw", "nas.local")):
            with self.assertRaises(ValueError):
                ProvisionPlan(ssid, password, ip, "http://192.168.1.8").payload()
        with self.assertRaises(ValueError):
            single_command("MRESET")

    def test_cli_sends_once_records_without_password_and_resets(self):
        from nctr_lab_cli import provision
        received = []
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        server.settimeout(2)
        self.addCleanup(server.close)

        def device():
            for _ in range(2):
                data, peer = server.recvfrom(2048)
                received.append(data)
                server.sendto(b"[OK]\r\n", peer)
        thread = threading.Thread(target=device, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as root:
            pw = Path(root) / "pw.txt"
            pw.write_text("secret12\n", encoding="utf-8")
            args = parser().parse_args(["provision", "--ssid", "HomeAP", "--server-ip", "192.168.1.8",
                                        "--password-file", str(pw), "--target", "127.0.0.1",
                                        "--target-port", str(server.getsockname()[1]),
                                        "--timeout", "0.5", "--record-dir", str(Path(root) / "rec")])
            answers = iter(["SEND", "RESET"])
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                result = provision(args, ask=lambda _: next(answers))
            thread.join(2)
            self.assertTrue(result["sent"])
            self.assertIn(b"AT+SCON=S,TCN,192.168.1.8,20000,,1", received[0])
            self.assertIn(b"AT+ABCD=S,TCN,http://nas.192.168.1.8.nip.io,20000,,1", received[0])
            self.assertEqual(received[1], b"WizFi250AirCmd:AT+MRESET\r")
            record = Path(result["record"]).read_text(encoding="utf-8")
            self.assertNotIn("secret12", record)
            self.assertNotIn("secret12", out.getvalue())
            self.assertTrue(json.loads(record)["reset_sent"])

    def test_allowed_client_accepts_a_network(self):
        config = Config(allowed_client_ip="192.168.1.0/24")
        config.validate()
        self.assertTrue(config.allows("192.168.1.57"))
        self.assertFalse(config.allows("192.168.137.99"))
        self.assertTrue(Config(allowed_client_ip="192.168.1.2").allows("192.168.1.2"))

    def test_no_reply_means_no_reset_like_the_app(self):
        from nctr_lab_cli import provision
        with tempfile.TemporaryDirectory() as root:
            args = parser().parse_args(["provision", "--ssid", "HomeAP", "--server-ip", "192.168.1.8",
                                        "--target", "127.0.0.1", "--record-dir", root])
            sent = []
            with patch("nctr_lab_cli.exchange", side_effect=lambda p, **k: sent.append(p) or []), \
                    patch("nctr_lab_cli.read_password", return_value="pw12345678"), \
                    patch("sys.stdout", new_callable=io.StringIO):
                result = provision(args, ask=lambda _: "SEND")
            self.assertEqual(len(sent), 1)
            self.assertFalse(json.loads(Path(result["record"]).read_text(encoding="utf-8"))["reset_sent"])

    def test_controller_dns_rewrite_is_predicted(self):
        from nctr_lab.provision import default_abcd, resolved_name
        self.assertEqual(resolved_name("http://192.168.1.8"), "1924.168.1.8")
        self.assertEqual(resolved_name("http://192.168.1.8.nip.io"), "1924.168.1.8.nip.io")
        self.assertEqual(resolved_name("http://igs.krb.co.kr"), "igs4.krb.co.kr")
        self.assertEqual(resolved_name(default_abcd("192.168.1.8")), "nas4.192.168.1.8.nip.io")

    def test_cli_cancel_sends_nothing(self):
        from nctr_lab_cli import provision
        args = parser().parse_args(["provision", "--ssid", "HomeAP", "--server-ip", "192.168.1.8",
                                    "--password-file", __file__, "--target", "127.0.0.1"])
        with patch("nctr_lab_cli.exchange", side_effect=AssertionError("must not send")), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("nctr_lab_cli.read_password", return_value="pw"):
            self.assertEqual(provision(args, ask=lambda _: "no"), {"sent": False})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "run"

    def recorder(self, **kwargs):
        recorder = Recorder(self.path, {}, reserve_bytes=0, **kwargs)
        self.addCleanup(recorder.close)
        return recorder

    def test_rotation_integrity_and_no_overwrite(self):
        rec = self.recorder(rotate_bytes=1024, max_bytes=16384)
        for i in range(10):
            rec.emit("operator_marker", text="한글 기록 " * 20)
        rec.close()
        self.assertGreater(len(list(self.path.glob("events.*.jsonl"))), 1)
        manifest = verify(self.path)
        self.assertEqual(manifest["status"], "closed")
        self.assertEqual([e["event_seq"] for e in load_events(self.path)], list(range(1, 11)))
        with self.assertRaises(FileExistsError):
            Recorder(self.path, {})

    def test_budget_failure_manifest(self):
        rec = self.recorder(rotate_bytes=1024, max_bytes=1024)
        with self.assertRaises(RecordingError):
            rec.emit("test", body_hex="aa" * 1024)
        rec.close()
        manifest = verify(self.path)
        self.assertEqual(manifest["status"], "storage_failed")
        self.assertEqual(manifest["loss_count"], 1)

    def test_low_disk_failure(self):
        rec = self.recorder()
        with patch("nctr_lab.recording.shutil.disk_usage") as usage:
            usage.return_value.free = 0
            with self.assertRaises(RecordingError):
                rec.emit("test")
        self.assertTrue(rec.failed)

    def test_active_and_tampered_run_rejected(self):
        rec = self.recorder()
        rec.emit("test")
        with self.assertRaises(ValueError):
            verify(self.path)
        rec.close()
        event_file = next(self.path.glob("events.*.jsonl"))
        event_file.write_bytes(event_file.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify(self.path)

    def test_manifest_path_escape_rejected(self):
        rec = self.recorder()
        rec.close()
        manifest = json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))
        outside = self.path.parent / "outside.jsonl"
        outside.write_text("secret", encoding="ascii")
        manifest["file_hashes"] = {"../outside.jsonl": hashlib.sha256(b"secret").hexdigest()}
        (self.path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            verify(self.path)

    def test_truncated_raw_reports_diagnostic_without_sending(self):
        rec = self.recorder()
        rec.emit("socket_bytes", "synthetic", channel="raw", direction="rx", offset=0, data_hex="00")
        rec.close()
        raw = next((self.path / "raw").glob("*.jsonl"))
        raw.write_bytes(b'{"incomplete":')
        manifest_path = self.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["file_hashes"][raw.relative_to(self.path).as_posix()] = hashlib.sha256(raw.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with patch("socket.socket", side_effect=AssertionError("offline must not create sockets")):
            self.assertEqual(len(replay(self.path)["errors"]), 1)

    def test_concurrent_event_sequences(self):
        rec = self.recorder()
        workers = [threading.Thread(target=lambda: [rec.emit("test") for _ in range(10)])
                   for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        rec.close()
        self.assertEqual([e["event_seq"] for e in load_events(self.path)], list(range(1, 31)))

    def test_attachment_is_preserved_hashed_and_never_overwritten(self):
        rec = self.recorder()
        rec.emit("test")
        source = self.path.parent / "network.pcap"
        source.write_bytes(b"synthetic-capture")
        with self.assertRaises(ValueError):
            attach(self.path, source)
        rec.close()
        result = attach(self.path, source)
        self.assertEqual(result["attachment"], "network/network.pcap")
        self.assertIn(result["attachment"], verify(self.path)["attachment_hashes"])
        with self.assertRaises(FileExistsError):
            attach(self.path, source)
        (self.path / result["attachment"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "attachment hash mismatch"):
            verify(self.path)


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(mode="handshake", time_tail=b"\x01\x23", port=0)
        self.rec = Recorder(self.root / "run", self.config.manifest(), reserve_bytes=0)
        self.server = LabServer(self.config, self.rec)
        self.server.start()
        self.addCleanup(self.finish)
        self.client = socket.create_connection(self.server.listener.getsockname(), timeout=2)
        self.client.settimeout(2)
        self.addCleanup(self.client.close)

    def finish(self):
        self.server.stop()
        self.rec.close()

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("loopback condition timed out")

    def test_collect_replay_analyze_and_metadata_export(self):
        wire = packet(1, TOKEN, NODE) + packet(0x01FF, TOKEN, bytes(103))
        for part in (wire[:3], wire[3:17], wire[17:]):
            self.client.sendall(part)
        self.wait_for(lambda: self.server.status()["ready"])
        changed = bytearray(103)
        changed[16], changed[32] = 22, 23
        self.client.sendall(packet(0x01FF, b"\x22\x22", changed))
        self.client.sendall(packet(0x7777, TOKEN, bytes(range(256))))
        self.wait_for(lambda: any(e["event_type"] == "unknown_event" for e in load_events(self.rec.path)))
        self.server.mark("LCD 목표 23")
        self.client.close()
        self.wait_for(lambda: not self.server.status()["connected"])
        self.finish()
        with patch("socket.socket", side_effect=AssertionError("offline must not create sockets")):
            report = analyze(self.rec.path, self.root / "analysis")
            self.assertTrue(report["replay_matches_logged_frames"])
            self.assertEqual({d["offset"] for d in report["state_diffs"]}, {16, 32})
            self.assertEqual(report["unknown_events"][0]["mid"], "7777")
            self.assertEqual(replay(self.rec.path), replay(self.rec.path))
            destination = self.root / "shared.jsonl"
            export_metadata(self.rec.path, destination)
            shared = destination.read_text(encoding="utf-8")
            self.assertNotIn(NODE.hex(), shared)
            self.assertNotIn("body_hex", shared)
            self.assertNotIn("127.0.0.1", shared)
            self.assertNotIn("목표", shared)

    def test_malformed_stream_preserves_residual(self):
        self.client.sendall(b"\x77\x77\x00\x01")
        self.wait_for(lambda: self.server.session() is not None and not self.server.session().alive)
        self.finish()
        result = replay(self.rec.path)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(next(iter(result["residuals"].values())), "77770001")

    def test_rejects_second_connection_when_replacement_disabled(self):
        self.server.config.replace_session = False
        self.wait_for(lambda: self.server.session() is not None)
        with socket.create_connection(self.server.listener.getsockname(), timeout=2) as other:
            other.settimeout(2)
            self.assertEqual(other.recv(1), b"")
        self.assertTrue(any(e["event_type"] == "connection_rejected" for e in load_events(self.rec.path)))

    def test_new_connection_replaces_silent_session(self):
        # Device re-associates Wi-Fi and abandons the old TCP session without FIN/RST.
        self.wait_for(lambda: self.server.session() is not None)
        old = self.server.session()
        with socket.create_connection(self.server.listener.getsockname(), timeout=2) as other:
            other.settimeout(2)
            self.wait_for(lambda: self.server.session() is not old)
            other.sendall(packet(1, TOKEN, NODE) + packet(0x01FF, TOKEN, bytes(103)))
            self.wait_for(lambda: self.server.status()["ready"])
            self.assertEqual(self.client.recv(1), b"")
        events = load_events(self.rec.path)
        kinds = [e["event_type"] for e in events]
        self.assertIn("session_replaced", kinds)
        self.assertNotIn("connection_rejected", kinds)
        old_events = [e for e in events if e.get("session_id") == old.id]
        self.assertNotIn("connection_error", [e["event_type"] for e in old_events])
        self.assertEqual([e.get("reason") for e in old_events if e["event_type"] == "disconnected"],
                         ["replaced_by_new_connection"])

    def test_idle_session_is_closed(self):
        self.server.config.idle_timeout = 0.5
        self.wait_for(lambda: self.server.session() is not None)
        self.wait_for(lambda: not self.server.status()["connected"])
        events = load_events(self.rec.path)
        self.assertTrue(any(e["event_type"] == "idle_timeout" for e in events))
        self.assertFalse(any(e["event_type"] == "connection_error" for e in events))

    def test_silent_connection_is_closed_and_loop_is_flagged(self):
        self.server.config.silent_timeout = 0.4
        self.server.config.silent_close_alert = 2
        self.wait_for(lambda: self.server.session() is not None)
        self.wait_for(lambda: not self.server.status()["connected"])
        self.assertEqual(self.client.recv(1), b"")
        with socket.create_connection(self.server.listener.getsockname(), timeout=2) as again:
            again.settimeout(2)
            self.wait_for(lambda: self.server.status()["silent_closes"] == 2)
            events = load_events(self.rec.path)
            self.assertEqual(sum(e["event_type"] == "silent_timeout" for e in events), 2)
            self.assertTrue(any(e["event_type"] == "silent_reconnect_loop" for e in events))
        with socket.create_connection(self.server.listener.getsockname(), timeout=2) as good:
            good.sendall(packet(1, TOKEN, NODE))
            self.wait_for(lambda: self.server.status().get("identity"))
            self.assertEqual(self.server.status()["silent_closes"], 0)

    def test_resumed_session_without_0001_is_kept_and_ready(self):
        # field-20261122-24: the device resumed with 01FF only and was cut by the old rule.
        self.server.config.silent_timeout = 0.4
        self.wait_for(lambda: self.server.session() is not None)
        self.client.sendall(packet(0x01FF, TOKEN, bytes(103)))
        self.wait_for(lambda: self.server.status()["ready"])
        time.sleep(0.8)
        status = self.server.status()
        self.assertTrue(status["connected"] and status["resumed"] and not status["identity"])
        events = load_events(self.rec.path)
        self.assertTrue(any(e["event_type"] == "session_resumed" for e in events))
        self.assertFalse(any(e["event_type"] == "silent_timeout" for e in events))

    def test_stop_is_not_recorded_as_connection_error(self):
        self.client.sendall(packet(1, TOKEN, NODE))
        self.wait_for(lambda: self.server.status().get("identity"))
        self.finish()
        events = load_events(self.rec.path)
        self.assertFalse(any(e["event_type"] == "connection_error" for e in events))
        reasons = [e.get("reason") for e in events if e["event_type"] == "disconnected"]
        self.assertEqual(reasons, ["server_stop"])

    def test_recording_failure_closes_socket_and_listener(self):
        self.wait_for(lambda: self.server.session() is not None)
        self.rec.max_bytes = self.rec.total_bytes
        self.client.sendall(packet(1, TOKEN, NODE))
        self.wait_for(lambda: self.server.stopping.is_set())
        self.assertTrue(self.rec.failed)
        try:
            closed = self.client.recv(1) == b""
        except ConnectionResetError:
            closed = True          # Windows may answer the unread close with RST instead of FIN
        self.assertTrue(closed)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "capture"
        self.recorder = Recorder(self.path, {"mode": "offline_capture"}, reserve_bytes=0)
        self.addCleanup(self.recorder.close)

    def row(self, seq, data, number=1, tx=False, at="100.0"):
        return [str(number), at, "0", "20000" if tx else "60000",
                "60000" if tx else "20000", str(seq), data.hex()]

    def test_out_of_order_and_retransmissions(self):
        wire = packet(1, TOKEN, NODE) + packet(0x01FF, TOKEN, bytes(103))
        rows = [self.row(1010, wire[10:], 1), self.row(1000, wire[:10], 2),
                self.row(1000, wire, 3)]
        self.assertEqual(ingest_rows(self.recorder, rows), [])
        self.recorder.close()
        report = analyze(self.path)
        self.assertEqual(report["message_counts"], {"0001": 1, "01FF": 1})
        self.assertTrue(report["replay_matches_logged_frames"])

    def test_sequence_wrap_and_legacy_server_response(self):
        wire = packet(1, TOKEN, bytes(8), declared=11) + packet(0x01FF, TOKEN)
        rows = [self.row(2**32 - 8, wire[:8], 1, True),
                self.row(0, wire[8:], 2, True)]
        self.assertEqual(ingest_rows(self.recorder, rows), [])
        events = load_events(self.path)
        frames = [e for e in events if e["event_type"] == "frame_tx"]
        self.assertEqual([e["mid"] for e in frames], ["0001", "01FF"])
        self.assertTrue(frames[0]["length_exception"])

    def test_maker_keepalive_reply_uses_legacy_length(self):
        # maker_night 00:52:02: 0003 reply with length 11, then a 01FF ACK.
        wire = bytes.fromhex("0003000b9dd51a0b170034030194" "01ff0002b71a")
        frames = list(Decoder(legacy_time_reply=True).feed(wire))
        self.assertEqual([(f.mid, f.body.hex()) for f in frames],
                         [(0x0003, "1a0b170034030194"), (0x01FF, "")])

    def test_gap_and_conflict_not_silently_repaired(self):
        wire = packet(1, TOKEN, NODE)
        rows = [self.row(100, wire), self.row(120, wire, 2)]
        warnings = ingest_rows(self.recorder, rows)
        self.assertTrue(any(w["reason"] == "tcp_gap" for w in warnings))

    def test_conflicting_retransmission(self):
        wire = packet(1, TOKEN, NODE)
        rows = [self.row(100, wire), self.row(100, packet(1, TOKEN, bytes(8)), 2)]
        warnings = ingest_rows(self.recorder, rows)
        self.assertTrue(any(w["reason"] == "conflicting_retransmission" for w in warnings))

    def test_wire_command_ack_matching_uses_capture_time(self):
        rows = [self.row(100, packet(0x0102, TOKEN, bytes((1, 0, 0, 0, 23, 0))),
                         1, True, "100.0"),
                self.row(500, packet(0x0102, TOKEN), 2, False, "100.25")]
        self.assertEqual(ingest_rows(self.recorder, rows), [])
        self.recorder.close()
        transaction = analyze(self.path)["wire_transactions"][0]
        self.assertEqual(transaction["ack_delay"], 0.25)

    def test_tshark_wrapper_keeps_output_directory_and_extraction_distinct(self):
        capture = self.path.parent / "source.pcap"
        capture.write_bytes(b"synthetic-extraction-fixture")
        destination = self.path.parent / "imported"
        row = self.row(100, packet(1, TOKEN, NODE))

        def extract(args, *, stdout, stderr, timeout):
            self.assertIn("-n", args)
            stdout.write(("\t".join(row) + "\n").encode("ascii"))
            return subprocess.CompletedProcess(args, 0, stderr=b"")

        with patch("nctr_lab.pcap_import.subprocess.run", side_effect=extract):
            result = import_capture(capture, destination, tshark="synthetic-tshark")
        self.assertEqual(result["run_dir"], str(destination))
        self.assertTrue(verify(destination)["capture_complete"])
        self.assertTrue(analyze(destination)["replay_matches_logged_frames"])


class DoctorTests(unittest.TestCase):
    def dns_exchange(self, mutate=lambda data: data):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        self.addCleanup(server.close)
        errors = []

        def respond():
            try:
                data, peer = server.recvfrom(4096)
                reply = data[:2] + struct.pack("!5H", 0x8180, 1, 1, 0, 0) + data[12:]
                reply += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("127.0.0.2")
                server.sendto(mutate(reply), peer)
            except OSError as exc:
                errors.append(exc)

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        try:
            return query_a("127.0.0.1", "igs4.krb.co.kr", port=server.getsockname()[1])
        finally:
            worker.join(timeout=3)
            self.assertFalse(errors)

    def test_dns_response(self):
        self.assertEqual(self.dns_exchange(), ["127.0.0.2"])

    def test_dns_mismatch_or_truncation(self):
        for mutate in (lambda data: b"\xff\xff" + data[2:], lambda data: data[:8],
                       lambda data: data[:2] + b"\x83\x80" + data[4:]):
            with self.assertRaises(ValueError):
                self.dns_exchange(mutate)

    def test_dns_check_is_not_device_path_proof(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("nctr_lab.doctor.query_a", side_effect=[["127.0.0.2"], ["203.0.113.5"]]):
                report = check_environment("127.0.0.1", 0, root, "127.0.0.1", "127.0.0.2")
                self.assertTrue(report["ok"])
                self.assertFalse(report["device_dns_path_proven"])

    def test_doctor_invalid_port_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                check_environment("127.0.0.1", 65536, root)


class CLITests(unittest.TestCase):
    def test_invalid_config_before_listener(self):
        for config in (Config(mode="handshake"), Config(state_ttl=float("nan")),
                       Config(state_length=0), Config(bind="127.1"), Config(pair_code=10000)):
            with self.assertRaises(ValueError):
                config.validate()

    BASE = ["collect", "--bind", "127.0.0.1", "--allowed-client-ip", "127.0.0.1",
            "--mode", "handshake", "--time-tail", "0123"]

    def test_cli_enables_every_command_by_default_with_manual_ranges(self):
        from nctr_lab_cli import build_config
        config = build_config(parser().parse_args(self.BASE))
        config.validate()
        for flag in ("enable_temperature", "enable_power", "enable_hot_water",
                     "enable_bath", "enable_away", "enable_ondol", "enable_hot_water_only",
                     "enable_reservation", "enable_heating_water", "enable_schedule"):
            self.assertTrue(getattr(config, flag), flag)
        self.assertEqual((config.temp_min, config.temp_max), (10, 45))
        self.assertEqual((config.hot_water_min, config.hot_water_max), (35, 60))
        self.assertEqual((config.ondol_min, config.ondol_max), (45, 85))
        legacy = build_config(parser().parse_args(self.BASE + ["--enable-temperature"]))
        self.assertTrue(legacy.enable_temperature)

    def test_cli_can_disable_commands(self):
        from nctr_lab_cli import build_config
        none = build_config(parser().parse_args(self.BASE + ["--no-control"]))
        self.assertFalse(none.any_control())
        some = build_config(parser().parse_args(self.BASE + ["--disable", "ondol", "--disable", "power"]))
        self.assertFalse(some.enable_ondol or some.enable_power)
        self.assertTrue(some.enable_hot_water and some.enable_temperature)
        observe = build_config(parser().parse_args(
            ["collect", "--bind", "127.0.0.1", "--allowed-client-ip", "127.0.0.1"]))
        observe.validate()
        self.assertFalse(observe.any_control())

    def test_bundle_excludes_raw_and_verifies_payload_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "deploy.zip"
            result = bundle(output)
            self.assertGreater(result["files"], 5)
            with zipfile.ZipFile(output) as archive:
                self.assertFalse(any(name.startswith(("experiments/", "captures/", "ADB/", "src/archive/"))
                                     for name in archive.namelist()))
                manifest = json.loads(archive.read("bundle_manifest.json"))
                for name, digest in manifest["file_hashes"].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)
                unpacked = Path(root) / "unpacked"
                archive.extractall(unpacked)
            self.assertGreater(check_bundle(unpacked)["deployment_files_verified"], 5)
            (unpacked / "src/nctr_lab_cli.py").write_text("tampered", encoding="ascii")
            with self.assertRaises(ValueError):
                check_bundle(unpacked)
            with self.assertRaises(FileExistsError):
                bundle(output)

    def test_headless_collection_subprocess(self):
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run([sys.executable, str(ROOT / "src/nctr_lab_cli.py"), "collect",
                                     "--bind", "127.0.0.1", "--port", "0",
                                     "--allowed-client-ip", "127.0.0.1", "--no-console",
                                     "--duration", "0.1", "--output-root", root, "--run-id", "smoke"],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            self.assertEqual(verify(Path(root) / "smoke")["status"], "closed")

    def test_cli_distinguishes_integrity_from_failed_collection(self):
        with tempfile.TemporaryDirectory() as root:
            rec = Recorder(Path(root) / "failed", {}, reserve_bytes=0,
                           rotate_bytes=1024, max_bytes=1024)
            with self.assertRaises(RecordingError):
                rec.emit("overflow", data="a" * 2048)
            rec.close()
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                code = main(["verify", "--run-dir", str(rec.path)])
                report = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertTrue(report["integrity_verified"])
            self.assertFalse(report["collection_clean"])


if __name__ == "__main__":
    unittest.main()
