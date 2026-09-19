"""The live runner: reading the tunnel's address out of cloudflared's log.

A farmer's map and upload links are that address. Taking the wrong one out of the
log put Cloudflare's own "Method Not Allowed" page in a tester's hands.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

LIVE = Path(__file__).resolve().parents[1] / "examples" / "live_demo.py"
spec = importlib.util.spec_from_file_location("live_demo", LIVE)
live_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_demo)

#: cloudflared's own start-up chatter, as it comes out, newest quick tunnel last.
LOG = """\
2026-09-19T21:25:02Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment and try it out. However, be aware that these account-less Tunnels have no uptime guarantee...
2026-09-19T21:25:02Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-09-19T21:25:03Z INF POST https://api.trycloudflare.com/tunnel
2026-09-19T21:25:05Z INF +--------------------------------------------------------------------------------------------+
2026-09-19T21:25:05Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-09-19T21:25:05Z INF |  https://sie-reasons-prominent-valued.trycloudflare.com                                     |
2026-09-19T21:25:05Z INF +--------------------------------------------------------------------------------------------+
"""


def test_the_address_is_the_tunnels_own_not_cloudflares_api() -> None:
    found = [live_demo.QUICK_TUNNEL.search(line) for line in LOG.splitlines()]
    addresses = [match.group(0) for match in found if match]
    assert addresses == ["https://sie-reasons-prominent-valued.trycloudflare.com"]


@pytest.mark.parametrize("line, address", [
    ("|  https://falls-mixer-wines-commissioner.trycloudflare.com  |",
     "https://falls-mixer-wines-commissioner.trycloudflare.com"),
    ("INF POST https://api.trycloudflare.com/tunnel", None),
    ("INF Requesting new quick Tunnel on trycloudflare.com...", None),
    ("registered at https://update.argotunnel.com", None),
])
def test_only_a_quick_tunnels_own_name_counts(line, address) -> None:
    match = live_demo.QUICK_TUNNEL.search(line)
    assert (match.group(0) if match else None) == address


def test_an_address_that_answers_as_a_stranger_is_a_warning(monkeypatch, capsys) -> None:
    monkeypatch.setattr(live_demo, "tunnel_answers", lambda address, **kw: "no")
    live_demo.check_tunnel("https://api.trycloudflare.com")
    printed = capsys.readouterr().out
    assert "not as this server" in printed and "start it again" in printed


def test_a_name_this_computer_cannot_look_up_is_not_alarming(monkeypatch, capsys) -> None:
    """This PC often remembers a brand-new tunnel name as missing; phones do not."""
    monkeypatch.setattr(live_demo, "tunnel_answers", lambda address, **kw: "dns")
    live_demo.check_tunnel("https://witch-clicks-tiffany-structural.trycloudflare.com")
    printed = capsys.readouterr().out
    assert "cannot look up" in printed and "phones normally open it" in printed
    assert "WARNING" not in printed


def test_an_address_that_reaches_us_is_confirmed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(live_demo, "tunnel_answers", lambda address, **kw: "ok")
    live_demo.check_tunnel("https://sie-reasons.trycloudflare.com")
    assert "opens this server" in capsys.readouterr().out


def test_the_check_knows_our_server_by_its_own_front_page(monkeypatch) -> None:
    import urllib.request

    class Answer:
        def read(self, n=None):
            return b"Dos Ojos SMS is running."

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Answer())
    assert live_demo.tunnel_answers("https://x.trycloudflare.com", timeout=1) == "ok"


def test_a_name_that_does_not_resolve_says_dns(monkeypatch) -> None:
    import socket
    import urllib.error
    import urllib.request

    def refuse(*args, **kwargs):
        raise urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(live_demo.time, "sleep", lambda seconds: None)
    assert live_demo.tunnel_answers("https://new.trycloudflare.com", timeout=0.1) == "dns"


def test_someone_elses_page_is_not_us(monkeypatch) -> None:
    import urllib.request

    class Answer:
        def read(self, n=None):
            return b"<html>Cloudflare</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Answer())
    monkeypatch.setattr(live_demo.time, "sleep", lambda seconds: None)
    assert live_demo.tunnel_answers("https://api.trycloudflare.com", timeout=0.1) == "no"
