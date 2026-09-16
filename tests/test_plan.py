"""What a farmer signed up for: satellite alone, plus their drone, plus a thermal camera."""

from __future__ import annotations

import json

import pytest
from conftest import Phone, draw, onboard, register_field

from dosojos_sms import parse, status as status_mod, store, text


# --------------------------------------------------------------------------- #
# Reading the answer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("said, expected", [
    ("1", "satellite"), ("2", "drone"), ("3", "thermal"),
    ("solo satelite", "satellite"), ("satellite only", "satellite"),
    ("con dron", "drone"), ("drone", "drone"),
    ("termica", "thermal"), ("thermal camera", "thermal"),
    ("la termica para plagas", "thermal"),
    # Naming both means the larger of the two, not the first word read.
    ("dron y camara termica", "thermal"),
    ("no se", None), ("", None),
])
def test_the_plan_is_read_from_a_number_or_the_words(said, expected) -> None:
    assert parse.plan(said) == expected


# --------------------------------------------------------------------------- #
# Signing up
# --------------------------------------------------------------------------- #


def test_everyone_is_asked_what_they_want_right_after_consent(phone: Phone) -> None:
    replies = phone.all("hola", "1", "Juan Ejemplo", "si")
    assert "1 S" in replies[-1] and "dron" in replies[-1].lower()
    assert phone.state == "plan"


def test_satellite_only_is_told_it_needs_nothing(phone: Phone) -> None:
    phone.all("hola", "1", "Juan Ejemplo", "si")
    replies = phone("1")
    assert phone.farmer.plan == "satellite"
    assert "No necesita dron" in replies[0]
    assert not any("107" in r for r in replies)      # no licence talk for someone not flying


def test_picking_a_drone_is_told_what_the_law_asks_before_spending_anything(
        phone: Phone) -> None:
    phone.all("hola", "1", "Juan Ejemplo", "si")
    replies = phone("2")
    assert phone.farmer.plan == "drone"
    assert any("107" in r for r in replies)
    assert any("licencia" in r for r in replies)


def test_thermal_says_what_the_extra_camera_buys(phone: Phone) -> None:
    phone.all("hola", "1", "Juan Ejemplo", "si")
    replies = phone("3")
    assert phone.farmer.plan == "thermal"
    assert "plaga" in replies[0]
    assert any("107" in r for r in replies)


def test_a_plan_is_asked_again_until_it_is_understood(phone: Phone) -> None:
    phone.all("hola", "1", "Juan Ejemplo", "si")
    phone("pues no se")
    assert phone.state == "plan"


def test_the_questions_carry_on_after_the_plan(phone: Phone) -> None:
    onboard(phone)
    assert phone.state == "field_name"


# --------------------------------------------------------------------------- #
# Changing it later
# --------------------------------------------------------------------------- #


