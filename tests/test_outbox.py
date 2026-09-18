"""Texts nobody asked for: never to a STOP, never at night, never by accident."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from dosojos_sms import outbox, store, twilio
from dosojos_sms.config import Settings

FARM = ZoneInfo("America/Chicago")
NOON = datetime(2026, 9, 12, 12, 0, tzinfo=FARM)
NIGHT = datetime(2026, 9, 12, 22, 30, tzinfo=FARM)


@pytest.fixture
def ready(tmp_path) -> Settings:
    settings = Settings.load(tmp_path, env={"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t",
                                            "TWILIO_FROM": "+19565550100"})
    settings.ensure_dirs()
    return settings


@pytest.fixture
def db(ready: Settings):
    connection = store.connect(ready.db_path)
    yield connection
    connection.close()


@pytest.fixture
def sent(monkeypatch) -> list:
    calls = []
    monkeypatch.setattr(twilio, "send", lambda settings, to, body, media=None:
                        calls.append((to, body)) or "SM1")
    return calls


def farmer(db, channel: str = "sms") -> store.Farmer:
    return store.add_farmer(db, "+19565550123", channel=channel)


def test_by_day_it_goes_out(db, ready, sent) -> None:
    assert outbox.deliver(db, ready, farmer(db), "Aviso: riegue", now=NOON) == "sent"
    assert sent == [("+19565550123", "Aviso: riegue")]


def test_accents_are_made_gsm_before_sending(db, ready, sent) -> None:
    outbox.deliver(db, ready, farmer(db), "¿Ya regó? Está bien.", now=NOON)
    assert sent[0][1] == "¿Ya rego? Esta bien."


def test_at_night_it_waits_for_eight_in_the_morning(db, ready, sent) -> None:
    assert outbox.deliver(db, ready, farmer(db), "Aviso", now=NIGHT) == "queued"
    row = store.queued(db, "2026-09-13T13:00:00+00:00")[0]
    assert row["send_after"] == "2026-09-13T13:00:00+00:00"      # 8 am CDT
    assert not sent
    assert outbox.flush(db, ready, now=NIGHT) == 0
    assert outbox.flush(db, ready, now=datetime(2026, 9, 13, 8, 5, tzinfo=FARM)) == 1
    assert sent == [("+19565550123", "Aviso")]


def test_an_answer_to_the_farmers_own_action_goes_at_any_hour(db, ready, sent) -> None:
    assert outbox.deliver(db, ready, farmer(db), "Guardamos el mapa", now=NIGHT,
                          urgent=True) == "sent"


def test_never_to_someone_who_said_stop(db, ready, sent) -> None:
    record = farmer(db)
    record.opted_out_at = store.now_iso()
    assert outbox.deliver(db, ready, record, "Aviso", now=NOON) == "opted out"
    assert not sent


def test_simulator_farmers_keep_their_texts(db, ready, sent) -> None:
    assert outbox.deliver(db, ready, farmer(db, "sim"), "Aviso", now=NOON) == "kept"
    assert not sent


def test_without_twilio_nothing_leaves_the_computer(tmp_path) -> None:
    settings = Settings.load(tmp_path, env={})
    settings.ensure_dirs()
    db = store.connect(settings.db_path)
    assert outbox.deliver(db, settings, farmer(db), "Aviso", now=NOON) == "kept"


def test_a_stop_reported_by_twilio_is_remembered(db, ready, monkeypatch) -> None:
    def refuse(settings, to, body):
        raise twilio.TwilioError("stop", 21610)

    monkeypatch.setattr(twilio, "send", refuse)
    record = farmer(db)
    assert outbox.deliver(db, ready, record, "Aviso", now=NOON) == "failed"
    assert store.get_farmer(db, record.phone).opted_out_at
