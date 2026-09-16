"""Whole conversations: what the bot asks, what it stores, and that it reads back first."""

from __future__ import annotations

from datetime import date

from conftest import (NOW, PHONE, FakeStatus, FakeWater, Phone, draw, onboard,
                      register_field, resolver_to)

from dosojos_sms import store, text
from dosojos_sms.bot import Bot


def events(conn, field_id: str = "F001", kind: str | None = None):
    return [e for e in store.events_for(conn, field_id) if kind is None or e.kind == kind]


def test_a_new_number_gets_the_bilingual_welcome(phone: Phone) -> None:
    replies = phone("Hola")
    assert replies == [text.gsm_safe(text.WELCOME)]
    assert phone.state == "lang"


def test_the_whole_first_field_in_spanish(phone: Phone, conn) -> None:
    onboard(phone)
    farmer = phone.farmer
    assert farmer.lang == "es" and farmer.name == "Juan Ejemplo" and farmer.alerts is True
    assert farmer.consent_at
    assert phone.state == "field_name"

    assert "acres" in phone("Campo Norte")[0]
    assert "Dónde".replace("ó", "o") in phone("40")[0]
    replies = phone("26.1484, -97.9940")
    assert "/f/" in replies[0] and "sembrado" in replies[1]
    assert "sembró".replace("ó", "o") in phone("1")[0]
    readback = phone("7/20")
    assert readback == ["Anoté: sorgo sembrado en Campo Norte el lun 20 jul. ¿Correcto? SI o NO"]
    assert events(conn) == []                       # nothing stored before the yes
    assert "riega" in phone("si")[0]
    assert "lado" in phone("1")[0]
    assert "último".replace("ú", "u") in phone("N")[0]
    readback = phone("8/18 4")
    assert "mar 18 ago, 4 pulgadas" in readback[0]
    assert "otro riego" in phone("si")[0]
    done = phone("no")
    assert "quedo registrado" in done[0] and "/f/" in done[1]
    assert phone.state == "idle"

    record = store.get_field(conn, "F001")
    assert (record.name, record.acres_said, record.crop, record.irrigation, record.water_enters) \
        == ("Campo Norte", 40.0, "sorghum", "furrow", "N")
    assert (record.lat, record.lon) == (26.1484, -97.994)
    planted, watered = events(conn, kind="planted"), events(conn, kind="irrigated")
    assert [(e.day, e.inches) for e in planted] == [(date(2026, 7, 20), None)]
    assert [(e.day, e.inches) for e in watered] == [(date(2026, 8, 18), 4.0)]
    assert all(e.message_id for e in planted + watered)   # traceable to the text


def test_english_too(phone: Phone, conn) -> None:
    onboard(phone, lang="2")
    assert phone.farmer.lang == "en"
    phone("South 40")
    phone("not sure")
    replies = phone("26.1484, -97.9940")
    assert replies[1].startswith("What's growing in South 40?")
    assert phone("cotton")[0].startswith("What day was the cotton planted in South 40?")
    record = store.get_field(conn, "F001")
    assert record.answers.get("acres") == "unknown" and record.crop == "cotton"


def test_every_reply_fits_gsm(phone: Phone) -> None:
    onboard(phone)
    replies = []
    for body in ("Campo Norte", "40", "26.1484, -97.9940", "1", "3/4"):
        replies += phone(body)
    assert all(all(c in text._GSM for c in r) for r in replies)


def test_a_date_that_could_be_two_is_asked(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone, planted="", method="", side="", last="")
    # register_field stopped at the planting question
    assert phone.state == "a:day"
    pick = phone("3/4")
    assert pick == ["¿Qué fecha es? 1) mié 4 mar 2) vie 3 abr. Responda 1 o 2."]
    readback = phone("2")
    assert "vie 3 abr" in readback[0]
    phone("si")
    assert events(conn, kind="planted")[0].day == date(2026, 4, 3)


def test_no_to_a_read_back_asks_again(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone, planted="", method="", side="", last="")
    phone("7/20")
    again = phone("no")
    assert "sembró".replace("ó", "o") in again[0]
    phone("7/22")
    phone("si")
    assert [e.day for e in events(conn, kind="planted")] == [date(2026, 7, 22)]


def test_a_correction_instead_of_a_yes_keeps_the_rest(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone, last="")
    phone("8/18 4")
    corrected = phone("no, fue el 17")
    assert "lun 17 ago, 4 pulgadas" in corrected[0]
    phone("si")
    phone("no")
    assert [(e.day, e.inches) for e in events(conn, kind="irrigated")] == [(date(2026, 8, 17), 4.0)]