def test_plan_shows_the_current_one_and_offers_the_menu(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    replies = phone("PLAN")
    # GSM keeps the e-acute and drops the o-acute, so "sólo satélite" goes out like this.
    assert "solo satélite" in replies[0]
    assert phone.state == "plan"
    phone("3")
    assert phone.farmer.plan == "thermal"


def test_changing_the_plan_does_not_start_the_fields_over(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("PLAN")
    phone("2")
    assert phone.state == "idle"
    assert len(store.fields_of(conn, phone.number)) == 1


def test_the_help_text_mentions_plan(phone: Phone) -> None:
    onboard(phone)
    assert "PLAN" in phone("AYUDA")[0]


# --------------------------------------------------------------------------- #
# What each plan unlocks
# --------------------------------------------------------------------------- #


def test_drone_photos_are_not_offered_to_someone_who_did_not_ask_for_them(
        phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    reply = phone("DRON")[0]
    assert "/u/" not in reply
    assert "PLAN" in reply and "2" in reply
    assert phone.state == "idle"


def test_and_the_way_back_is_one_message(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("DRON")
    phone("PLAN")
    phone("2")
    phone("DRON")
    phone("si")
    assert "/u/" in phone("hoy")[0]


# --------------------------------------------------------------------------- #
# The thermal camera's answer
# --------------------------------------------------------------------------- #


def _thermal(settings, chances: list[float], flown_on: str = "2026-09-10") -> None:
    drone = settings.drone_workspace
    (drone / "flights.json").write_text(json.dumps({"flights": {
        "F001-20260910": {"field_id": "F001", "flown_on": flown_on}}}), encoding="utf-8")
    (drone / "out" / "F001-20260910").mkdir(parents=True, exist_ok=True)
    (drone / "out" / "F001-20260910" / "thermal.json").write_text(json.dumps({
        "patches": [{"chance": c, "where": "north-east corner", "area_m2": 1240.0,
                     "above_c": 3.1, "signs": []} for c in chances]}), encoding="utf-8")


def test_a_warm_patch_reaches_the_farmer_as_a_chance_and_a_place(settings, phone: Phone,
                                                                 conn) -> None:
    onboard(phone, plan="3")
    register_field(phone)
    draw(conn, "F001")
    _thermal(settings, [0.62])
    replies = phone("AGUA")
    assert any("62%" in r and "esquina noreste" in r for r in replies)
    assert any("no sabe qu" in r for r in replies)     # the camera can't name it


def test_only_the_strongest_patch_goes_out_by_text(settings, phone: Phone, conn) -> None:
    """Five warm spots in 160 characters help nobody; the rest are in the file."""
    onboard(phone, plan="3")
    register_field(phone)
    draw(conn, "F001")
    _thermal(settings, [0.62, 0.55, 0.48])
    pest = [r for r in phone("AGUA") if "%" in r]
    assert len(pest) == 1
    assert "62%" in pest[0] and "2 manchas" in pest[0]


def test_a_weak_score_is_not_worth_a_text_message(settings, phone: Phone, conn) -> None:
    onboard(phone, plan="3")
    register_field(phone)
    draw(conn, "F001")
    _thermal(settings, [0.2])
    assert not any("%" in r for r in phone("AGUA"))


def test_a_farmer_without_a_thermal_camera_never_hears_about_pests(settings, phone: Phone,
                                                                   conn) -> None:
    onboard(phone, plan="2")
    register_field(phone)
    draw(conn, "F001")
    _thermal(settings, [0.8])
    assert not any("%" in r for r in phone("AGUA"))


def test_the_pest_line_is_bilingual(settings) -> None:
    report = {"patches": [{"chance": 0.7, "where": "south side", "area_m2": 900.0}]}
    assert "on the south side" in status_mod.pest_line(report, "en", "North Field")
    assert "en el lado sur" in status_mod.pest_line(report, "es", "Campo Norte")


def test_no_thermal_report_means_no_line(settings) -> None:
    assert status_mod.pest_line(None, "es", "Campo Norte") is None
    assert status_mod.pest_line({"patches": []}, "es", "Campo Norte") is None


# --------------------------------------------------------------------------- #
# The database
# --------------------------------------------------------------------------- #


def test_a_database_made_before_plans_existed_gains_the_column(tmp_path) -> None:
    """A farm's database is the record of what the farmers said; it is migrated."""
    import sqlite3

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE farmers (phone TEXT PRIMARY KEY, name TEXT, lang TEXT,
            channel TEXT NOT NULL DEFAULT 'sms', state TEXT NOT NULL DEFAULT 'new',
            context TEXT NOT NULL DEFAULT '{}', alerts INTEGER, consent_at TEXT,
            opted_out_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO farmers(phone, name, created_at, updated_at)
            VALUES ('+19565550123', 'Juan', '2026-01-01', '2026-01-01');
    """)
    old.commit()
    old.close()

    conn = store.connect(path)
    farmer = store.get_farmer(conn, "+19565550123")
    assert farmer.name == "Juan"                      # nothing was lost
    assert farmer.plan == "satellite"                 # and the default is the safe one
    farmer.plan = "thermal"
    store.save_farmer(conn, farmer)
    assert store.get_farmer(conn, "+19565550123").plan == "thermal"
    conn.close()


def test_every_plan_has_a_name_in_both_languages() -> None:
    for key in text.PLAN_MENU:
        for lang in text.LANGS:
            assert text.plan_name(key, lang)
