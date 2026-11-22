#!/usr/bin/env python3
"""Cross-platform CLI; the real-device listener runs on the Ubuntu host."""

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import zipfile

from nctr_lab.analysis import analyze, attach, child_path, export_metadata, replay, verify
from nctr_lab.doctor import check_environment
from nctr_lab.messages import COMMANDS, parse_schedule
from nctr_lab.provision import (AP_ADDRESS, ProvisionPlan, default_abcd, exchange, resolved_name,
                                single_command, write_record)
from nctr_lab.protocol import parse_ack_rule
from nctr_lab.pcap_import import import_capture
from nctr_lab.recording import KST, Recorder, RecordingError
from nctr_lab.server import Config, DEFAULT_ACK_RULES, KEEPALIVE_REPLIES, LabServer


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


COMMAND_WORDS = {"temp": "temperature", "power": "power", "hotwater": "hot_water",
                 "ondol": "ondol", "bath": "bath", "away": "away",
                 "hotwateronly": "hot_water_only", "reserve": "reservation",
                 "heatwater": "heating_water", "schedule": "schedule"}
HELP = ("명령: status | device | mark 설명 | temp 값 | hotwater 값 | ondol 값 | bath | away | "
        "hotwateronly [값] | reserve 정지시간 가동분 | heatwater 값 | "
        "schedule 00:00-09:00,18:00-24:00 | power on/off | "
        "lcd 명령ID 값 | cancel | quit")


def command_value(word, rest, device=None):
    if word == "hotwateronly" and not rest.strip():
        # Keep the current hot-water setting; the 0114 report carried it in byte4.
        item = (device or {}).get("hot_water_target")
        return item["value"] if item else 60
    if word == "power":
        return {"on": 1, "off": 2}[rest.strip()]
    if word == "schedule":
        return parse_schedule(rest)
    if word == "reserve":
        stop_hours, run_minutes = (int(part) for part in rest.split())
        return (stop_hours, run_minutes)
    if word in ("bath", "away"):
        if rest.strip():
            raise ValueError(f"{word}는 값을 받지 않습니다")
        return 1
    return int(rest)


def lcd_value(text):
    """`lcd ID 45`, `lcd ID 3:20` (reservation), `lcd ID 00:00-09:00,18:00-24:00` (schedule)."""
    if "-" in text:
        return parse_schedule(text)
    if ":" in text:
        stop_hours, run_minutes = text.split(":")
        return (int(stop_hours), int(run_minutes))
    return int(text)


def shell(server):
    print(HELP, flush=True)
    while not server.stopping.is_set():
        try:
            line = input("nctr> ").strip()
        except EOFError:
            break
        if not line:
            continue
        command, _, rest = line.partition(" ")
        try:
            if command in ("quit", "exit"):
                break
            if command == "status":
                print_json(server.status())
            elif command == "mark":
                print("action_id=" + server.mark(rest), flush=True)
            elif command == "device":
                print_json(dict(server.device))
            elif command in COMMAND_WORDS:
                session = server.session()
                if session is None:
                    raise ValueError("현재 연결이 없습니다")
                feature = COMMAND_WORDS[command]
                value = command_value(command, rest, dict(server.device))
                spec = COMMANDS[feature]
                print(f"실물 설정 변경 요청: {command} {rest}".rstrip()
                      + f" → {spec.mid:04X} {spec.body(value).hex()}. 정상 모드와 복구값을 확인하십시오.")
                if spec.format_source.startswith("UNVERIFIED"):
                    print("주의: 제조사 형식을 확인하지 못한 명령입니다. " + spec.format_source)
                if input("한 번 전송하려면 SEND 입력: ").strip() != "SEND":
                    print("취소했습니다. 전송하지 않았습니다.")
                    continue
                if server.session() is not session:
                    raise ValueError("확인 도중 연결이 바뀌었습니다")
                ident = session.command(feature, value, confirmed=True)
                print("command_id=" + ident, flush=True)
            elif command == "lcd":
                ident, value = shlex.split(rest)
                session = server.session()
                if not session:
                    raise ValueError("현재 연결이 없습니다")
                session.confirm_lcd(ident, lcd_value(value))
                print("LCD 관측을 기록했습니다. 패킷·모드·조작 이력을 함께 대조하십시오.")
            elif command == "cancel":
                session = server.session()
                if session:
                    session.cancel()
                print("대기 추적을 취소했습니다. 이미 보낸 명령은 원상복구되지 않습니다.")
            else:
                print("알 수 없는 명령입니다. " + HELP)
        except (ValueError, KeyError) as exc:
            print("차단 또는 입력 오류: " + str(exc), flush=True)


