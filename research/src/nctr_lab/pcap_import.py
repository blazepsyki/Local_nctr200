"""Offline TShark extraction and sequence-aware TCP stream reconstruction."""

from collections import defaultdict
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

from .protocol import Decoder, ProtocolError
from .recording import Recorder


FIELDS = ("frame.number", "frame.time_epoch", "tcp.stream", "tcp.srcport",
          "tcp.dstport", "tcp.seq_raw", "tcp.payload")


def ingest_rows(recorder, rows, *, server_port=20000, legacy_time=True):
    groups = defaultdict(list)
    for row in rows:
        direction = "tx" if int(row[3]) == server_port else "rx"
        sid = "capture-" + row[2]
        payload = bytes.fromhex(row[6].replace(":", ""))
        seq = int(row[5])
        recorder.emit("capture_segment", sid, channel="raw", direction=direction,
                      packet_number=int(row[0]), capture_at=row[1], tcp_sequence=seq,
                      data_hex=payload.hex())
        groups[sid, direction].append((seq, payload, int(row[0]), row[1]))
    warnings = []
    for (sid, direction), segments in sorted(groups.items()):
        first = segments[0][0]
        lifted = [(((seq - first + 2**31) % 2**32) - 2**31, data, index, at)
                  for seq, data, index, at in segments]
        lifted.sort(key=lambda item: (item[0], item[2]))
        base = lifted[0][0]
        assembled = bytearray()
        decoder = Decoder(legacy_time_reply=direction == "tx" and legacy_time)
        blocked = False
        for seq, data, index, at in lifted:
            offset = seq - base
            if offset > len(assembled):
                warnings.append({"session_id": sid, "direction": direction,
                                 "reason": "tcp_gap", "packet": index,
                                 "expected_offset": len(assembled), "actual_offset": offset})
                recorder.emit("capture_gap", sid, direction=direction, packet_number=index,
                              expected_offset=len(assembled), actual_offset=offset)
                # The remaining wire data is already retained in capture_segment records.
                break
            overlap = min(len(data), len(assembled) - offset)
            if assembled[offset:offset + overlap] != data[:overlap]:
                warnings.append({"session_id": sid, "direction": direction,
                                 "reason": "conflicting_retransmission", "packet": index})
                recorder.emit("capture_conflict", sid, direction=direction, packet_number=index)
                break
            fresh = data[overlap:]
            if not fresh:
                recorder.emit("capture_duplicate", sid, direction=direction, packet_number=index)
                continue
            stream_offset = len(assembled)
            assembled.extend(fresh)
            recorder.emit("socket_bytes", sid, channel="raw", direction=direction,
                          offset=stream_offset, data_hex=fresh.hex(),
                          packet_number=index, capture_at=at, source="offline_tcp_reassembly")
            if blocked:
                continue
            try:
                for frame in decoder.feed(fresh):
                    recorder.emit("frame_tx" if direction == "tx" else "frame_rx", sid,
                                  mid=f"{frame.mid:04X}", token=frame.token.hex(),
                                  declared=frame.declared, body_length=len(frame.body),
                                  body_hex=frame.body.hex(), stream_offset=frame.offset,
                                  capture_at=at, packet_number=index,
                                  length_exception=(len(frame.raw) != frame.declared + 4))
            except ProtocolError as exc:
                blocked = True
                warnings.append({"session_id": sid, "direction": direction,
                                 "reason": "protocol_error", "packet": index})
                recorder.emit("protocol_error", sid, direction=direction, error=str(exc),
                              residual_hex=bytes(decoder.buffer).hex())
        if decoder.buffer:
            recorder.emit("capture_residual", sid, direction=direction,
                          residual_hex=bytes(decoder.buffer).hex())
            warnings.append({"session_id": sid, "direction": direction, "reason": "partial_frame"})
    return warnings


def import_capture(source, output, *, tshark=None, server_port=20000, legacy_time=True):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("capture file does not exist")
    program = tshark or shutil.which("tshark")
    if not program:
        raise ValueError("TShark is required for capture import; use --tshark PATH")
    args = [str(program), "-n", "-r", str(source), "-Y",
            f"tcp.port == {server_port} and tcp.len > 0", "-T", "fields"]
    for name in FIELDS:
        args += ["-e", name]
    with tempfile.TemporaryFile() as extracted:
        process = subprocess.run(args, stdout=extracted, stderr=subprocess.PIPE, timeout=120)
        size = extracted.tell()
        if size > 128 * 1024 * 1024:
            raise ValueError("capture extraction exceeds 128 MiB; split the capture before import")
        extracted.seek(0)
        extraction = extracted.read()
    if process.returncode:
        raise ValueError("TShark extraction failed: " + process.stderr.decode("utf-8", errors="replace")[:1000])
    rows = []
    for line in extraction.decode("ascii").splitlines():
        fields = line.split("\t")
        if len(fields) != len(FIELDS) or not all(fields):
            raise ValueError("unsupported or missing IPv4/TCP capture fields")
        rows.append(fields)
    if not rows:
        raise ValueError("capture contains no matching TCP payload")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    recorder = Recorder(output, {"mode": "offline_capture", "source_name": source.name,
                                "source_sha256": digest.hexdigest(), "server_port": server_port,
                                "legacy_time_reply": legacy_time}, max_bytes=1024 * 1024 * 1024)
    reason = "normal"
    try:
        warnings = ingest_rows(recorder, rows, server_port=server_port, legacy_time=legacy_time)
        recorder.manifest["capture_warnings"] = warnings
        recorder.manifest["capture_complete"] = not warnings
    except BaseException:
        reason = "import_failure"
        raise
    finally:
        recorder.close(reason)
    return {"run_dir": str(output), "packets": len(rows), "warnings": warnings}
