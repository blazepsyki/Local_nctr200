"""Self-provisioning of the NCTR-200 Wi-Fi module without the manufacturer app.

The manufacturer app (Setup_Step06.java) sends one UDP datagram to the controller's
setup AP (SSID krb_set, 192.168.200.1:20000), commands separated by CR (bytes confirmed
in a PCAPdroid capture of the app, 2026-11-22 03:48:57, from an ephemeral source port):

    WizFi250AirCmd:AT+WSET=0,<SSID>
    AT+WSEC=0,,<password>
    AT+WNET=1
    AT+SCON=S,TCN,121.141.70.84,20000,,1
    AT+ABCD=S,TCN,http://igs4.krb.co.kr,20000,,1
    AT+FSOCK=6,15
    AT+MPROF=S

The controller sent no reply and the app sent no AT+MRESET; the controller restarts by
itself and the setup AP disappears within about a second. An LF-separated datagram is
ignored (the AP stays up). The ADB log showed line breaks only because the app logger
rewrote them. The controller later connected to igs4's DNS
address, not to the SCON IP, so ABCD (domain) appears to be the path in use; the boot
HTTP answer {"uri":"D"} from igis.krb.co.kr is consistent with that. This module builds
the same datagram with local targets. Nothing here is sent without an explicit call.

After provisioning the controller always resolves the ABCD host through DNS, even when it
is an IP literal, and it inserts "4" after the first three characters of the host
(192.168.1.8 -> 1924.168.1.8, 192.168.1.8.nip.io -> 1924.168.1.8.nip.io; the stock
value igs4.krb.co.kr fits the same "igs" + "4" pattern). A wildcard-DNS name with a
throw-away three-letter first label survives that rewrite:
http://nas.192.168.1.8.nip.io -> nas4.192.168.1.8.nip.io -> 192.168.1.8
(field-20261122-30: connected, identified and ready at 04:16, no DNS redirect).
"""


def default_abcd(server_ip):
    """ABCD value that the controller rewrites into a name resolving to server_ip."""
    ipaddress.IPv4Address(server_ip)
    return f"http://nas.{server_ip}.nip.io"


def resolved_name(abcd_value):
    """The DNS name the controller is expected to query for an ABCD value."""
    host = abcd_value.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host[:3] + "4" + host[3:]

from dataclasses import dataclass
import ipaddress
import json
import os
import socket
import time

AP_ADDRESS = ("192.168.200.1", 20000)
PREFIX = "WizFi250AirCmd:"
FORBIDDEN = set("\r\n,\x00")
LINE_ENDINGS = {"lf": "\n", "cr": "\r", "crlf": "\r\n"}


def _check(label, value, *, allow_empty=False):
    if not value and not allow_empty:
        raise ValueError(f"{label} is empty")
    if not value.isascii() or not value.isprintable() or FORBIDDEN & set(value):
        raise ValueError(f"{label} must be printable ASCII without CR, LF, commas or NUL")
    return value


@dataclass(frozen=True)
class ProvisionPlan:
    ssid: str
    password: str
    scon_host: str
    abcd_value: str
    port: int = 20000
    fsock: str = "6,15"
    line_ending: str = "cr"

    def validate(self):
        _check("SSID", self.ssid)
        _check("Wi-Fi password", self.password, allow_empty=True)
        ipaddress.IPv4Address(self.scon_host)
        _check("ABCD target", self.abcd_value)
        if not 1 <= self.port <= 65535:
            raise ValueError("invalid server port")
        if self.line_ending not in LINE_ENDINGS:
            raise ValueError("line ending must be lf, cr or crlf")
        return self

    def lines(self, *, mask=False):
        password = "*" * len(self.password) if mask else self.password
        return [
            f"AT+WSET=0,{self.ssid}",
            f"AT+WSEC=0,,{password}",
            "AT+WNET=1",
            f"AT+SCON=S,TCN,{self.scon_host},{self.port},,1",
            f"AT+ABCD=S,TCN,{self.abcd_value},{self.port},,1",
            f"AT+FSOCK={self.fsock}",
            "AT+MPROF=S",
        ]

    def payload(self):
        self.validate()
        sep = LINE_ENDINGS[self.line_ending]
        return (PREFIX + sep.join(self.lines()) + sep).encode("ascii")

    def masked_text(self):
        mark = {"lf": "\\n", "cr": "\\r", "crlf": "\\r\\n"}[self.line_ending]
        return PREFIX + (mark + "\n").join(self.lines(mask=True)) + mark


def single_command(command, line_ending="cr"):
    """One Air Command such as AT+MRESET or AT+MINFO."""
    _check("AT command", command.replace(",", ""))
    if not command.startswith("AT+"):
        raise ValueError("Air Command must start with AT+")
    return (PREFIX + command + LINE_ENDINGS[line_ending]).encode("ascii")


def exchange(payload, *, target=AP_ADDRESS, bind=None, source_port=0, timeout=5.0,
             clock=time.monotonic):
    """Send one datagram and collect every reply until the timeout."""
    replies = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        if bind or source_port:
            sock.bind((bind or "", source_port))
        sock.sendto(payload, target)
        deadline = clock() + timeout
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, peer = sock.recvfrom(2048)
            except socket.timeout:
                break
            replies.append({"from": list(peer), "hex": data.hex(),
                            "text": data.decode("ascii", "replace")})
    return replies


def write_record(directory, record):
    """Private JSON record of one provisioning step (password never included)."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    name = time.strftime("provision-%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000000:06d}.json"
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    return path
