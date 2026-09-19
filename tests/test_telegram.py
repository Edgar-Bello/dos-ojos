"""Telegram as a stand-in for SMS: messages in, answers out, never a real call."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import NOW, FakeWater, resolver_to

from dosojos_sms import outbox, store, telegram
from dosojos_sms.config import Settings
from dosojos_sms.web import App


class Response:
    def __init__(self, payload: dict, status: int = 200):
        self.payload, self.status_code = payload, status

    def json(self) -> dict:
        return self.payload


@pytest.fixture
def settings(tmp_path) -> Settings:
    settings = Settings.load(tmp_path / "farm_data", env={
        "TELEGRAM_BOT_TOKEN": "123:abc", "DOSOJOS_PUBLIC_URL": "https://farm.example.com"})
    settings.ensure_dirs()
    return settings


@pytest.fixture
def calls(monkeypatch) -> list:
    made = []

    def post(url, data, timeout):
        made.append((url.rsplit("/", 1)[-1], data))
        return Response({"ok": True, "result": {"message_id": len(made)}})

    monkeypatch.setattr(telegram.requests, "post", post)
    return made


def update(update_id: int, **message) -> dict:
    return {"update_id": update_id,
            "message": {"chat": {"id": 42, "type": "private"}, **message}}


def test_a_chat_is_kept_as_a_number_no_phone_can_have() -> None:
    phone = telegram.phone_for(42)
    assert phone == "+042" and telegram.is_telegram(phone) and telegram.chat_of(phone) == 42
    assert not telegram.is_telegram(store.normalize_phone("956-555-0123"))


def test_opening_the_bot_says_hola(settings: Settings) -> None:
    message = telegram.inbound(settings, update(1, text="/start"))
    assert (message.phone, message.body, message.channel) == ("+042", "hola", "telegram")


def test_groups_and_a_shared_location(settings: Settings) -> None:
    group = {"update_id": 2, "message": {"chat": {"id": -5, "type": "group"}, "text": "hi"}}
    assert telegram.inbound(settings, group) is None
    pin = telegram.inbound(settings, update(3, location={"latitude": 26.2, "longitude": -98.1}))
    assert (pin.lat, pin.lon) == (26.2, -98.1)


def test_send_splits_long_text_and_sends_public_pictures(settings: Settings, calls: list) -> None:
    telegram.send(settings, "+042", "a" * 5000,
                  media=["https://farm.example.com/p/x", "http://localhost/p/y"])
    assert [method for method, _ in calls] == ["sendMessage", "sendMessage", "sendPhoto"]
    assert calls[2][1]["photo"] == "https://farm.example.com/p/x"


def test_a_refusal_never_shows_the_token(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(telegram.requests, "post", lambda url, data, timeout: Response(
        {"ok": False, "error_code": 401, "description": "Unauthorized"}, 401))
    with pytest.raises(telegram.TelegramError) as caught:
        telegram.send(settings, "+042", "hola")
    assert "123:abc" not in str(caught.value) and "@BotFather" in str(caught.value)


def test_answers_go_out_through_telegram(settings: Settings, calls: list) -> None:
    app = App(settings, resolve=resolver_to("none"), water=FakeWater())
    app.now = NOW
    replies = app.receive(telegram.inbound(settings, update(4, text="hola")),
                          provider_id="tg4")
    assert replies == []                                 # already sent, not returned
    assert calls and calls[0][0] == "sendMessage" and calls[0][1]["chat_id"] == 42
    with app.db() as conn:
        assert store.get_farmer(conn, "+042").channel == "telegram"
        assert outbox.can_send(settings, "telegram")
        assert not outbox.can_send(replace(settings, telegram_token=None), "telegram")


def test_our_own_pictures_go_as_files_not_links(settings: Settings, monkeypatch, tmp_path) -> None:
    picture = tmp_path / "flag_overlay.png"
    picture.write_bytes(b"PNG fake")
    made = []

    def post(url, data, timeout, files=None):
        made.append((url.rsplit("/", 1)[-1], data, files))
        return Response({"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(telegram.requests, "post", post)
    with store.session(settings.db_path) as conn:
        farmer = store.add_farmer(conn, "+042", channel="telegram")
        field = store.add_field(conn, "+042", "Campo Norte")
        token = store.new_link(conn, "picture", field.id, days=14, meta={"path": str(picture)})
        # Still inside this unfinished write, as the server is when a flight is done:
        # the picture must be found without a second connection ("database is locked").
        outbox.deliver(conn, settings, farmer, "listo", urgent=True,
                       media=[settings.link(f"p/{token}")])
    method, data, files = made[-1]
    assert method == "sendPhoto" and "photo" not in data
    assert files["photo"] == ("flag_overlay.png", b"PNG fake")