def test_a_date_to_come_is_refused(phone: Phone) -> None:
    onboard(phone)
    register_field(phone, last="")
    reply = phone("9/20 4")
    assert "todavia no llega" in reply[0]
    assert phone.state == "a:day"


def test_an_implausible_amount_is_asked_again(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone, last="")
    reply = phone("8/18 40")
    assert "mucha agua" in reply[0]
    readback = phone("4")
    assert "4 pulgadas" in readback[0]


def test_acre_feet_are_read_back_as_inches(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone, last="")
    readback = phone("8/18 10 af")
    assert "10 af = 3 pulgadas en 40 acres" in readback[0]
    phone("si")
    assert events(conn, kind="irrigated")[0].inches == 3.0


def test_rainfed_skips_the_water_side_and_the_last_irrigation(phone: Phone, conn) -> None:
    onboard(phone)
    phone("La Loma")
    phone("20")
    phone("26.15, -97.99")
    phone("1")
    phone("5/15")
    phone("si")
    done = phone("6")
    assert "quedo registrado" in done[0]
    assert store.get_field(conn, "F001").irrigation == "none"


def test_citrus_has_no_planting_date(phone: Phone) -> None:
    onboard(phone)
    phone("Huerta")
    phone("15")
    phone("26.15, -97.99")
    assert "riega" in phone("5")[0]


def test_cane_asks_for_the_last_cut(phone: Phone, conn) -> None:
    onboard(phone)
    phone("Caña")
    phone("60")
    phone("26.15, -97.99")
    assert "cortó".replace("ó", "o") in phone("4")[0]
    phone("1/20")
    phone("si")
    assert events(conn, kind="planted")[0].note == "planted or last cut"


def test_a_place_in_words_goes_to_the_team(phone: Phone, conn) -> None:
    onboard(phone)
    phone("El Rancho")
    phone("40")
    reply = phone("por la carretera 107 y la milla 10 norte")
    assert "equipo" in reply[0]
    record = store.get_field(conn, "F001")
    assert record.place.startswith("por la carretera") and record.lat is None
    assert store.messages(conn, PHONE, unread_only=True)


def test_a_short_link_that_opens(conn, settings, water) -> None:
    bot = Bot(conn, settings, now=NOW, water=water, resolve=resolver_to(
        "https://www.google.com/maps/search/26.148400,+-97.994000?entry=tts"))
    phone = Phone(bot)
    onboard(phone)
    phone("Campo Norte")
    phone("40")
    reply = phone("https://maps.app.goo.gl/AbC123")
    assert "26.14840, -97.99400" in reply[0]


def test_a_short_link_that_will_not_open(phone: Phone) -> None:
    onboard(phone)
    phone("Campo Norte")
    phone("40")
    reply = phone("https://maps.app.goo.gl/AbC123")
    assert "No pude abrir" in reply[0] and phone.state == "f:location"


def test_mexico_is_refused(phone: Phone) -> None:
    onboard(phone)
    phone("Campo Norte")
    phone("40")
    assert "no esta en Estados Unidos" in phone("19.4326, -99.1332")[0]


# ---- once set up ------------------------------------------------------------------


def two_fields(phone: Phone, conn, *, plan: str = "1") -> None:
    onboard(phone, plan=plan)
    register_field(phone)
    phone("NUEVO")
    register_field(phone, "La Loma", last="9/1 3")
    draw(conn, "F001")
    draw(conn, "F002")


