"""Offline integrity checks, raw reconstruction, and metadata-only sharing."""

from collections import Counter
import csv
import hashlib
import json
import os
from pathlib import Path

from .protocol import Decoder
from .recording import load_events, write_private


def child_path(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("manifest path escapes the run directory")
    return path


def verify(directory):
    root = Path(directory)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") not in ("closed", "storage_failed"):
        raise ValueError("run is active or was not finalized; stop the collector first")
    hashes = manifest.get("file_hashes", {})
    actual = {p.relative_to(root).as_posix() for pattern in ("*.jsonl", "raw/*.jsonl")
              for p in root.glob(pattern)}
    if actual != set(hashes):
        raise ValueError("run file inventory differs from manifest")
    for name, expected in hashes.items():
        digest = hashlib.sha256()
        with child_path(root, name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"hash mismatch: {name}")
    for name, expected in manifest.get("attachment_hashes", {}).items():
        digest = hashlib.sha256()
        with child_path(root, name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"attachment hash mismatch: {name}")
    return manifest


def attach(directory, source, category="network"):
    if category not in ("network", "observations", "media"):
        raise ValueError("invalid attachment category")
    root, source = Path(directory), Path(source)
    if not source.is_file():
        raise ValueError("attachment source is not a file")
    lock_path = root / "attachment.lock"
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(lock_fd)
    try:
        manifest = verify(root)
        folder = root / category
        folder.mkdir(mode=0o700, exist_ok=True)
        destination = child_path(root, f"{category}/{source.name}")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        name = destination.relative_to(root.resolve()).as_posix()
        manifest.setdefault("attachment_hashes", {})[name] = digest.hexdigest()
        temporary = root / "attachment_manifest.tmp"
        write_private(temporary, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        os.replace(temporary, root / "manifest.json")
        return {"attachment": name, "sha256": digest.hexdigest()}
    finally:
        lock_path.unlink()


def replay(directory):
    """Decode recorded RX chunks without opening any network sockets."""
    verify(directory)
    decoders = {}
    blocked = set()
    rows, errors = [], []
    paths = sorted((Path(directory) / "raw").glob("traffic.*.jsonl"))
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                event = {}
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("raw event must be an object")
                    if event["event_type"] != "socket_bytes":
                        continue
                    if event["direction"] != "rx":
                        continue
                    session = event["session_id"]
                    if session in blocked:
                        continue
                    if session not in decoders:
                        decoders[session] = Decoder()
                    decoder = decoders[session]
                    if event["offset"] != decoder.offset + len(decoder.buffer):
                        raise ValueError("raw RX offset discontinuity")
                    for frame in decoder.feed(bytes.fromhex(event["data_hex"])):
                        rows.append({"session_id": session, "mid": f"{frame.mid:04X}",
                                     "token": frame.token.hex(), "body_length": len(frame.body),
                                     "body_hex": frame.body.hex(), "stream_offset": frame.offset})
                except (ValueError, KeyError, TypeError) as exc:
                    errors.append({"file": path.name, "line": line_number, "error": str(exc)})
                    # A malformed stream has no implicit resynchronization policy.
                    if isinstance(event, dict) and "session_id" in event:
                        blocked.add(event["session_id"])
                    else:
                        break
    return {"frames": rows, "errors": errors, "residuals": {
        s: bytes(d.buffer).hex() for s, d in decoders.items() if d.buffer}}


def load_novel(directory):
    """Messages the collector diverted to novel.*.jsonl (unlike past recordings)."""
    rows = []
    for path in sorted(Path(directory).glob("novel.*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            rows.extend(json.loads(line) for line in stream if line.strip())
    return sorted(rows, key=lambda row: row["event_seq"])


def summarize_novel(rows):
    groups = {}
    for row in rows:
        key = row["mid"], tuple(row["reasons"])
        group = groups.setdefault(key, {"mid": row["mid"], "reasons": list(row["reasons"]),
                                        "count": 0, "first_at": row["at"],
                                        "first_event_seq": row["event_seq"],
                                        "first_marker": (row.get("last_marker") or {}).get("text")})
        if row.get("event_type") == "novel_message_repeated":
            group["count"] = max(group["count"], row["repeat_total"])
        else:
            group["count"] += 1
    return list(groups.values())


def analyze(directory, output=None):
    manifest = verify(directory)
    events = load_events(directory)
    novel = summarize_novel(load_novel(directory))
    messages = Counter()
    lengths = Counter()
    actions, commands, diffs = [], [], []
    unknown = Counter()
    transmitted = Counter()
    previous = {}
    frame_events = []
    sessions = set()
    for event in events:
        kind, sid = event["event_type"], event["session_id"]
        if sid:
            sessions.add(sid)
        if kind == "frame_rx":
            frame_events.append(("rx", event))
            messages[event["mid"]] += 1
            lengths[(event["mid"], event["body_length"])] += 1
            if event["mid"] == "01FF":
                data = bytes.fromhex(event["body_hex"])
                key = sid, len(data)
                if key in previous:
                    before_seq, before = previous[key]
                    for index, (a, b) in enumerate(zip(before, data)):
                        if a != b:
                            diffs.append({"session_id": sid, "before_event": before_seq,
                                          "after_event": event["event_seq"], "offset": index,
                                          "before": a, "after": b})
                previous[key] = event["event_seq"], data
        elif kind == "unknown_event":
            unknown[(event["mid"], event["body_length"])] += 1
        elif kind in ("frame_tx", "tx_attempt"):
            transmitted[event["mid"]] += 1
            frame_events.append(("tx", event))
        elif kind == "operator_marker":
            actions.append(event)
        if kind.startswith("command_") or kind == "lcd_observation":
            commands.append(event)
    reconstructed = replay(directory)
    logged_frames = [{k: e[k] for k in ("session_id", "mid", "token", "body_length",
                                      "body_hex", "stream_offset")}
                     for e in events if e["event_type"] == "frame_rx"]
    transactions, pending = [], {}
    for direction, event in sorted(frame_events, key=lambda pair: (
            float(pair[1].get("capture_at", pair[1]["monotonic"])), pair[1]["event_seq"])):
        key = event["session_id"], event["mid"], event["token"]
        body_length = len(bytes.fromhex(event["body_hex"]))
        if direction == "tx" and int(event["mid"], 16) >= 0x0100 and body_length:
            entry = {"session_id": key[0], "mid": key[1], "token": key[2],
                     "tx_event": event["event_seq"], "ack_event": None,
                     "tx_time": float(event.get("capture_at", event["monotonic"]))}
            transactions.append(entry)
            pending[key] = entry
        elif direction == "rx" and not body_length and key in pending:
            entry = pending.pop(key)
            entry["ack_event"] = event["event_seq"]
            entry["ack_delay"] = float(event.get("capture_at", event["monotonic"])) - entry["tx_time"]
    report = {
        "run_id": manifest["run_id"], "status": manifest["status"],
        "reason": manifest.get("reason"),
        "loss_count": manifest.get("loss_count"), "session_count": len(sessions),
        "message_counts": dict(sorted(messages.items())),
        "transmitted_message_counts": dict(sorted(transmitted.items())),
        "capture_warnings": manifest.get("capture_warnings", []),
        "wire_transactions": transactions,
        "length_counts": [{"mid": mid, "body_length": n, "count": count}
                          for (mid, n), count in sorted(lengths.items())],
        "unknown_events": [{"mid": mid, "body_length": n, "count": count}
                           for (mid, n), count in sorted(unknown.items())],
        "actions": actions, "commands": commands, "state_diffs": diffs,
        "novel_messages": novel,
        "replay_matches_logged_frames": reconstructed["frames"] == logged_frames,
        "replay_errors": reconstructed["errors"],
        "residuals": reconstructed["residuals"],
        "interpretation": "raw differences and candidate reports; no automatic field confirmation",
    }
    if output is not None:
        target = Path(output)
        target.mkdir(parents=True, mode=0o700, exist_ok=False)
        write_private(target / "summary.json",
                      json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
        with (target / "state_diffs.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("session_id", "before_event", "after_event",
                                                        "offset", "before", "after"))
            writer.writeheader()
            writer.writerows(diffs)
        write_private(target / "result.md", (
            "# 실행 결과\n"
            + f"\n실행 ID: `{manifest['run_id']}`\n\n"
            + f"- 종료 상태: `{manifest['status']}`\n"
            + f"- 기록 누락: {manifest.get('loss_count')}\n"
            + f"- 세션 수: {len(sessions)}\n"
            + f"- RX 원본 재생과 프레임 로그 일치: {report['replay_matches_logged_frames']}\n"
            + f"- 재생 오류 수: {len(reconstructed['errors'])}\n"
            + f"- 신규 메시지(novel.*.jsonl): {sum(g['count'] for g in novel)}건, "
            + f"{len(novel)}종\n\n"
            + "".join(f"  - `{g['mid']}` {', '.join(g['reasons'])} ×{g['count']} "
                      f"(처음 {g['first_at']}, 직전 marker: {g['first_marker'] or '없음'})\n"
                      for g in novel)
            + ("\n" if novel else "")
            + "기능별 실물 판정과 복구 결과는 현장 관측을 대조해 추가해야 합니다.\n"
        ).encode("utf-8"))
    return report


def export_metadata(directory, output):
    """Only allowlisted numeric/protocol metadata is shared by default."""
    verify(directory)
    sessions = {}
    rows = []
    for event in load_events(directory):
        sid = event["session_id"]
        if sid not in sessions:
            sessions[sid] = f"session-{len(sessions) + 1}" if sid else None
        row = {"session": sessions[sid], "event_seq": event["event_seq"],
               "event_type": event["event_type"], "at": event["at"]}
        for key in ("mid", "body_length", "declared", "actual_length", "ready"):
            if key in event:
                row[key] = event[key]
        rows.append(row)
    write_private(Path(output), ("\n".join(json.dumps(row, ensure_ascii=False)
                                          for row in rows) + "\n").encode("utf-8"))
    return len(rows)
