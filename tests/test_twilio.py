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


def test_quotes_inside_a_value_are_kept(tmp_path) -> None:
    (tmp_path / "sms").mkdir()
    (tmp_path / "sms" / "sms.env").write_text(
        'DOSOJOS_ON_UPLOAD="C:/py/python.exe" "step.py" --data "C:/farm"\n'
        "DOSOJOS_READ_NOW=1\n", encoding="utf-8")
    settings = Settings.load(tmp_path, env={})
    assert settings.on_upload == '"C:/py/python.exe" "step.py" --data "C:/farm"'
    assert settings.read_now


def test_fetch_media(ready: Settings, tmp_path) -> None:
    def get(url, auth, timeout):
        assert auth == ("AC1", "tok")
        return Response(200, {}, content=b"jpegbytes")

    path = twilio.fetch_media(ready, "https://api.twilio.com/m/1", tmp_path / "m", "x", get=get)
    assert path.name == "x.jpg" and path.read_bytes() == b"jpegbytes"


def test_the_number_is_pointed_at_the_new_address(ready: Settings) -> None:
    """A quick tunnel's address changes each start; the number follows it."""
    calls = []

    def get(url, params, auth, timeout):
        calls.append(("get", url, params))
        return Response(200, {"incoming_phone_numbers": [
            {"sid": "PN1", "phone_number": ready.twilio_from}]})

    def post(url, data, auth, timeout):
        calls.append(("post", url, data))
        return Response(200, {"sid": "PN1"})

    number = twilio.point_number_at(ready, "https://a-b.trycloudflare.com/sms/twilio",
                                    get=get, post=post)
    assert number == ready.twilio_from
    assert calls[0][2] == {"PhoneNumber": ready.twilio_from}
    assert calls[1][1].endswith("/IncomingPhoneNumbers/PN1.json")
    assert calls[1][2] == {"SmsUrl": "https://a-b.trycloudflare.com/sms/twilio",
                           "SmsMethod": "POST"}


def test_a_number_not_on_the_account_is_said_plainly(ready: Settings) -> None:
    get = lambda url, params, auth, timeout: Response(200, {"incoming_phone_numbers": []})
    with pytest.raises(twilio.NotOwned, match="not a number on this Twilio account"):
        twilio.point_number_at(ready, "https://x.trycloudflare.com/sms/twilio", get=get)


def test_a_borrowed_trial_number_gets_paste_instructions(monkeypatch, tmp_path) -> None:
    from click.testing import CliRunner
    from dosojos_sms import cli

    def not_owned(settings, url):
        raise twilio.NotOwned("not a number on this Twilio account")

    monkeypatch.setattr(cli.twilio_mod, "point_number_at", not_owned)
    monkeypatch.setenv("DOSOJOS_PUBLIC_URL", "https://x.trycloudflare.com")
    result = CliRunner().invoke(cli.cli, ["--data", str(tmp_path), "webhook"])
    assert result.exit_code == cli.NOT_OWNED_EXIT
    assert "Try out SMS" in result.output
    assert "https://x.trycloudflare.com/sms/twilio" in result.output
