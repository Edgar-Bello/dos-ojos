"""The local AI: reading farmers' own words, and the checked recommendation.

A stand-in plays the model, answering from a script, so these run with no model
installed and say exactly what Dos Ojos does with each kind of answer.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import NOW, SQUARE, FakeStatus, FakeWater, resolver_to

from dosojos_sms import ai, explain, store
from dosojos_sms.config import Settings
from dosojos_sms.status import FieldWater
from dosojos_sms.web import App

TODAY = NOW.date()


class Model:
    """Plays the language model: hands out scripted answers, keeps what it was asked."""

    model = "llama3.2:3b"

    def __init__(self, *answers: dict):
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def json(self, system, prompt, schema, *, max_tokens=350) -> dict:
        self.asked.append((system, prompt))
        if not self.answers:
            raise ai.AIError("no more answers")
        return self.answers.pop(0)


def use(monkeypatch, model: Model) -> Model:
    monkeypatch.setattr(ai, "for_settings", lambda settings: model)
    return model


# --------------------------------------------------------------------------- #
# Talking to Ollama
# --------------------------------------------------------------------------- #


def test_the_client_asks_ollama_for_json_held_to_a_schema() -> None:
    sent = {}

    def post(url, payload, timeout):
        sent.update(url=url, payload=payload)
        return {"message": {"content": json.dumps({"intent": "status"})}}

    client = ai.LocalAI("http://127.0.0.1:11434/", "llama3.2:3b", post=post,
                        get=lambda url, timeout: {"models": [{"name": "llama3.2:3b"}]})
    assert client.available()
    assert client.json("sys", "hi", {"type": "object"}) == {"intent": "status"}
    assert sent["url"] == "http://127.0.0.1:11434/api/chat"
    assert sent["payload"]["format"] == {"type": "object"} and not sent["payload"]["stream"]


def test_a_missing_model_or_server_means_no_ai() -> None:
    def refused(url, timeout):
        raise OSError("connection refused")

    assert not ai.LocalAI("http://x", "m", get=refused).available()
    assert not ai.LocalAI("http://x", "llama3.2:3b",
                          get=lambda u, t: {"models": [{"name": "other"}]}).available()


def test_garbage_from_the_model_is_an_error_not_a_crash() -> None:
    client = ai.LocalAI("http://x", "m", post=lambda u, p, t: {"message": {"content": "not json"}})
    with pytest.raises(ai.AIError):
        client.json("s", "p", {})


def test_ai_is_off_when_asked(tmp_path) -> None:
    settings = Settings.load(tmp_path, env={"DOSOJOS_AI": "0"})
    assert settings.ai_url is None


# --------------------------------------------------------------------------- #
# Reading a farmer's own words
# --------------------------------------------------------------------------- #

FIELDS = [{"name": "Campo Norte", "summary": "sorghum"}, {"name": "La Loma", "summary": "cotton"}]


def test_a_rain_report_in_the_farmers_words_becomes_the_bots_command() -> None:
    model = Model({"intent": "rain", "field": "", "crop": "", "answer": ""})
    heard = ai.understand(model, "anoche cayo como una pulgada de agua", lang="es",
                          today=TODAY, fields=FIELDS)
    # The model says what it is; the rules read "anoche" and "una pulgada".
    assert heard.command_text("") == "rain 9/11 1"
    assert "TODAY: 2026-09-12" in model.asked[0][1] and "Campo Norte" in model.asked[0][1]


def test_an_invented_field_or_a_day_to_come_is_dropped() -> None:
    model = Model({"intent": "irrigated", "field": "Rancho Grande", "crop": "",
                   "answer": "should be ignored"})
    heard = ai.understand(model, "regamos", lang="es", today=TODAY, fields=FIELDS)
    assert (heard.field, heard.day, heard.inches, heard.answer) == (None, None, None, None)
    assert heard.command_text("regamos") == "watered"


def test_a_question_keeps_its_answer() -> None:
    model = Model({"intent": "question", "field": "La Loma", "crop": "",
                   "answer": "La Loma necesita agua en unos 5 dias."})
    heard = ai.understand(model, "cuando le toca a la loma?", lang="es", today=TODAY,
                          fields=FIELDS)
    assert heard.answer.startswith("La Loma necesita") and heard.command_text("") is None


# --------------------------------------------------------------------------- #
# The recommendation and its checks
# --------------------------------------------------------------------------- #

BRIEF = {"today": "2026-09-12",
         "field": {"name": "Campo Norte", "crop": "sorghum", "watered_by": "furrows"},
         "water_checkbook": {"status": "water this week", "days_until_water": 5,
                             "days_range": [4, 7], "water_by": "2026-09-17", "refill_in": 5.26,
                             "soil_water_left_pct": 66, "confidence": "high"},
         "drone_trees_ai": {"flown": "2026-09-10", "trees": 424, "need_a_look": 32,
                            "gaps_missing_trees": 44, "typical_height_m": 1.8}}

GOOD = {"message": "Todavia no: riegue en unos 5 dias, antes del jueves 17, con 5.26 pulgadas. "
                   "Revise los 32 arboles marcados por el dron.",
        "check_first": "los 32 arboles marcados", "reasons": ["El balance de agua da 5 dias."],
        "confidence": "high"}


def test_the_facts_lead_with_the_water_balances_decision() -> None:
    lines = ai.facts(BRIEF)
    assert lines[0].startswith("DECISION") and "about 5 days" in lines[0]
    assert "Thursday September 17" in lines[0] and "5.26 inches" in lines[0]
    assert any("424 trees, 32 need a look, 44 gaps" in line for line in lines)


def test_a_sound_recommendation_passes() -> None:
    assert ai.check(GOOD, BRIEF) == []


@pytest.mark.parametrize("change, problem", [
    ({"message": "Riegue pronto y revise los arboles."}, "does not give the DECISION's day"),
    ({"message": "Riegue en 5 dias con 7.8 pulgadas."}, "7.8 is not in the FACTS"),
    ({"message": "Riegue en 5 dias. " + "x" * 400}, "characters"),
])
def test_an_answer_the_numbers_do_not_back_is_refused(change, problem) -> None:
    assert any(problem in p for p in ai.check({**GOOD, **change}, BRIEF))


def test_a_date_the_facts_do_not_have_is_refused() -> None:
    wrong = {**GOOD, "message": "Riegue en unos 5 dias, antes del 2026-09-23."}
    assert any("9/23 is not in the FACTS" in p for p in ai.check(wrong, BRIEF))
    assert any("9/23" in p for p in ai.check({**GOOD, "message": "Riegue en 5 dias, el 23 de "
                                                                 "septiembre."}, BRIEF))
    assert ai.check({**GOOD, "message": "Riegue en 5 dias, el 17 de septiembre."}, BRIEF) == []


def test_a_crop_word_finds_the_field_the_model_missed() -> None:
    model = Model({"intent": "irrigated", "field": "", "crop": "", "answer": ""})
    fields = [{"name": "Norte", "crop": "sorghum"}, {"name": "Edgar Citrus", "crop": "citrus"}]
    heard = ai.understand(model, "le dimos una regada a la huerta antier como 2 pulgadas",
                          lang="es", today=TODAY, fields=fields)
    assert heard.command_text("") == "watered Edgar Citrus 9/10 2"


def test_a_repeated_check_is_not_said_twice() -> None:
    from dosojos_sms.bot import ai_text
    advice = ai.Advice("water_now", 0, "Riegue hoy. Revise la mancha caliente del oeste.",
                       "la mancha caliente del oeste", [], "high", "m", "t")
    assert "Revise primero" not in ai_text("Norte", advice, "es")


def test_a_rainfed_field_is_never_told_to_water() -> None:
    rainfed = {**BRIEF, "field": {**BRIEF["field"], "watered_by": "rainfed (not irrigated)"}}
    assert ai.check({**GOOD, "message": "Riegue ahora mismo."}, rainfed)
    assert ai.check({**GOOD, "message": "Siga vigilando el campo."}, rainfed) == []


def test_water_now_must_say_now() -> None:
    urgent = {**BRIEF, "water_checkbook": {**BRIEF["water_checkbook"], "days_until_water": 0}}
    assert ai.check({**GOOD, "message": "Riegue pronto."}, urgent)
    assert ai.check({**GOOD, "message": "Riegue hoy con 5.26 pulgadas."}, urgent) == []


def test_a_refused_answer_is_asked_again_then_given_up() -> None:
    wrong = {**GOOD, "message": "Riegue pronto."}
    model = Model(wrong, GOOD)
    advice = ai.recommend(model, BRIEF, lang="es", now="2026-09-12T10:00")
    assert advice.message.startswith("Todavia no: riegue en unos 5 dias") and len(model.asked) == 2
    assert "WAS REFUSED" in model.asked[1][1]
    assert (advice.action, advice.water_in_days) == ("no_water_yet", 5)   # from the balance
    assert ai.recommend(Model(wrong, wrong), BRIEF, lang="es", now="x") is None


# --------------------------------------------------------------------------- #
# In the conversation
# --------------------------------------------------------------------------- #


@pytest.fixture
def app(tmp_path) -> App:
    settings = Settings.load(tmp_path / "farm_data", env={})
    settings.ensure_dirs()
    application = App(settings, sim=True, resolve=resolver_to("none"), water=FakeWater())
    application.now = NOW
    application.thinker = ai.Thinker(hold=True)
    with application.db() as conn:
        farmer = store.add_farmer(conn, "+19565550123", channel="sim")
        farmer.lang, farmer.state, farmer.name, farmer.alerts = "es", "idle", "Rosa", True
        store.save_farmer(conn, farmer)
        record = store.add_field(conn, farmer.phone, "Campo Norte")
        record.crop, record.irrigation, record.plan = "sorghum", "furrow", "satellite"
        record.outline = {"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]}
        store.save_field(conn, record)
        store.add_event(conn, record.id, date(2026, 7, 20), "planted")
    return application


def texts(app: App) -> list[str]:
    with app.db() as conn:
        return [r["body"] for r in store.messages(conn, "+19565550123")]


def say(app: App, body: str) -> None:
    from dosojos_sms.bot import Inbound
    app.receive(Inbound("+19565550123", body, channel="sim"), reply_status="kept")
    app.thinker.run_held()          # what the AI's own thread does next


def test_words_the_rules_miss_are_read_by_the_ai_and_still_read_back(app, monkeypatch) -> None:
    use(monkeypatch, Model({"intent": "irrigated", "field": "Campo Norte", "crop": "",
                            "answer": ""}))
    say(app, "le dimos una buena mojada al de la carretera ayer, como 3 pulgadas")
    got = texts(app)
    assert "Déjeme leer eso con la IA" in got[-3]
    assert got[-2] == "La IA entendio: watered Campo Norte 9/11 3"      # GSM has no ó
    assert "riego en Campo Norte el vie 11 sep (ayer), 3 pulgadas" in got[-1]
    say(app, "si")
    with app.db() as conn:
        assert [(e.day, e.inches) for e in store.events_for(conn, "F001")
                if e.kind == "irrigated"] == [(date(2026, 9, 11), 3.0)]


def test_a_question_is_answered_from_the_fields_numbers(app, monkeypatch) -> None:
    model = use(monkeypatch, Model({"intent": "question", "field": "Campo Norte", "crop": "",
                                    "answer": "Campo Norte necesita agua en unos 5 dias."}))
    say(app, "oiga y cuanto le falta al campo norte pa regarlo")
    assert texts(app)[-1] == "Campo Norte necesita agua en unos 5 dias."
    assert "water in 5 days" in model.asked[0][1]           # it was given the numbers


def test_without_the_ai_the_text_goes_to_the_team_as_before(app) -> None:
    say(app, "le dimos una buena mojada al de la carretera norte ayercito")
    assert "se lo pasé al equipo" in texts(app)[-1]


def test_water_sends_the_ais_recommendation_once_written(app, monkeypatch) -> None:
    use(monkeypatch, Model({**GOOD, "message": "Riegue en unos 5 dias, antes del jue 17 sep.",
                            "check_first": "que el agua llegue al final del surco"}))
    say(app, "AGUA")
    got = texts(app)
    # The checkbook answers at once; the AI's recommendation follows when written.
    assert got[-4].startswith("Campo Norte") and "La IA esta revisando" in got[-3]
    assert got[-1].startswith("Campo Norte: Riegue en unos 5 dias, antes del jue 17 sep.")
    assert "Revise primero: que el agua llegue al final del surco." in got[-1]
    assert "IA local, revisada con las cuentas del agua" in got[-1]
    say(app, "AGUA")                                         # the same readings: kept
    assert texts(app)[-2].startswith("Campo Norte: Riegue en unos 5 dias")


def test_a_recommendation_that_fails_its_checks_never_reaches_the_farmer(app, monkeypatch) -> None:
    bad = {**GOOD, "message": "Riegue pronto."}                   # no day: refused
    use(monkeypatch, Model(bad, bad))
    say(app, "AGUA")
    assert not any("IA local" in t for t in texts(app))


#: For the test field, which has no drone: the 32 trees would be refused as made up.
PLAIN = {**GOOD, "message": "Todavia no: riegue en unos 5 dias, antes del jueves 17.",
         "check_first": ""}


def test_a_finished_reading_texts_the_ais_recommendation(app, monkeypatch) -> None:
    use(monkeypatch, Model(PLAIN))
    app._read_done("F001", True, 1)
    got = texts(app)
    assert got[-2].startswith("Campo Norte: Todavia no: riegue en unos 5 dias")
    assert "PORQUE" in got[-1]


def test_the_why_page_leads_with_the_ai_and_shows_how_it_was_checked(app, monkeypatch,
                                                                      settings) -> None:
    use(monkeypatch, Model(PLAIN))
    advice = app._prepare_advice("F001")
    with app.db() as conn:
        record, farmer = store.get_field(conn, "F001"), store.get_farmer(conn, "+19565550123")
    item = FieldWater(record, status=FakeStatus(), intake="slow")
    page = explain.build(app.settings, farmer, item, [], today=TODAY, advice=advice)
    assert "Lo que recomienda la IA" in page
    assert "Campo Norte: Todavia no: riegue en unos 5 dias, antes del jueves 17.</div>" in page
    assert "llama3.2:3b" in page and "cada número sale de las lecturas" not in page
    assert "every number comes from the readings" in page
    assert "Lo que dicen las cuentas del agua solas" in page
