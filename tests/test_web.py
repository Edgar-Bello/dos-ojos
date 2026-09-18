"""The web side over real HTTP: webhook, map, upload and simulator."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest
from conftest import NOW, SQUARE, FakeWater, resolver_to

from dosojos_sms import store, text, twilio
from dosojos_sms.config import Settings
from dosojos_sms.web import App, make_server

TOKEN = "test-token"


def serve(app: App):
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def call(url: str, *, method: str = "GET", data: bytes | None = None,
         headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def post_json(url: str, payload: dict) -> tuple[int, dict]:
    status, body = call(url, method="POST", data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"})
    return status, json.loads(body)


@pytest.fixture
def app(tmp_path) -> App:
    settings = Settings.load(tmp_path / "farm_data", env={
        "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": TOKEN, "TWILIO_FROM": "+19565550100",
        "DOSOJOS_PUBLIC_URL": "https://farm.example.com"})
    settings.ensure_dirs()
    application = App(settings, sim=True, resolve=resolver_to("none"), water=FakeWater())
    application.now = NOW
    return application


@pytest.fixture
def base(app: App):
    server, url = serve(app)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def sent(monkeypatch) -> list:
    calls = []
    monkeypatch.setattr(twilio, "send", lambda settings, to, body, media=None: calls.append((to, body)) or "SM9")
    return calls


def webhook(base: str, params: dict, *, token: str = TOKEN) -> tuple[int, bytes]:
    signature = twilio.signature("https://farm.example.com/sms/twilio", params, token)
    return call(f"{base}/sms/twilio", method="POST", data=urllib.parse.urlencode(params).encode(),
                headers={"X-Twilio-Signature": signature,
                         "Content-Type": "application/x-www-form-urlencoded"})


def test_reply_by_api_sends_the_answer_and_leaves_the_webhook_empty(base: str, app: App,
                                                                    sent: list) -> None:
    from dataclasses import replace
    app.settings = replace(app.settings, reply_by_api=True)
    status, body = webhook(base, {"From": "+19565550123", "Body": "hola", "MessageSid": "SM1",
                                  "NumMedia": "0"})
    assert status == 200
    assert "<Message>" not in body.decode()
    assert sent and sent[0][0] == "+19565550123"
    with app.db() as conn:
        statuses = [row[0] for row in conn.execute(
            "select status from messages where direction = 'out'")]
    assert statuses == ["sent"] * len(sent)


def test_a_signed_text_gets_twiml_back(base: str, app: App) -> None:
    status, body = webhook(base, {"From": "+19565550123", "Body": "hola", "MessageSid": "SM1",
                                  "NumMedia": "0"})
    assert status == 200
    assert body.decode().startswith('<?xml version="1.0" encoding="UTF-8"?><Response><Message>'
                                    'Dos Ojos: le decimos cuando regar')
    with app.db() as conn:
        rows = store.messages(conn, "+19565550123")
    assert [r["direction"] for r in rows] == ["in", "out"]


def test_a_redelivered_text_is_answered_once(base: str) -> None:
    params = {"From": "+19565550123", "Body": "hola", "MessageSid": "SM1", "NumMedia": "0"}
    webhook(base, params)
    status, body = webhook(base, params)
    assert status == 200 and b"<Message>" not in body


def test_a_forged_text_is_refused(base: str) -> None:
    status, _ = webhook(base, {"From": "+19565550123", "Body": "hola"}, token="wrong")
    assert status == 403


def test_twilio_handling_stop_itself_means_no_answer(base: str, app: App) -> None:
    status, body = webhook(base, {"From": "+19565550123", "Body": "STOP", "MessageSid": "SM2",
                                  "OptOutType": "STOP"})
    assert status == 200 and b"<Message>" not in body
    with app.db() as conn:
        assert store.get_farmer(conn, "+19565550123").opted_out_at


def test_a_lost_text_is_recorded(base: str, app: App) -> None:
    with app.db() as conn:
        message_id = store.log_out(conn, "+19565550123", "Aviso", status="sent", provider_id="SM7")
    params = {"MessageSid": "SM7", "MessageStatus": "undelivered", "ErrorCode": "30034"}
    signature = twilio.signature("https://farm.example.com/sms/status", params, TOKEN)
    status, _ = call(f"{base}/sms/status", method="POST",
                     data=urllib.parse.urlencode(params).encode(),
                     headers={"X-Twilio-Signature": signature})
    assert status == 204
    with app.db() as conn:
        row = conn.execute("SELECT status, error FROM messages WHERE id = ?", (message_id,)).fetchone()
    assert row["status"] == "undelivered" and "10DLC" in row["error"]


# ---- simulator -----------------------------------------------------------------------------


def test_the_simulator_talks(base: str) -> None:
    status, page = call(f"{base}/sim")
    assert status == 200 and b"Dos Ojos" in page
    assert post_json(f"{base}/sim/send", {"phone": "+19565550150", "body": "hola"})[0] == 200
    status, body = call(f"{base}/sim/messages?phone=%2B19565550150&after=0")
    messages = json.loads(body)["messages"]
    assert [m["direction"] for m in messages] == ["in", "out"]
    assert messages[1]["segments"] == 2 and messages[1]["status"] == "kept"


def test_the_simulator_refuses_tunnels(base: str) -> None:
    status, _ = call(f"{base}/sim", headers={"X-Forwarded-For": "8.8.8.8"})
    assert status == 403


def test_the_simulator_is_off_by_default(app: App) -> None:
    app.sim = False
    server, url = serve(app)
    try:
        assert call(f"{url}/sim")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


# ---- the map ------------------------------------------------------------------------------


def farmer_with_a_pin(app: App, channel: str = "sms") -> str:
    with app.db() as conn:
        farmer = store.add_farmer(conn, "+19565550123", channel=channel)
        farmer.lang, farmer.state, farmer.name = "es", "idle", "Juan"
        store.save_farmer(conn, farmer)
        record = store.add_field(conn, farmer.phone, "Campo Norte")
        record.lat, record.lon, record.acres_said = 26.1488, -97.997, 40.0
        store.save_field(conn, record)
        return store.new_link(conn, "map", record.id, days=14)


def test_the_map_page_and_saving_the_corners(base: str, app: App, sent: list) -> None:
    token = farmer_with_a_pin(app)
    status, page = call(f"{base}/f/{token}")
    assert status == 200 and "Campo Norte".encode() in page and b"leaflet" in page

    status, result = post_json(f"{base}/f/{token}", {"coordinates": SQUARE})

    assert status == 200 and result["ok"] and 35 < result["acres"] < 45
    with app.db() as conn:
        record = store.get_field(conn, "F001")
    assert record.outline["type"] == "Polygon" and record.outline_by == "farmer"
    assert len(sent) == 1 and sent[0][1].startswith("Guardamos el mapa de Campo Norte:")
    assert "Usted dijo 40." in sent[0][1]


def test_the_why_page_and_its_download(base: str, app: App) -> None:
    with app.db() as conn:
        farmer = store.add_farmer(conn, "+19565550123")
        farmer.lang, farmer.state, farmer.name = "es", "idle", "Juan"
        store.save_farmer(conn, farmer)
        record = store.add_field(conn, farmer.phone, "Campo Norte")
        record.crop, record.irrigation, record.acres = "sorghum", "furrow", 40.0
        record.outline = {"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]}
        store.save_field(conn, record)
        token = store.new_link(conn, "explain", record.id, days=14)

    status, page = call(f"{base}/r/{token}")
    assert status == 200
    assert "Por qué: Campo Norte".encode() in page
    assert b"<h2>La respuesta</h2>" in page

    request = urllib.request.Request(f"{base}/r/{token}/file")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        assert "attachment" in response.headers["Content-Disposition"]
        assert response.read().startswith(b"<!doctype html>")


def test_an_expired_why_link_says_how_to_get_another(base: str, app: App) -> None:
    status, page = call(f"{base}/r/never-existed")
    assert status == 404
    assert b"PORQUE" in page          # no Accept-Language, so the Spanish page


def test_crossed_lines_are_straightened_or_refused(base: str, app: App, sent: list) -> None:
    token = farmer_with_a_pin(app)
    bow_tie = [SQUARE[0], SQUARE[2], SQUARE[1], SQUARE[3]]
    status, result = post_json(f"{base}/f/{token}", {"coordinates": bow_tie})
    assert (status == 200 and result["acres"] < 40) or status == 400


def test_two_corners_are_not_a_field(base: str, app: App) -> None:
    token = farmer_with_a_pin(app)
    status, result = post_json(f"{base}/f/{token}", {"coordinates": SQUARE[:2]})
    assert status == 400 and "three" in result["error"]


def test_an_unknown_link_is_gone(base: str) -> None:
    status, page = call(f"{base}/f/nope")
    assert status == 404 and "ya no sirve".encode() in page


def test_a_team_drawn_map_tells_the_farmer(base: str, app: App, sent: list) -> None:
    farmer_with_a_pin(app)
    with app.db() as conn:
        token = store.new_link(conn, "map", "F001", days=14, meta={"by": "team"})
    post_json(f"{base}/f/{token}", {"coordinates": SQUARE})
    with app.db() as conn:
        assert store.get_field(conn, "F001").outline_by == "team"
    assert sent and "Nuestro equipo marcó".replace("ó", "o") in sent[0][1]


# ---- uploads -------------------------------------------------------------------------------


def an_upload(app: App) -> str:
    farmer_with_a_pin(app)
    with app.db() as conn:
        token = store.new_link(conn, "upload", "F001", days=14)
        folder = app.settings.drone_workspace / "data" / "raw" / "F001-20260912"
        store.new_upload(conn, token, flight_id="F001-20260912", folder=str(folder),
                         flown_on=NOW.date(), bare_soil=True)
    return token


def test_a_big_file_arrives_in_pieces(base: str, app: App, sent: list) -> None:
    token = an_upload(app)
    whole = bytes(range(256)) * 40                              # 10,240 bytes
    url = f"{base}/u/{token}/file/map.tif"
    piece = lambda start, end: call(f"{url}?offset={start}&total={len(whole)}", method="PUT",
                                    data=whole[start:end])
    assert piece(0, 4000)[0] == 200
    assert piece(8000, len(whole))[0] == 409                    # the middle is missing
    assert piece(4000, 6000)[0] == 200                          # cut off on the way...
    status, body = piece(4000, 8000)                            # ...so sent again whole
    assert status == 200 and json.loads(body)["received"] == 8000
    listed = json.loads(call(f"{base}/u/{token}/files")[1])["files"]
    assert "map.tif" not in listed                              # not finished yet
    status, body = piece(8000, len(whole))
    assert status == 200 and json.loads(body)["bytes"] == len(whole)
    folder = app.settings.drone_workspace / "data" / "raw" / "F001-20260912"
    assert (folder / "map.tif").read_bytes() == whole
    assert call(f"{url}?offset=9000&total={len(whole)}", method="PUT",
                data=b"x" * 2000)[0] == 400                     # past the end


def test_uploading_a_flight(base: str, app: App, sent: list) -> None:
    token = an_upload(app)
    assert call(f"{base}/u/{token}")[0] == 200
    for name, content in (("DJI_0001.JPG", b"a" * 1000), ("DJI_0002.JPG", b"b" * 2000)):
        status, body = call(f"{base}/u/{token}/file/{name}", method="PUT", data=content)
        assert status == 200, body
    call(f"{base}/u/{token}/file/DJI_0001.JPG", method="PUT", data=b"a" * 1000)   # a resend
    listed = json.loads(call(f"{base}/u/{token}/files")[1])["files"]
    assert listed == {"DJI_0001.JPG": 1000, "DJI_0002.JPG": 2000}

    status, result = post_json(f"{base}/u/{token}/done", {})

    assert status == 200 and result == {"ok": True, "files": 2, "flight": "F001-20260912"}
    flights = json.loads((app.settings.drone_workspace / "flights.json").read_text())["flights"]
    assert flights["F001-20260912"]["field_id"] == "F001"
    assert (app.settings.drone_workspace / "data" / "raw" / "F001-20260912" / "DJI_0002.JPG").exists()
    assert sent and sent[0][1].startswith("Recibimos 2 fotos (3.0 KB) de Campo Norte")


def test_only_flight_files_are_taken(base: str, app: App) -> None:
    token = an_upload(app)
    status, body = call(f"{base}/u/{token}/file/virus.exe", method="PUT", data=b"x")
    assert status == 415
    status, body = call(f"{base}/u/{token}/file/..%2F..%2Fescape.jpg", method="PUT", data=b"x")
    assert status == 200 and json.loads(body)["name"] == "escape.jpg"
    assert not (app.settings.drone_workspace / "data" / "escape.jpg").exists()


def test_a_laser_flight_is_taken_too(base: str, app: App) -> None:
    """A lidar drone writes point clouds and nothing else; refusing them refuses the flight."""
    token = an_upload(app)
    for name in ("20180710_field54_dsm_4cm.las", "scan.laz", "ortho_1cm.tif"):
        status, body = call(f"{base}/u/{token}/file/{name}", method="PUT", data=b"x" * 10)
        assert status == 200, body


def test_done_with_nothing_uploaded(base: str, app: App) -> None:
    token = an_upload(app)
    status, result = post_json(f"{base}/u/{token}/done", {})
    assert status == 400 and not result["ok"]


def test_every_page_text_is_there_in_both_languages() -> None:
    from dosojos_sms.web import PAGE_TEXT

    for group in (PAGE_TEXT["map"], PAGE_TEXT["upload"]):
        for pair in group.values():
            assert len(pair) == 2 and all(pair)
    assert text.pick(PAGE_TEXT["gone"], "en").startswith("This link")