CONTROL_FEATURES = ("temperature", "power", "hot_water", "bath", "away", "ondol", "hot_water_only",
                    "reservation", "heating_water", "schedule")


def build_config(args):
    """CLI defaults: every remote command is enabled in handshake mode (2026-11-22).

    Commands still require a ready session, configured ranges and a typed SEND.
    --no-control or --disable FEATURE turns them off; observe mode never transmits.
    """
    node = None
    if args.node_id_file:
        node = bytes.fromhex(Path(args.node_id_file).read_text(encoding="ascii").strip())
    extra_rules = tuple(parse_ack_rule(value) for value in args.ack_event)
    if (extra_rules or args.state_length != 103 or args.time_length != "legacy-11") and not args.policy_note:
        raise ValueError("changed protocol rules require --policy-note with evidence")
    off = set(CONTROL_FEATURES) if args.no_control or args.mode != "handshake" else set(args.disable)
    enabled = {f"enable_{feature}": feature not in off for feature in CONTROL_FEATURES}
    return Config(bind=args.bind, port=args.port, allowed_client_ip=args.allowed_client_ip,
                  mode=args.mode, time_tail=bytes.fromhex(args.time_tail or ""),
                  time_length=args.time_length, state_length=args.state_length,
                  state_ttl=args.state_ttl, ack_timeout=args.ack_timeout,
                  report_timeout=args.report_timeout,
                  ack_rules=tuple(dict.fromkeys(DEFAULT_ACK_RULES + extra_rules)),
                  policy_note=args.policy_note or "v4-observed-events",
                  temp_min=args.temp_min, temp_max=args.temp_max,
                  hot_water_min=args.hot_water_min, hot_water_max=args.hot_water_max,
                  ondol_min=args.ondol_min, ondol_max=args.ondol_max,
                  pair_code=args.pair_code, expected_node=node,
                  replace_session=not args.no_replace_session,
                  ack_unlisted_events=not args.no_ack_unlisted,
                  keepalive_reply="none" if args.no_keepalive_reply else args.keepalive_reply,
                  idle_timeout=args.idle_timeout, silent_timeout=args.silent_timeout,
                  accept_resumed_session=not args.no_resumed_session,
                  **enabled)


def collect(args):
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0 or not args.no_console):
        raise ValueError("--duration requires --no-console and a positive duration")
    config = build_config(args)
    config.validate()
    run_id = args.run_id or datetime.now(KST).strftime("%Y%m%d-%H%M%S-%f")
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
        raise ValueError("run ID must contain only ASCII letters, digits, hyphens and underscores")
    recorder = Recorder(Path(args.output_root) / run_id, config.manifest(),
                        rotate_bytes=args.rotate_mib * 1024 * 1024,
                        max_bytes=args.max_mib * 1024 * 1024,
                        reserve_bytes=args.reserve_mib * 1024 * 1024)
    server = LabServer(config, recorder)
    reason = "normal"
    try:
        server.start()
        print("수집 폴더: " + str(recorder.path.resolve()), flush=True)
        print("수집 중입니다. 원본에는 전체 식별자·미해독 데이터가 포함됩니다.", flush=True)
        if args.no_console:
            deadline = None if args.duration is None else time.monotonic() + args.duration
            while not server.stopping.wait(0.1):
                if deadline is not None and time.monotonic() >= deadline:
                    break
        else:
            shell(server)
        if server.failure:
            raise RecordingError(server.failure)
    except KeyboardInterrupt:
        reason = "interrupt"
    except BaseException:
        reason = "failure"
        raise
    finally:
        server.stop()
        recorder.close(reason)
    if server.failure:
        raise RecordingError(server.failure)
    return {"run_dir": str(recorder.path), "status": recorder.manifest["status"]}


