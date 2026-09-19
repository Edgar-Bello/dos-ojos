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


def test_an_address_that_is_not_our_server_is_called_out(monkeypatch, capsys) -> None:
    monkeypatch.setattr(live_demo, "tunnel_reaches_us", lambda address, **kw: False)
    live_demo.check_tunnel("https://api.trycloudflare.com")
    printed = capsys.readouterr().out
    assert "did not answer as this server" in printed and "start it again" in printed


def test_an_address_that_reaches_us_is_confirmed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(live_demo, "tunnel_reaches_us", lambda address, **kw: True)
    live_demo.check_tunnel("https://sie-reasons.trycloudflare.com")
    assert "opens this server" in capsys.readouterr().out


def test_the_check_knows_our_server_by_its_own_header(monkeypatch) -> None:
    import urllib.request

    class Answer:
        headers = {"Server": "DosOjosSMS/0.1 Python/3.11"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Answer())
    assert live_demo.tunnel_reaches_us("https://x.trycloudflare.com", timeout=1)


def test_a_stranger_answering_is_not_us(monkeypatch) -> None:
    import urllib.request

    class Answer:
        headers = {"Server": "cloudflare"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Answer())
    monkeypatch.setattr(live_demo.time, "sleep", lambda seconds: None)
    assert not live_demo.tunnel_reaches_us("https://api.trycloudflare.com", timeout=0.1)