def test_watered_with_two_fields_asks_which(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    pick = phone("REGUE 4")
    assert pick == ["¿Cual campo? 1 Campo Norte, 2 La Loma, 3 Todos"]
    readback = phone("1")
    assert readback == ["Anoté: riego en Campo Norte el sab 12 sep (hoy), 4 pulgadas. ¿Correcto? "
                        "Responda SI o NO."]
    saved = phone("si")
    assert saved[0] == "Anotado." and "5 dias" in saved[1]
    assert [(e.day, e.inches) for e in events(conn, "F001", "irrigated")][-1] == (NOW.date(), 4.0)


def test_watered_naming_the_field_and_the_day(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    readback = phone("regamos la loma ayer como 3 pulgadas")
    assert "La Loma el vie 11 sep (ayer), 3 pulgadas" in readback[0]
    phone("si")
    assert events(conn, "F002", "irrigated")[-1].day == date(2026, 9, 11)


def test_watered_all_fields(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    phone("REGUE todos ayer")
    phone("si")
    assert events(conn, "F001", "irrigated")[-1].day == date(2026, 9, 11)
    assert events(conn, "F002", "irrigated")[-1].day == date(2026, 9, 11)


def test_rain_goes_on_every_field(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    readback = phone("LLUVIA 1.2")
    assert "1.2 pulgadas" in readback[0] and "Campo Norte y La Loma" in readback[0]
    phone("si")
    assert events(conn, "F002", "rain")[0].inches == 1.2


def test_rain_without_inches_asks_for_them(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    assert "Cuantas pulgadas llovio" in phone("llovió")[0]
    assert "0.5 pulgadas" in phone("media pulgada")[0]


def test_no_to_a_rain_read_back_asks_the_day_and_inches(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    phone("LLUVIA 1.2")
    assert phone("no") == ["¿Qué dia llovio y cuantas pulgadas? (ej. ayer 1.2)"]
    assert "lluvia de 1.5 pulgadas el vie 11 sep (ayer)" in phone("ayer 1.5")[0]


def test_harvest_suggests_a_bare_soil_flight(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    phone("COSECHA campo norte")
    saved = phone("si")
    assert "dron" in saved[1].lower() and events(conn, "F001", "harvested")


def test_planting_a_new_crop(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    phone("COSECHA campo norte ayer")
    phone("si")
    readback = phone("SEMBRE algodón en campo norte")
    assert "algodon sembrado en Campo Norte el sab 12 sep (hoy)" in readback[0]
    phone("si")
    record = store.get_field(conn, "F001")
    assert record.crop == "cotton" and events(conn, "F001", "planted")[-1].day == NOW.date()


def test_undo_voids_the_last_entry(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    phone("REGUE la loma 4")
    phone("si")
    assert "Quito esto: riego La Loma" in phone("BORRAR")[0]
    assert phone("si") == ["Quitado."]
    assert [e.day for e in events(conn, "F002", "irrigated")] == [date(2026, 9, 1)]


def test_status_answers_per_field(phone: Phone, conn, water: FakeWater) -> None:
    two_fields(phone, conn)
    replies = phone("AGUA")
    assert len(replies) == 3                    # a field each, then the offer of the why file
    assert replies[0].startswith("Campo Norte (sorgo): tiene agua para unos 5 dias (4 a 7)")
    assert "5.3 pulgadas por surcos" in replies[0]
    assert "PORQUE" in replies[-1]


def test_the_ground_report_follows_when_a_watering_is_near(phone: Phone, conn, settings,
                                                          water: FakeWater) -> None:
    import json

    two_fields(phone, conn)
    drone = settings.drone_workspace
    (drone / "flights.json").write_text(json.dumps({"flights": {
        "F001-20260910": {"field_id": "F001", "flown_on": "2026-09-10"}}}), encoding="utf-8")
    (drone / "out" / "F001-20260910").mkdir(parents=True)
    (drone / "out" / "F001-20260910" / "terrain.json").write_text(json.dumps({"advice": [
        {"topic": "high spot", "priority": 1, "advice": "...",
         "finding": "High spot H1 in the west side: 112 m2 standing up to 7 cm"}]}),
        encoding="utf-8")

    replies = phone("AGUA")
    assert replies[1].startswith("Terreno: Hay una parte alta en el lado oeste")
    assert len(replies) == 4                    # La Loma has no flight; then the offer

    water.status = FakeStatus(days_left=20, days_range=[16, 25], water_by="2026-10-02")
    assert len(phone("AGUA")) == 3              # nothing to change before a far-off watering

    water.status = FakeStatus(method="none")
    assert len(phone("AGUA")) == 3              # rainfed: no watering to change


def test_status_when_a_map_is_missing(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    reply = phone("AGUA")
    assert "falta marcar el mapa" in reply[0] and "/f/" in reply[0]


def test_status_mid_question_keeps_the_question(phone: Phone, conn) -> None:
    onboard(phone)
    phone("Campo Norte")
    replies = phone("AGUA")
    assert replies[-1].startswith("¿Cuantos acres")
    assert phone.state == "f:acres"


def test_an_action_mid_setup_resumes_the_field_after(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("NUEVO")
    phone("La Loma")
    phone("REGUE campo norte ayer 3")
    replies = phone("si")
    assert "Sigamos con La Loma." in replies and phone.state == "f:acres"


def test_help_mid_question_asks_the_question_again(phone: Phone) -> None:
    onboard(phone)
    phone("Campo Norte")
    replies = phone("AYUDA")
    assert replies[0].startswith("Dos Ojos. Mande:") and replies[1].startswith("¿Cuantos acres")


def test_switching_language(phone: Phone) -> None:
    onboard(phone)
    replies = phone("English")
    assert replies[0] == "OK, English it is." and "first field" in replies[1]


def test_stop_means_silence_until_start(phone: Phone) -> None:
    onboard(phone)
    assert "no le mandaremos" in phone("STOP")[0]
    assert phone("Campo Norte") == []
    assert phone.farmer.opted_out_at
    replies = phone("START")
    assert "Bienvenido de vuelta" in replies[0] and replies[1].startswith("Vamos a registrar")
    assert not phone.farmer.opted_out_at


def test_an_unknown_text_goes_to_the_team(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    assert "se lo pasé al equipo" in phone("¿Y si le echo abono?")[0]
    assert store.messages(conn, PHONE, unread_only=True)


def test_thanks_needs_no_answer(phone: Phone, conn) -> None:
    two_fields(phone, conn)
    assert phone("gracias") == []


def test_a_ticket_photo(phone: Phone, conn, tmp_path) -> None:
    two_fields(phone, conn)
    photo = tmp_path / "ticket.jpg"
    photo.write_bytes(b"jpeg")
    assert "Ticket de agua" in phone(photo=str(photo))[0]
    phone("1")
    assert "Gracias por el ticket" in phone("2")[0]
    readback = phone("9/10 4")
    assert "La Loma el jue 10 sep, 4 pulgadas" in readback[0]
    phone("si")
    event = events(conn, "F002", "irrigated")[-1]
    assert event.source == "ticket" and event.evidence == str(photo)


def test_a_ticket_photo_while_asked_for_the_last_irrigation(phone: Phone, conn, tmp_path) -> None:
    onboard(phone)
    register_field(phone, last="")
    photo = tmp_path / "ticket.jpg"
    photo.write_bytes(b"jpeg")
    assert "Gracias por el ticket" in phone(photo=str(photo))[0]
    phone("8/18 4")
    phone("si")
    assert events(conn, kind="irrigated")[0].evidence == str(photo)


def test_a_problem_photo(phone: Phone, conn, tmp_path) -> None:
    two_fields(phone, conn)
    photo = tmp_path / "leaf.jpg"
    photo.write_bytes(b"jpeg")
    phone(photo=str(photo))
    phone("2")
    assert phone("1") == ["Gracias, se la pasamos al equipo."]
    assert events(conn, "F001", "photo")[0].evidence == str(photo)


def test_drone_photos_get_an_upload_link(phone: Phone, conn) -> None:
    two_fields(phone, conn, plan="2")
    phone("DRON")
    assert "sin cultivo" in phone("1")[0]
    phone("si")
    reply = phone("hoy")
    assert "/u/" in reply[0] and "14 dias" in reply[0]
    upload = store.uploads(conn)[0]
    assert upload["flight_id"] == "F001-20260912" and upload["bare_soil"] == 1


def test_the_map_link_is_reused(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    first = phone("MAPA")[0].split()[-1]
    second = phone("MAPA")[0].split()[-1]
    assert first == second and "/f/" in first


def test_the_checkin(bot: Bot, phone: Phone, conn) -> None:
    two_fields(phone, conn)
    bot.checkin(phone.farmer, "F001")
    assert "ya necesita agua" in phone("no")[0]
    bot.checkin(phone.farmer, "F001")
    readback = phone("si, ayer 4")
    assert "Campo Norte el vie 11 sep (ayer), 4 pulgadas" in readback[0]


def test_a_reminder_puts_the_question(bot: Bot, phone: Phone, conn) -> None:
    onboard(phone)
    phone("Campo Norte")
    phone("MENU")
    assert phone.state == "idle"
    preview = bot.ask(phone.farmer, "crop", "F001", save=False)
    assert "tiene sembrado en Campo Norte" in preview and phone.state == "idle"
    prompt = bot.ask(phone.farmer, "crop", "F001")
    assert "tiene sembrado en Campo Norte" in prompt and phone.state == "f:crop"


def test_no_checkbook_yet(conn, settings) -> None:
    bot = Bot(conn, settings, now=NOW, water=FakeWater(reason="no_data"),
              resolve=resolver_to("x"))
    phone = Phone(bot)
    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    assert "todavia no tenemos los datos" in phone("AGUA")[0]


def test_water_now_with_the_stage(conn, settings) -> None:
    status = FakeStatus(days_left=0, days_range=None, water_by="2026-09-12",
                        status="water now",
                        sensitive="grain sorghum reaches boot to flowering now (day 54)")
    bot = Bot(conn, settings, now=NOW, water=FakeWater(status), resolve=resolver_to("x"))
    phone = Phone(bot)
    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    reply = phone("AGUA")[0]
    assert reply.startswith("Campo Norte (sorgo): REGAR YA. Ponga unas 5.3 pulgadas por surcos.")
    assert "Esta en embuche y floracion" in reply
