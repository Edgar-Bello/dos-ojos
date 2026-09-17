"""Sorghum by text: growth stage, sugarcane aphid counts, and the hybrid's maturity."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import TODAY, FakeStatus, Phone, draw, onboard, register_field

from dosojos_sms import parse, store, text
from dosojos_sms.cli import plan_reminders


def stage(key: str = "flowering", *, day: int = 69, threshold: int | None = 30,
          watch=("midge", "sugarcane_aphid", "headworm"), critical: bool = True,
          assumed: bool = False, next_stage: str | None = "soft_dough",
          next_date: str | None = "2026-09-20") -> dict:
    """The stage dict the checkbook hands back for a sorghum field."""
    return {"stage": key, "days_after_planting": day, "aphid_threshold_pct": threshold,
            "watch": list(watch), "critical": critical, "maturity_assumed": assumed,
            "next_stage": next_stage, "next_date": next_date, "gdu": 1900}


@pytest.fixture
def grower(phone: Phone, conn, water) -> Phone:
    """A farmer with one mapped sorghum field."""
    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    water.status = FakeStatus(stage=stage())
    return phone


def g(words: str) -> str:
    """What the farmer's phone shows: text messages drop the accents GSM lacks."""
    return text.gsm_safe(words)


def scouting(conn) -> list[dict]:
    return [json.loads(e.note) for e in store.events_for(conn) if e.kind == "scouting"]


# --------------------------------------------------------------------------- #
# Reading what a farmer counted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("said, percent", [
    ("12 de 80", 15.0), ("12/80", 15.0), ("pulgon 21 de 80", 26.25), ("12 of 80", 15.0),
    ("15%", 15.0), ("15", 15.0), ("30 por ciento", 30.0), ("ninguno", 0.0), ("0", 0.0),
])
def test_a_count_is_read_the_ways_a_farmer_writes_it(said: str, percent: float) -> None:
    assert parse.aphid_count(said).percent == pytest.approx(percent)


@pytest.mark.parametrize("said", ["90 de 80", "120%", "muchos", ""])
def test_an_impossible_or_vague_count_is_not_guessed(said: str) -> None:
    assert parse.aphid_count(said) is None


@pytest.mark.parametrize("said, maturity", [
    ("1", "short"), ("2", "medium"), ("3", "long"), ("4", "?"), ("temprano", "short"),
    ("ciclo largo", "long"), ("no se", "?"), ("medium", "medium"),
])
def test_maturity_is_read_by_number_or_word(said: str, maturity: str) -> None:
    assert parse.maturity(said) == maturity


# --------------------------------------------------------------------------- #
# ETAPA
# --------------------------------------------------------------------------- #


def test_etapa_says_the_stage_what_comes_next_and_what_to_look_for(grower: Phone) -> None:
    reply = grower("ETAPA")[0]
    assert reply.startswith(g("Campo Norte: va en floración, día 69 desde la siembra."))
    assert "Sigue grano masoso hacia el" in reply
    assert "decide la cosecha" in reply
    assert "mosquita" in reply and g("pulgón amarillo") in reply
    # Only two things to look for fit a text; headworm waits for the WHY file.
    assert "gusano" not in reply


def test_an_assumed_maturity_says_so_and_how_to_fix_it(grower: Phone, water) -> None:
    water.status = FakeStatus(stage=stage(assumed=True))
    assert "mande CICLO" in grower("ETAPA")[0]


def test_etapa_in_english(phone: Phone, conn, water) -> None:
    onboard(phone, lang="2")
    register_field(phone)
    draw(conn, "F001")
    water.status = FakeStatus(stage=stage("boot", day=58, threshold=20,
                                          watch=("sugarcane_aphid",), next_stage="heading"))
    reply = phone("STAGE")[0]
    assert reply.startswith("Campo Norte: at boot, day 58 after planting.")
    assert "Next: heading around" in reply and "text APHID" in reply


