"""Provisioning datagram and the standalone tool."""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from custom_components.nctr200 import provision

APP_DATAGRAM = (
    b"WizFi250AirCmd:AT+WSET=0,home\rAT+WSEC=0,,secret\rAT+WNET=1\r"
    b"AT+SCON=S,TCN,192.168.1.8,20000,,1\r"
    b"AT+ABCD=S,TCN,http://nas.192.168.1.8.nip.io,20000,,1\r"
    b"AT+FSOCK=6,15\rAT+MPROF=S\r"
)
LOOPBACK = ipaddress.ip_network("127.0.0.0/8")


def test_payload_matches_the_app_format() -> None:
    plan = provision.Plan("home", "secret", "192.168.1.8")
    assert plan.payload() == APP_DATAGRAM
    assert "AT+WSEC=0,,******" in plan.lines(mask=True)
    assert provision.queried_name(plan.target_url) == "nas4.192.168.1.8.nip.io"
    assert provision.queried_name("http://igs.krb.co.kr") == "igs4.krb.co.kr"


@pytest.mark.parametrize(
    ("ssid", "password", "server"),
    [
        ("", "x", "192.168.1.8"),
        ("a,b", "x", "192.168.1.8"),
        ("home", "pä", "192.168.1.8"),
        ("home", "x", "nas.local"),
    ],
)
def test_plan_rejects_bad_values(ssid: str, password: str, server: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        provision.Plan(ssid, password, server)


class Ap:
    """A UDP endpoint standing in for the controller's setup access point."""

    def __init__(self, reply: bytes | None) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.address = self.sock.getsockname()
        self.reply = reply
        self.received: list[bytes] = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                data, peer = self.sock.recvfrom(4096)
            except OSError:
                return
            self.received.append(data)
            if self.reply is not None:
                self.sock.sendto(self.reply, peer)


@pytest.fixture
def make_ap(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Ap]]:
    made: list[Ap] = []

    def make(reply: bytes | None = None) -> Ap:
        ap = Ap(reply)
        made.append(ap)
        monkeypatch.setattr(provision, "AP_ADDRESS", ap.address)
        return ap

    monkeypatch.setattr(provision, "REPLY_TIMEOUT", 0.3)
    yield make
    for ap in made:
        ap.sock.close()


def test_send_without_reply_sends_once(make_ap: Callable[..., Ap]) -> None:
    ap = make_ap()
    result = provision.send(provision.Plan("home", "secret", "192.168.1.8"))
    assert (result.replies, result.reset_sent, result.rejected) == ([], False, False)
    assert ap.received == [APP_DATAGRAM]


def test_send_restarts_only_after_a_good_reply(make_ap: Callable[..., Ap]) -> None:
    ap = make_ap(b"[OK]")
    result = provision.send(provision.Plan("home", "secret", "192.168.1.8"))
    assert result.reset_sent
    assert ap.received[1] == b"WizFi250AirCmd:AT+MRESET\r"

    ap = make_ap(b"[ERROR]")
    result = provision.send(provision.Plan("home", "secret", "192.168.1.8"))
    assert result.rejected
    assert not result.reset_sent
    assert len(ap.received) == 1


def test_setup_source_ip_requires_the_setup_network(monkeypatch: pytest.MonkeyPatch) -> None:
    assert provision.setup_source_ip(("127.0.0.1", 9)) is None
    monkeypatch.setattr(provision, "SETUP_NETWORK", LOOPBACK)
    assert provision.setup_source_ip(("127.0.0.1", 9)) == "127.0.0.1"


def test_cli(
    make_ap: Callable[..., Ap],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ap = make_ap()
    monkeypatch.setattr(provision, "SETUP_NETWORK", LOOPBACK)
    secret = tmp_path / "pw"
    secret.write_text("secret\n", encoding="utf-8")
    argv = ["--ssid", "home", "--server-ip", "192.168.1.8", "--password-file", str(secret)]

    assert provision.main(argv, ask=lambda _: "no") == 1
    assert ap.received == []
    assert provision.main(argv, ask=lambda _: "SEND") == 0
    assert ap.received == [APP_DATAGRAM]
    out = capsys.readouterr().out
    assert "secret" not in out
    assert "nas4.192.168.1.8.nip.io" in out

    answers = iter(["one", "two"])
    assert provision.main(argv[:4], ask=lambda _: "SEND", ask_secret=lambda _: next(answers)) == 2
