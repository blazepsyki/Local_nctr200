"""Synchronous bounded run storage; raw data stays private."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import threading
import time

KST = timezone(timedelta(hours=9), "KST")


def stamp():
    return datetime.now(KST).isoformat(timespec="milliseconds")


def write_private(path, data):
    # os.open mode is honored on POSIX; Windows uses the parent directory ACL.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def source_hashes():
    base = Path(__file__).resolve().parent.parent
    files = sorted((base / "nctr_lab").glob("*.py")) + [base / "nctr_lab_cli.py"]
    return {p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files if p.is_file()}


class RecordingError(OSError):
    pass


class Recorder:
    def __init__(self, directory, config, *, rotate_bytes=8 * 1024 * 1024,
                 max_bytes=1024 * 1024 * 1024, reserve_bytes=64 * 1024 * 1024,
                 clock=time.monotonic):
        if rotate_bytes < 1024 or max_bytes < rotate_bytes or reserve_bytes < 0:
            raise ValueError("invalid storage limits")
        self.path = Path(directory)
        self.path.mkdir(parents=True, mode=0o700, exist_ok=False)
        (self.path / "raw").mkdir(mode=0o700)
        self.lock = threading.RLock()
        self.clock = clock
        self.sequence = 0
        self.total_bytes = 0
        self.rotate_bytes = rotate_bytes
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes
        self.failed = False
        self.closed = False
        self.loss_count = 0
        self.files = {}
        self.streams = {}
        self.indexes = {}
        self.manifest = {
            "schema_version": 1, "run_id": self.path.name, "started_at": stamp(),
            "python": platform.python_version(), "platform": platform.platform(),
            "source_hashes": source_hashes(), "config": config,
            "storage": {"mode": "synchronous", "rotate_bytes": rotate_bytes,
                        "max_bytes": max_bytes, "reserve_bytes": reserve_bytes},
            "status": "running", "private_raw": True,
        }
        self._manifest()

    def _manifest(self):
        data = json.dumps(self.manifest, ensure_ascii=False, indent=2).encode("utf-8")
        temporary = self.path / "manifest.tmp"
        # The temporary name is owned exclusively by this run.
        if temporary.exists():
            temporary.unlink()
        write_private(temporary, data)
        os.replace(temporary, self.path / "manifest.json")

    def _stream(self, channel, size):
        if channel not in self.streams or self.files[channel]["bytes"] + size > self.rotate_bytes:
            if channel in self.streams:
                self.streams[channel].close()
            index = self.indexes.get(channel, 0) + 1
            self.indexes[channel] = index
            relative = (f"raw/traffic.{index:06}.jsonl" if channel == "raw"
                        else f"{channel}.{index:06}.jsonl")
            fd = os.open(self.path / relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.streams[channel] = os.fdopen(fd, "wb", buffering=0)
            self.files[channel] = {"path": relative, "bytes": 0}
        return self.streams[channel]

    def emit(self, event_type, session_id=None, *, channel="events", **fields):
        with self.lock:
            if self.failed or self.closed:
                raise RecordingError("run storage is unavailable")
            self.sequence += 1
            event = {**fields, "schema_version": 1, "run_id": self.path.name,
                     "session_id": session_id, "event_seq": self.sequence,
                     "at": stamp(), "monotonic": self.clock(), "event_type": event_type}
            data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                if self.total_bytes + len(data) > self.max_bytes:
                    raise RecordingError("run storage budget exceeded")
                if shutil.disk_usage(self.path).free - len(data) < self.reserve_bytes:
                    raise RecordingError("disk reserve reached")
                stream = self._stream(channel, len(data))
                if stream.write(data) != len(data):
                    raise RecordingError("short log write")
                # Persist before permitting an associated physical command.
                os.fsync(stream.fileno())
                self.files[channel]["bytes"] += len(data)
                self.total_bytes += len(data)
            except OSError as exc:
                self.failed = True
                self.loss_count += 1
                raise RecordingError(str(exc)) from exc
            return event

    def close(self, reason="normal"):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for stream in self.streams.values():
                stream.close()
            hashes = {}
            for pattern in ("*.jsonl", "raw/*.jsonl"):
                for path in sorted(self.path.glob(pattern)):
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    hashes[path.relative_to(self.path).as_posix()] = digest.hexdigest()
            self.manifest.update(status="storage_failed" if self.failed else "closed",
                                 ended_at=stamp(), reason=reason, loss_count=self.loss_count,
                                 total_bytes=self.total_bytes, last_event_seq=self.sequence,
                                 file_hashes=hashes)
            self._manifest()


def load_events(directory):
    # Preserve event order across streams and rotated files.
    paths = list(Path(directory).glob("events.*.jsonl"))
    paths += list(Path(directory).glob("commands.*.jsonl"))
    events = []
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or not isinstance(event.get("event_seq"), int):
                        raise ValueError("invalid event schema")
                    events.append(event)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"incomplete JSONL: {path.name}:{line_number}") from exc
    return sorted(events, key=lambda event: event["event_seq"])