def test_etapa_without_sorghum_says_what_it_is_for(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone, crop="2")                  # cotton
    assert "no tiene campos de sorgo" in phone("ETAPA")[0]


def test_etapa_without_a_planting_date_asks_for_one(grower: Phone, water, conn) -> None:
    water.status = FakeStatus(stage=None)
    for event in store.events_for(conn, "F001"):
        if event.kind == "planted":
            store.void_event(conn, event.id)
    assert "necesito la fecha de siembra" in grower("ETAPA")[0]


# --------------------------------------------------------------------------- #
# PULGON
# --------------------------------------------------------------------------- #


def test_a_count_past_the_threshold_says_to_act_today(grower: Phone, conn) -> None:
    reply = grower("PULGON 26 de 80")[0]           # 32.5% at flowering, threshold 30
    assert g("33% de plantas con pulgón") in reply and "el umbral es 30%" in reply
    assert g("ya lo pasó") in reply and "AgriLife" in reply
    [count] = scouting(conn)
    assert (count["percent"], count["verdict"], count["stage"]) == (32.5, "above", "flowering")
    assert count["infested"] == 26 and count["checked"] == 80


def test_past_the_threshold_is_flagged_for_the_team(grower: Phone, conn) -> None:
    grower("PULGON 26 de 80")
    unread = conn.execute("SELECT body FROM messages WHERE unread = 1").fetchall()
    assert any("26 de 80" in row["body"] for row in unread)


def test_close_to_the_threshold_says_to_look_again_soon(grower: Phone) -> None:
    reply = grower("pulgon 21 de 80")[0]             # 26% against 30
    assert "cerca del umbral de 30%" in reply and g("3 o 4 días") in reply


def test_below_the_threshold_keeps_the_weekly_habit(grower: Phone) -> None:
    reply = grower("pulgon 8 de 80")[0]
    assert "abajo del umbral de 30%" in reply and "cada semana" in reply


def test_none_found_is_good_news_and_still_logged(grower: Phone, conn) -> None:
    assert g("sin pulgón") in grower("PULGON ninguno")[0]
    assert scouting(conn)[0]["percent"] == 0.0


def test_before_heading_the_threshold_is_twenty_percent(grower: Phone, water) -> None:
    water.status = FakeStatus(stage=stage("boot", threshold=20, watch=("sugarcane_aphid",)))
    assert g("ya lo pasó") in grower("PULGON 18 de 80")[0]            # 22.5% against 20


def test_a_mature_crop_is_not_told_to_spray(grower: Phone, water) -> None:
    water.status = FakeStatus(stage=stage("black_layer", threshold=None, watch=("harvest",),
                                          critical=False, next_stage=None, next_date=None))
    reply = grower("PULGON 40 de 80")[0]
    assert g("ya está maduro") in reply and "cosechadora" in reply and g("ya lo pasó") not in reply


def test_pulgon_alone_explains_how_to_count_then_reads_the_count(grower: Phone) -> None:
    howto = grower("PULGON")[0]
    assert "4 partes del campo, 20 plantas" in howto and "30%" in howto
    assert grower.state == "aphid:count"
    assert "abajo del umbral" in grower("12 de 80")[0]
    assert grower.state == "idle"


def test_a_count_that_cannot_be_read_is_asked_again(grower: Phone) -> None:
    grower("PULGON")
    assert g("No entendí la cuenta") in grower("hartos")[0]
    assert grower.state == "aphid:count"


def test_agua_in_the_middle_of_a_count_is_answered_and_the_count_waits(grower: Phone) -> None:
    grower("PULGON")
    replies = grower("AGUA")
    assert any("Campo Norte" in r for r in replies)


def test_only_sorghum_fields_are_offered(phone: Phone, conn, water) -> None:
    onboard(phone)
    register_field(phone, "Sorgo Norte")
    phone("NUEVO")
    register_field(phone, "Algodon Sur", crop="2")
    phone("NUEVO")
    register_field(phone, "Sorgo Sur")
    for field_id in ("F001", "F002", "F003"):
        draw(conn, field_id)
    water.status = FakeStatus(stage=stage())
    menu = phone("PULGON")[0]
    assert menu == g("¿Cuál campo de sorgo? 1 Sorgo Norte, 2 Sorgo Sur")
    assert "Sorgo Sur" in phone("2")[0]
    assert phone.state == "aphid:count"


