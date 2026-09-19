"""Each field its own plan; a drone field answers once, with everything in; commands mid-question."""

from __future__ import annotations

import json

from conftest import Phone, draw, onboard, register_field

from dosojos_sms import store


def test_each_new_field_is_asked_its_own_plan(phone: Phone, conn) -> None:
    onboard(phone, plan="1")
    register_field(phone)
    phone("NUEVO")
    asked = phone("Huerta")
    assert "Huerta" in asked[0] and "1 S" in asked[0] and phone.state == "f:plan"
    said = phone("3")
    assert "Huerta" in said[0] and "térmica" in said[0]
    assert any("licencia" in r for r in said)          # the first field with a drone
    plans = {f.name: f.plan for f in store.fields_of(conn, phone.number)}
    assert plans == {"Campo Norte": "satellite", "Huerta": "thermal"}


def test_a_drone_field_is_reminded_of_its_photos_when_done(phone: Phone) -> None:
    onboard(phone, plan="2")
    phone("Campo Norte")
    phone("40")
    phone("26.1484, -97.9940")
    phone("1")
    phone("7/20")
    phone("si")
    phone("2")
    phone("1")
    phone("N")
    phone("8/18 4")
    phone("si")
    done = phone("no")
    assert any("DRON" in r and "fotos del dron de Campo Norte" in r for r in done)


def test_water_waits_for_the_drone_photos(phone: Phone, conn) -> None:
    onboard(phone, plan="1")
    register_field(phone)
    phone("NUEVO")
    register_field(phone, "La Loma", plan="2", last="9/1 3")
    draw(conn, "F001")
    draw(conn, "F002")
    replies = phone("AGUA")
    assert any(r.startswith("La Loma: la respuesta del agua llega junto con sus fotos")
               for r in replies)
    assert any(r.startswith("Campo Norte") and "fotos" not in r for r in replies)


def test_water_says_the_photos_are_being_processed(phone: Phone, conn) -> None:
    onboard(phone, plan="2")
    register_field(phone)
    draw(conn, "F001")
    record = store.get_field(conn, "F001")
    record.answers["flight"] = "working"
    store.save_field(conn, record)
    assert any("todavia estoy procesando" in r for r in phone("AGUA"))
    assert any("todavia estoy procesando" in r for r in phone("PORQUE"))


def test_drone_for_a_satellite_only_field_says_how_to_add_it(phone: Phone, conn) -> None:
    onboard(phone, plan="2")
    register_field(phone)
    phone("NUEVO")
    register_field(phone, "La Loma", plan="1", last="9/1 3")
    phone("DRON la loma")
    phone("no")
    reply = phone("hoy")
    assert "La Loma esta en solo satélite" in reply[0]
    assert not store.uploads(conn)


def test_map_during_the_crop_question_is_the_map_not_a_crop(phone: Phone, conn) -> None:
    onboard(phone)
    phone("Campo Norte")
    phone("40")
    phone("26.1484, -97.9940")
    assert phone.state == "f:crop"
    replies = phone("MAPA")
    assert "/f/" in replies[0]
    assert store.get_field(conn, "F001").crop is None
    assert any("Que tiene sembrado" in r or "Qué tiene sembrado" in r for r in replies)
    assert phone.state == "f:crop"


def test_crop_changes_a_fields_crop(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    assert store.get_field(conn, "F001").crop == "sorghum"
    asked = phone("CULTIVO")
    assert "Campo Norte" in asked[0] and phone.state == "f:crop"
    saved = phone("5")
    assert "Campo Norte tiene" in saved[0]
    record = store.get_field(conn, "F001")
    assert record.crop == "citrus" and "maturity" not in record.answers
    assert phone.state == "idle"


def test_crop_with_two_fields_asks_which(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("NUEVO")
    register_field(phone, "La Loma", last="9/1 3")
    assert "1 Campo Norte\n2 La Loma" in phone("CROP")[0]
    phone("2")
    phone("3")
    assert store.get_field(conn, "F002").crop == "corn"
    assert store.get_field(conn, "F001").crop == "sorghum"
