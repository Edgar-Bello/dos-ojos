"""Twilio's signature, its XML answer, and sending, without the network."""

from __future__ import annotations

import pytest

from dosojos_sms import twilio
from dosojos_sms.config import Settings


def test_the_signature_matches_twilios_own_example() -> None:
    """The vector from Twilio's request-validator tests."""
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675309", "Digits": "1234",
              "From": "+14158675309", "To": "+18005551212"}
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert twilio.signature(url, params, "12345") == "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    assert twilio.valid_signature(url, params, "12345", "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert not twilio.valid_signature(url, {**params, "Digits": "9"}, "12345",
                                      "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert not twilio.valid_signature(url, params, "12345", None)


def test_twiml_escapes_what_it_carries() -> None:
    xml = twilio.twiml(["a < b & c", "dos"])
    assert "<Message>a &lt; b &amp; c</Message><Message>dos</Message>" in xml
    assert twilio.twiml([]).endswith("<Response></Response>")


class Response:
    def __init__(self, status: int, payload: dict, content: bytes = b"", kind: str = "image/jpeg"):
        self.status_code, self._payload = status, payload
        self.content, self.headers, self.text = content, {"Content-Type": kind}, str(payload)

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def ready(tmp_path) -> Settings:
    return Settings.load(tmp_path, env={"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tok",
                                        "TWILIO_FROM": "+19565550100",
                                        "DOSOJOS_PUBLIC_URL": "https://farm.example.com"})


def test_send(ready: Settings) -> None:
    calls = []

    def post(url, data, auth, timeout):
        calls.append((url, data, auth))
        return Response(201, {"sid": "SM123"})

    assert twilio.send(ready, "+19565550123", "hola", post=post) == "SM123"
    url, data, auth = calls[0]
    assert url.endswith("/Accounts/AC1/Messages.json") and auth == ("AC1", "tok")
    assert data["From"] == "+19565550100" and data["Body"] == "hola"
    assert data["StatusCallback"] == "https://farm.example.com/sms/status"


def test_a_refusal_says_why(ready: Settings) -> None:
    def post(url, data, auth, timeout):
        return Response(400, {"code": 21610, "message": "unsubscribed"})

    with pytest.raises(twilio.TwilioError, match="replied STOP") as caught:
        twilio.send(ready, "+19565550123", "hola", post=post)
    assert caught.value.code == 21610


def test_sending_without_keys_is_refused(tmp_path) -> None:
    with pytest.raises(twilio.TwilioError, match="not set up"):
        twilio.send(Settings.load(tmp_path, env={}), "+19565550123", "hola")


def test_half_the_keys_is_an_error(tmp_path) -> None:
    from dosojos_sms.config import ConfigError

    with pytest.raises(ConfigError, match="TWILIO_FROM"):
        Settings.load(tmp_path, env={"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tok"})


def test_keys_from_the_env_file(tmp_path) -> None:
    (tmp_path / "sms").mkdir()
    (tmp_path / "sms" / "sms.env").write_text(
        "# Twilio\nTWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN='tok'\nTWILIO_FROM=+19565550100\n",
        encoding="utf-8")
    settings = Settings.load(tmp_path, env={})
    assert settings.twilio_ready and settings.twilio_token == "tok"


def test_fetch_media(ready: Settings, tmp_path) -> None:
    def get(url, auth, timeout):
        assert auth == ("AC1", "tok")
        return Response(200, {}, content=b"jpegbytes")

    path = twilio.fetch_media(ready, "https://api.twilio.com/m/1", tmp_path / "m", "x", get=get)
    assert path.name == "x.jpg" and path.read_bytes() == b"jpegbytes"