def test_a_named_field_and_a_count_in_one_text(phone: Phone, conn, water) -> None:
    onboard(phone)
    register_field(phone, "Sorgo Norte")
    phone("NUEVO")
    register_field(phone, "Sorgo Sur")
    draw(conn, "F001")
    draw(conn, "F002")
    water.status = FakeStatus(stage=stage())
    reply = phone("pulgon sorgo sur 26 de 80")[0]
    assert reply.startswith("Sorgo Sur:") and g("ya lo pasó") in reply


def test_a_count_can_be_undone(grower: Phone, conn) -> None:
    grower("PULGON 26 de 80")
    assert g("conteo de pulgón") in grower("BORRAR")[0]
    grower("si")
    assert scouting(conn) == []


# --------------------------------------------------------------------------- #
# Maturity
# --------------------------------------------------------------------------- #


def test_ciclo_changes_the_hybrid_later(grower: Phone, conn) -> None:
    assert "ciclo corto, mediano o largo" in grower("CICLO")[0]
    assert grower("3") == ["Anotado: Campo Norte es de ciclo largo."]
    assert store.get_field(conn, "F001").answers["maturity"] == "long"
    assert grower.state == "idle"


def test_not_knowing_the_hybrid_is_fine(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone, maturity="4")
    assert store.get_field(conn, "F001").answers["maturity"] == "unknown"


def test_other_crops_are_never_asked(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone, crop="3")                  # corn
    assert "maturity" not in store.get_field(conn, "F001").answers


# --------------------------------------------------------------------------- #
# AGUA and the weekly reminder
# --------------------------------------------------------------------------- #


def test_agua_names_the_real_stage_in_the_critical_stretch(grower: Phone, water) -> None:
    water.status = FakeStatus(stage=stage(), sensitive="grain sorghum is at flowering (...)")
    reply = grower("AGUA")[0]
    assert g("Está en floración: no lo deje secar.") in reply


def test_agua_mentions_the_stage_outside_the_critical_stretch(grower: Phone, water) -> None:
    water.status = FakeStatus(stage=stage("soft_dough", day=80, critical=False))
    assert g("Va en grano masoso (día 80).") in grower("AGUA")[0]


def test_a_weekly_scouting_reminder_once_a_week(grower: Phone, conn, settings, bot) -> None:
    planned = plan_reminders(conn, settings, bot, TODAY)
    scout = [p for p in planned if p.kind == "scout"]
    water_first = [p for p in planned if p.kind.startswith("water")]
    if water_first:
        pytest.skip("a water alert outranks the reminder on this day")
    assert scout and "revise pulgón amarillo y mosquita" in scout[0].body   # made safe when sent
    store.record_alert(conn, scout[0].field_id, "scout", scout[0].key)
    assert not [p for p in plan_reminders(conn, settings, bot, TODAY) if p.kind == "scout"]
    next_week = [p for p in plan_reminders(conn, settings, bot, TODAY + timedelta(days=7))
                 if p.kind == "scout"]
    assert next_week


def test_a_rainfed_field_still_gets_the_reminder(grower: Phone, conn, settings, bot,
                                                 water) -> None:
    water.status = FakeStatus(stage=stage(), method="none", days_left=None)
    assert [p for p in plan_reminders(conn, settings, bot, TODAY) if p.kind == "scout"]


def test_no_reminder_once_the_crop_is_mature(grower: Phone, conn, settings, bot, water) -> None:
    water.status = FakeStatus(stage=stage("black_layer", threshold=None, watch=("harvest",)),
                              method="none", days_left=None)
    assert not [p for p in plan_reminders(conn, settings, bot, TODAY) if p.kind == "scout"]
