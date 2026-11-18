"""SMTP connects over IPv4 first: a dead IPv6 route must not stop sending.

On some networks Gmail's IPv6 address times out while IPv4 connects at
once; aiosmtplib alone tries IPv6 first and every send failed."""
import socket
from types import SimpleNamespace

import pytest

from harvey.integrations import smtp_mail


class FakeSocket:
    attempts = []

    def __init__(self, family, kind, proto):
        self.family = family

    def settimeout(self, t):
        pass

    def setblocking(self, flag):
        self.blocking = flag

    def connect(self, addr):
        FakeSocket.attempts.append(self.family)
        if self.family == socket.AF_INET6:
            raise TimeoutError("dead IPv6 route")

    def close(self):
        pass


@pytest.fixture
def dual_stack(monkeypatch):
    FakeSocket.attempts = []
    infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 587, 0, 0)),
             (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 587))]
    monkeypatch.setattr(smtp_mail.socket, "getaddrinfo", lambda *a, **k: list(infos))
    monkeypatch.setattr(smtp_mail.socket, "socket", FakeSocket)


def test_ipv4_is_tried_before_ipv6(dual_stack):
    sock = smtp_mail._connect_ipv4_first("smtp.mail.example", 587)
    assert sock.family == socket.AF_INET
    assert FakeSocket.attempts == [socket.AF_INET]  # IPv6 never needed
    assert sock.blocking is False  # handed to asyncio


def test_falls_back_to_ipv6_when_ipv4_fails(monkeypatch):
    FakeSocket.attempts = []
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 587)),
             (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 587, 0, 0))]

    class V4Down(FakeSocket):
        def connect(self, addr):
            FakeSocket.attempts.append(self.family)
            if self.family == socket.AF_INET:
                raise OSError("unreachable")

    monkeypatch.setattr(smtp_mail.socket, "getaddrinfo", lambda *a, **k: list(infos))
    monkeypatch.setattr(smtp_mail.socket, "socket", V4Down)
    assert smtp_mail._connect_ipv4_first("smtp.mail.example", 587).family == socket.AF_INET6
    assert FakeSocket.attempts == [socket.AF_INET, socket.AF_INET6]


@pytest.mark.asyncio
async def test_send_hands_the_socket_and_real_hostname_to_aiosmtplib(monkeypatch):
    # (socket.socket itself can't be faked here: asyncio needs the real one.)
    calls, connected = [], []

    async def fake_send(msg, **kwargs):
        calls.append(kwargs)

    def fake_connect(host, port, timeout):
        connected.append((host, port))
        return FakeSocket(socket.AF_INET, socket.SOCK_STREAM, 6)

    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", fake_send)
    monkeypatch.setattr(smtp_mail, "_connect_ipv4_first", fake_connect)
    cfg = SimpleNamespace(
        persona=SimpleNamespace(name="Sam", email="sam@sender.example", company="Acme"),
        channels=SimpleNamespace(email=SimpleNamespace(signature=None)))
    env = SimpleNamespace(smtp_host="smtp.mail.example", smtp_port=587,
                          smtp_username="sam@sender.example", smtp_password="x")
    result = await smtp_mail.SmtpImapProvider(cfg, env).send_email("jane@acme.example", "hi", "Hi Jane.")

    assert result.ok, result.error
    assert connected == [("smtp.mail.example", 587)]
    kw = calls[0]
    assert kw["hostname"] == "smtp.mail.example"  # TLS checks the real name
    assert kw["sock"].family == socket.AF_INET
    assert "port" not in kw  # aiosmtplib refuses a port alongside a socket
    assert kw["start_tls"] is True