def bundle(output):
    root = Path(__file__).resolve().parent.parent
    paths = [root / "src/nctr_lab_cli.py"]
    for pattern in ("src/nctr_lab/*.py", "tests/test_*.py", "docs/*.md", "docs/archive/*.md",
                    "tools/*.sh"):
        paths.extend(sorted(root.glob(pattern)))
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in paths}
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(root).as_posix())
        archive.writestr("bundle_manifest.json", json.dumps({"schema_version": 1,
                          "file_hashes": hashes}, ensure_ascii=False, indent=2))
    return {"bundle": str(target), "files": len(paths),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def check_bundle(directory):
    root = Path(directory)
    manifest = json.loads((root / "bundle_manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["file_hashes"].items():
        if hashlib.sha256(child_path(root, name).read_bytes()).hexdigest() != expected:
            raise ValueError("deployment file hash mismatch: " + name)
    return {"deployment_files_verified": len(manifest["file_hashes"])}


def parser():
    root = argparse.ArgumentParser(description="NCTR-200 수집·분석 도구 (Python 3.10 이상)")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("collect", help="Ubuntu 실물 또는 loopback 모의 수집")
    run.add_argument("--bind", required=True)
    run.add_argument("--port", type=int, default=20000)
    run.add_argument("--allowed-client-ip", required=True)
    run.add_argument("--mode", choices=("observe", "handshake"), default="observe")
    run.add_argument("--time-tail")
    run.add_argument("--time-length", choices=("legacy-11", "actual"), default="legacy-11")
    run.add_argument("--state-length", type=int, default=103)
    run.add_argument("--state-ttl", type=float, default=900)
    run.add_argument("--ack-timeout", type=float, default=10)
    run.add_argument("--report-timeout", type=float, default=720)
    run.add_argument("--ack-event", action="append", default=[], metavar="HEX_ID:BODY_LENGTH")
    run.add_argument("--policy-note")
    run.add_argument("--node-id-file")
    run.add_argument("--pair-code", type=int)
    run.add_argument("--no-control", action="store_true",
                     help="disable every remote command (collection only)")
    run.add_argument("--disable", action="append", default=[], choices=CONTROL_FEATURES,
                     help="disable one remote command; repeatable")
    for legacy in ("temperature", "power", "hot-water", "bath", "away", "ondol"):
        # Accepted for older command lines; every command is enabled by default.
        run.add_argument(f"--enable-{legacy}", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--temp-min", type=int, default=10, help="room target lower bound (manual 10)")
    run.add_argument("--temp-max", type=int, default=45, help="room target upper bound (manual 45)")
    run.add_argument("--hot-water-min", type=int, default=35)
    run.add_argument("--hot-water-max", type=int, default=60)
    run.add_argument("--ondol-min", type=int, default=45)
    run.add_argument("--ondol-max", type=int, default=85)
    run.add_argument("--no-replace-session", action="store_true",
                     help="reject a new connection while a session is open (pre-2026-11-21 behaviour)")
    run.add_argument("--keepalive-reply", choices=KEEPALIVE_REPLIES, default="echo",
                     help="reply to the device's empty 0003: echo (default), none, time "
                          "(0003 + 8-byte time), time6, identity (0001 time reply) or "
                          "probe (time, time6, identity, echo, 3 each per run)")
    run.add_argument("--no-keepalive-reply", action="store_true",
                     help="same as --keepalive-reply none")
    run.add_argument("--no-ack-unlisted", action="store_true",
                     help="do not acknowledge device events (0x0100-0x01FE) outside --ack-event rules")
    run.add_argument("--silent-timeout", "--identity-timeout", dest="silent_timeout",
                     type=float, default=180,
                     help="close a connection that has sent nothing within this many seconds; "
                          "0 disables")
    run.add_argument("--no-resumed-session", action="store_true",
                     help="require 0001 before readiness even if the device resumes with 01FF")
    run.add_argument("--idle-timeout", type=float, default=900,
                     help="close a session after this many seconds without received data; 0 disables")
    run.add_argument("--output-root", default="experiments")
    run.add_argument("--run-id")
    run.add_argument("--rotate-mib", type=int, default=8)
    run.add_argument("--max-mib", type=int, default=1024)
    run.add_argument("--reserve-mib", type=int, default=64)
    run.add_argument("--no-console", action="store_true")
    run.add_argument("--duration", type=float)
    doctor = commands.add_parser("doctor", help="로컬 바인딩·저장 공간·선택적 DNS 검사")
    doctor.add_argument("--bind", default="127.0.0.1")
    doctor.add_argument("--port", type=int, default=20000)
    doctor.add_argument("--output-root", default="experiments")
    doctor.add_argument("--dns-server")
    doctor.add_argument("--expected-ip")
    for name in ("analyze", "replay", "verify", "export"):
        sub = commands.add_parser(name)
        sub.add_argument("--run-dir", required=True)
        if name in ("analyze", "export"):
            sub.add_argument("--output", required=True)
    pack = commands.add_parser("bundle", help="Windows에서 Ubuntu 배포 ZIP 작성")
    pack.add_argument("--output", required=True)
    check = commands.add_parser("check-bundle", help="압축 해제한 배포 파일 해시 확인")
    check.add_argument("--root", default=".")
    attachment = commands.add_parser("attach", help="종료된 실행에 PCAP·관측 자료와 해시 추가")
    attachment.add_argument("--run-dir", required=True)
    attachment.add_argument("--input", required=True)
    attachment.add_argument("--category", choices=("network", "observations", "media"), default="network")
    setup = commands.add_parser("provision",
                                help="조절기 설정 AP(krb_set)에 Wi-Fi와 서버 주소를 직접 설정")
    setup.add_argument("--ssid", help="조절기가 접속할 Wi-Fi 이름(2.4GHz, 영문)")
    setup.add_argument("--server-ip", help="수집 서버(NAS) IP. SCON과 ABCD 기본값")
    setup.add_argument("--scon", help="AT+SCON의 IP(기본: --server-ip)")
    setup.add_argument("--abcd", help="AT+ABCD 값(기본: http://nas.<--server-ip>.nip.io, 앱은 "
                       "http://igs4.krb.co.kr). 조절기는 앞 3글자 뒤에 4를 넣어 DNS로 조회함")
    setup.add_argument("--server-port", type=int, default=20000)
    setup.add_argument("--password-file", help="Wi-Fi 암호 파일(없으면 입력 프롬프트)")
    setup.add_argument("--query", help="설정 대신 조회 명령 하나만 보냄(예: AT+MINFO)")
    setup.add_argument("--target", default=AP_ADDRESS[0])
    setup.add_argument("--target-port", type=int, default=AP_ADDRESS[1])
    setup.add_argument("--bind", help="보낼 때 쓸 로컬 IP(krb_set에 붙은 어댑터, 예: 192.168.200.2)")
    setup.add_argument("--timeout", type=float, default=5.0)
    setup.add_argument("--source-port", type=int, default=0,
                       help="보내는 쪽 UDP 포트(0=임의). 조절기가 20000번에서만 받거나 답할 수 있음")
    setup.add_argument("--line-ending", choices=("lf", "cr", "crlf"), default="cr",
                       help="명령 구분 문자. 앱은 CR(11-22 휴대폰 캡처). LF는 조절기가 무시함")
    setup.add_argument("--reset", choices=("auto", "always", "never"), default="auto",
                       help="AT+MRESET: auto=응답이 있고 ERROR가 아닐 때만(앱과 같음)")
    setup.add_argument("--record-dir", default="provision-records")
    setup.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    capture = commands.add_parser("import-pcap", help="TShark를 이용한 오프라인 PCAP 재조립")
    capture.add_argument("--input", required=True)
    capture.add_argument("--output", required=True)
    capture.add_argument("--tshark")
    capture.add_argument("--server-port", type=int, default=20000)
    capture.add_argument("--time-length", choices=("legacy-11", "actual"), default="legacy-11")
    return root


def read_password(args):
    if args.password_file:
        return Path(args.password_file).read_text(encoding="utf-8").rstrip("\r\n")
    import getpass
    first = getpass.getpass("조절기가 접속할 Wi-Fi 암호(화면에 표시되지 않음): ")
    if getpass.getpass("같은 암호를 한 번 더 입력: ") != first:
        raise ValueError("두 번 입력한 암호가 다릅니다. 전송하지 않았습니다")
    return first


def provision(args, *, ask=input):
    """Send the app-equivalent setup datagram to the controller AP (krb_set)."""
    target = (args.target, args.target_port)
    record = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "target": list(target),
              "source_port": args.source_port}
    if args.query:
        payload = single_command(args.query, args.line_ending)
        record.update(kind="query", command=args.query)
        print(f"조회 명령: {args.query} → {target[0]}:{target[1]}/UDP")
    else:
        server = args.server_ip
        plan = ProvisionPlan(ssid=args.ssid, password=read_password(args),
                             scon_host=args.scon or server,
                             abcd_value=args.abcd or default_abcd(server),
                             port=args.server_port, line_ending=args.line_ending).validate()
        payload = plan.payload()
        record.update(kind="provision", ssid=plan.ssid, scon=plan.scon_host,
                      line_ending=plan.line_ending,
                      abcd=plan.abcd_value, server_port=plan.port,
                      password_length=len(plan.password), datagram_masked=plan.masked_text())
        print(f"조절기가 DNS로 물을 이름(예상): {resolved_name(plan.abcd_value)}. "
              "이 이름이 공개 DNS에서 서버 IP로 풀려야 합니다.")
        record["expected_dns_name"] = resolved_name(plan.abcd_value)
        print(f"{target[0]}:{target[1]}/UDP 로 보낼 내용(암호 {len(plan.password)}자, 가림):")
        print(plan.masked_text())
    if not args.yes and ask("한 번 전송하려면 SEND 입력: ").strip() != "SEND":
        print("취소했습니다. 전송하지 않았습니다.")
        return {"sent": False}
    replies = exchange(payload, target=target, bind=args.bind, source_port=args.source_port,
                       timeout=args.timeout)
    record["replies"] = replies
    print(f"응답 {len(replies)}건" + "".join(f"\n  {r['from'][0]}: {r['text']!r}" for r in replies))
    if not args.query:
        failed = any("ERROR" in r["text"].upper() for r in replies)
        record["error_reply"] = failed
        want = args.reset == "always" or (args.reset == "auto" and replies and not failed)
        if failed:
            print("조절기가 ERROR로 응답했습니다. 재시작 명령을 보내지 않습니다.")
        elif not want:
            record["reset_sent"] = False
            print("재시작 명령은 보내지 않습니다(응답이 없을 때 앱도 보내지 않음). 조절기가 받아들였다면 "
                  "1초 안팎에 krb_set이 사라집니다. PC의 Wi-Fi 연결이 끊겼는지 확인하십시오.")
        elif args.yes or ask("설정을 적용하려면 재시작(AT+MRESET)을 보냅니다. RESET 입력: ").strip() == "RESET":
            record["reset_replies"] = exchange(single_command("AT+MRESET", args.line_ending),
                                               target=target, source_port=args.source_port,
                                               bind=args.bind, timeout=2.0)
            record["reset_sent"] = True
            print("AT+MRESET을 보냈습니다. 조절기가 새 Wi-Fi에 붙고 서버에 접속하는지 확인하십시오.")
        else:
            record["reset_sent"] = False
            print("재시작은 보내지 않았습니다.")
    path = write_record(args.record_dir, record)
    return {"sent": True, "replies": len(replies), "record": path}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.command == "collect":
            value = collect(args)
        elif args.command == "doctor":
            value = check_environment(args.bind, args.port, args.output_root,
                                      args.dns_server, args.expected_ip)
            print_json(value)
            return 0 if value["ok"] else 1
        elif args.command == "bundle":
            value = bundle(args.output)
        elif args.command == "check-bundle":
            value = check_bundle(args.root)
        elif args.command == "attach":
            value = attach(args.run_dir, args.input, args.category)
        elif args.command == "provision":
            if not args.query and not (args.ssid and args.server_ip):
                raise ValueError("provision needs --ssid and --server-ip (or --query)")
            value = provision(args)
        elif args.command == "import-pcap":
            if not 1 <= args.server_port <= 65535:
                raise ValueError("invalid server port")
            value = import_capture(args.input, args.output, tshark=args.tshark,
                                   server_port=args.server_port,
                                   legacy_time=args.time_length == "legacy-11")
        elif args.command == "analyze":
            report = analyze(args.run_dir, args.output)
            value = {key: report[key] for key in ("run_id", "status", "session_count",
                     "message_counts", "replay_matches_logged_frames", "replay_errors")}
        elif args.command == "export":
            value = {"metadata_events": export_metadata(args.run_dir, args.output),
                     "payloads_included": False}
        elif args.command == "verify":
            manifest = verify(args.run_dir)
            clean = (manifest["status"] == "closed" and not manifest.get("loss_count")
                     and manifest.get("reason") not in ("failure", "import_failure")
                     and not manifest.get("capture_warnings"))
            value = {"status": manifest["status"], "files": len(manifest["file_hashes"]),
                     "loss_count": manifest.get("loss_count"), "integrity_verified": True,
                     "collection_clean": clean}
            print_json(value)
            return 0 if clean else 2
        else:
            value = replay(args.run_dir)
        print_json(value)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("실행 오류: " + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
