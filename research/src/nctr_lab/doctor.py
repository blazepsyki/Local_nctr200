"""Read-only local checks and explicit DNS queries; never configure DNS."""

import ipaddress
import secrets
import shutil
import socket
import struct
import sys
from pathlib import Path


def _skip_name(data, offset):
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS name")
        length = data[offset]
        offset += 1
        if length == 0:
            return offset
        if length & 0xC0 == 0xC0:
            if offset >= len(data):
                raise ValueError("truncated DNS pointer")
            return offset + 1
        if length & 0xC0 or length > 63 or offset + length > len(data):
            raise ValueError("invalid DNS label")
        offset += length


def query_a(server, hostname, *, port=53, timeout=2):
    ipaddress.IPv4Address(server)
    query_id = secrets.randbits(16)
    labels = hostname.encode("idna").split(b".")
    if any(not label or len(label) > 63 for label in labels):
        raise ValueError("invalid hostname")
    name = b"".join(bytes((len(label),)) + label for label in labels) + b"\0"
    question = name + struct.pack("!HH", 1, 1)
    request = struct.pack("!6H", query_id, 0x0100, 1, 0, 0, 0) + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        # A connected UDP socket also rejects responses from other endpoints.
        sock.connect((server, port))
        sock.send(request)
        data = sock.recv(65535)
    if len(data) < 12:
        raise ValueError("truncated DNS response")
    ident, flags, qd, an, ns, ar = struct.unpack("!6H", data[:12])
    if ident != query_id or not flags & 0x8000 or flags & 0x020F or qd != 1:
        raise ValueError("invalid, truncated or unsuccessful DNS response")
    question_end = _skip_name(data, 12) + 4
    if data[12:question_end].lower() != question.lower():
        raise ValueError("DNS response question mismatch")
    offset = question_end
    addresses = []
    for index in range(an + ns + ar):
        offset = _skip_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("truncated DNS record")
        kind, klass, ttl, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        if offset + length > len(data):
            raise ValueError("truncated DNS body")
        if index < an and kind == 1 and klass == 1 and length == 4:
            addresses.append(socket.inet_ntoa(data[offset:offset + 4]))
        offset += length
    if not addresses:
        raise ValueError("DNS answer contains no IPv4 address")
    return sorted(set(addresses))


def check_environment(bind, port, output_root, dns_server=None, expected_ip=None):
    ipaddress.IPv4Address(bind)
    if not 0 <= port <= 65535:
        raise ValueError("invalid bind port")
    checks = {"python_supported": sys.version_info >= (3, 10)}
    root = Path(output_root).resolve()
    probe = root
    while not probe.exists():
        probe = probe.parent
    checks["disk_free_bytes"] = shutil.disk_usage(probe).free
    checks["output_root"] = str(root)
    checks["tools"] = {name: shutil.which(name) for name in ("tcpdump", "ss", "nslookup", "ssh")}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((bind, port))
        checks["bind_available"] = True
    except OSError as exc:
        checks.update(bind_available=False, bind_error=str(exc))
    if dns_server:
        if not expected_ip:
            raise ValueError("DNS check requires the expected local IPv4 address")
        ipaddress.IPv4Address(expected_ip)
        answers = {}
        for hostname in ("igs4.krb.co.kr", "igis.krb.co.kr"):
            try:
                answers[hostname] = query_a(dns_server, hostname)
            except (OSError, ValueError) as exc:
                answers[hostname] = {"error": str(exc)}
        checks["dns_answers"] = answers
        checks["dns_override_matches"] = answers["igs4.krb.co.kr"] == [expected_ip]
        http_answer = answers["igis.krb.co.kr"]
        checks["http_hostname_not_overridden"] = (isinstance(http_answer, list)
                                                   and expected_ip not in http_answer)
        checks["device_dns_path_proven"] = False
    checks["ok"] = (checks["python_supported"] and checks["bind_available"]
                    and checks["disk_free_bytes"] >= 64 * 1024 * 1024
                    and (not dns_server or (checks["dns_override_matches"]
                                           and checks["http_hostname_not_overridden"])))
    return checks
