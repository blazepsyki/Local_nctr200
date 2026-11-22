"""Point the NCTR-200 controller at a local server (standard library only).

This file also runs on its own, without Home Assistant:

    python provision.py --ssid <Wi-Fi name> --server-ip <Home Assistant IP>

The manufacturer app sends one UDP datagram to the controller's setup access point
(SSID ``krb_set``, 192.168.200.1:20000), commands separated by CR:

    WizFi250AirCmd:AT+WSET=0,<SSID>
    AT+WSEC=0,,<password>
    AT+WNET=1
    AT+SCON=S,TCN,<server IP>,20000,,1
    AT+ABCD=S,TCN,<server URL>,20000,,1
    AT+FSOCK=6,15
    AT+MPROF=S

The controller does not reply; it saves the settings, restarts and the setup access
point disappears within about a second. After that it always looks up the ABCD host
through DNS and inserts "4" after the first three characters of the host
(igs.krb.co.kr -> igs4.krb.co.kr). A wildcard DNS name with a throw-away first label
survives that: http://nas.192.168.1.8.nip.io is looked up as nas4.192.168.1.8.nip.io,
which resolves to 192.168.1.8. The controller needs Internet access for that lookup.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import socket
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

AP_ADDRESS = ("192.168.200.1", 20000)
SETUP_NETWORK = ipaddress.ip_network("192.168.200.0/24")
SETUP_SSID = "krb_set"
DEFAULT_PORT = 20000
REPLY_TIMEOUT = 5.0
PREFIX = "WizFi250AirCmd:"
SEPARATOR = "\r"
_FORBIDDEN = set("\r\n,\x00")


def check_text(label: str, value: str, *, allow_empty: bool = False) -> str:
    """Values go into comma-separated AT commands: printable ASCII only."""
    if not value and not allow_empty:
        raise ValueError(f"{label} is empty")
    if not value.isascii() or not value.isprintable() or _FORBIDDEN & set(value):
        raise ValueError(f"{label} must be printable ASCII without commas")
    return value


def server_url(server_ip: str) -> str:
    """The ABCD value the controller turns into a name resolving to server_ip."""
    ipaddress.IPv4Address(server_ip)
    return f"http://nas.{server_ip}.nip.io"


def queried_name(url: str) -> str:
    """The DNS name the controller will look up for an ABCD value."""
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host[:3] + "4" + host[3:]


@dataclass(frozen=True)
class Plan:
    ssid: str
    password: str
    server_ip: str
    port: int = DEFAULT_PORT
    url: str | None = None

    def __post_init__(self) -> None:
        check_text("SSID", self.ssid)
        check_text("Wi-Fi password", self.password, allow_empty=True)
        ipaddress.IPv4Address(self.server_ip)
        if not 1 <= self.port <= 65535:
            raise ValueError("invalid port")
        check_text("server URL", self.target_url)

    @property
    def target_url(self) -> str:
        return self.url or server_url(self.server_ip)

    def lines(self, *, mask: bool = False) -> list[str]:
        password = "*" * len(self.password) if mask else self.password
        return [
            f"AT+WSET=0,{self.ssid}",
            f"AT+WSEC=0,,{password}",
            "AT+WNET=1",
            f"AT+SCON=S,TCN,{self.server_ip},{self.port},,1",
            f"AT+ABCD=S,TCN,{self.target_url},{self.port},,1",
            "AT+FSOCK=6,15",
            "AT+MPROF=S",
        ]

    def payload(self) -> bytes:
        return (PREFIX + SEPARATOR.join(self.lines()) + SEPARATOR).encode("ascii")


@dataclass
class Result:
    replies: list[str] = field(default_factory=list)
    reset_sent: bool = False

    @property
    def rejected(self) -> bool:
        return any("ERROR" in reply.upper() for reply in self.replies)


def setup_source_ip(target: tuple[str, int] | None = None) -> str | None:
    """Local address used to reach the setup access point, if this host is on it."""
    target = target or AP_ADDRESS
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(target)
        except OSError:
            return None
        source = str(sock.getsockname()[0])
    return source if ipaddress.ip_address(source) in SETUP_NETWORK else None


def _exchange(payload: bytes, target: tuple[str, int], timeout: float) -> list[str]:
    replies: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(payload, target)
        while True:
            try:
                data, _ = sock.recvfrom(2048)
            except TimeoutError:
                return replies
            except OSError:
                # Windows reports an ICMP port unreachable as a reset on the next call.
                return replies
            replies.append(data.decode("ascii", "replace").strip())


def send(
    plan: Plan, *, target: tuple[str, int] | None = None, timeout: float | None = None
) -> Result:
    """Send the settings once. Restart only when the controller answered without error.

    The controller normally does not answer and restarts by itself; the app then sends
    nothing more, and neither does this.
    """
    target = target or AP_ADDRESS
    timeout = REPLY_TIMEOUT if timeout is None else timeout
    result = Result(_exchange(plan.payload(), target, timeout))
    if result.replies and not result.rejected:
        _exchange((PREFIX + "AT+MRESET" + SEPARATOR).encode("ascii"), target, timeout)
        result.reset_sent = True
    return result


# ----- command line -----


def _read_password(path: str | None, ask_secret: Callable[[str], str]) -> str:
    if path:
        with open(path, encoding="utf-8") as stream:
            return stream.read().rstrip("\r\n")
    first = ask_secret("Wi-Fi 암호(화면에 표시되지 않음): ")
    if ask_secret("한 번 더 입력: ") != first:
        raise ValueError("두 암호가 다릅니다")
    return first


def main(
    argv: Sequence[str] | None = None,
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "NCTR-200 조절기가 로컬 서버에 접속하도록 Wi-Fi를 설정합니다. 먼저 조절기를 설정 "
            f"모드로 두고 이 컴퓨터를 조절기 Wi-Fi({SETUP_SSID})에 연결하십시오."
        )
    )
    parser.add_argument("--ssid", required=True, help="조절기가 접속할 2.4GHz Wi-Fi 이름")
    parser.add_argument("--server-ip", required=True, help="Home Assistant 기기의 IP")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="서버 포트(기본 20000)")
    parser.add_argument("--url", help="서버 주소를 직접 지정(기본 http://nas.<server-ip>.nip.io)")
    parser.add_argument("--password-file", help="Wi-Fi 암호를 이 파일에서 읽음")
    parser.add_argument("--timeout", type=float, default=REPLY_TIMEOUT)
    args = parser.parse_args(argv)

    try:
        password = _read_password(args.password_file, ask_secret)
        plan = Plan(args.ssid, password, args.server_ip, args.port, args.url)
    except (OSError, ValueError) as err:
        print(f"오류: {err}", file=sys.stderr)
        return 2

    source = setup_source_ip()
    if source is None:
        print(f"오류: 이 컴퓨터가 {SETUP_SSID}에 연결되어 있지 않습니다", file=sys.stderr)
        return 2
    print(f"보낼 내용 ({source} → {AP_ADDRESS[0]}:{AP_ADDRESS[1]}, 암호는 *):")
    for line in plan.lines(mask=True):
        print(f"  {line}")
    print(f"조절기는 {queried_name(plan.target_url)}을(를) DNS로 조회합니다.")
    if ask("한 번 보내려면 SEND 입력: ").strip() != "SEND":
        print("보내지 않았습니다")
        return 1

    result = send(plan, timeout=args.timeout)
    for reply in result.replies:
        print(f"응답: {reply}")
    if result.rejected:
        print("조절기가 설정을 거부했습니다.", file=sys.stderr)
        return 1
    if result.reset_sent:
        print("응답이 와서 재시작 명령(AT+MRESET)을 보냈습니다.")
    print(
        f"보냈습니다. 몇 초 안에 {SETUP_SSID}가 사라지고, 조절기가 Wi-Fi에 붙은 뒤 "
        "서버에 접속합니다."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
