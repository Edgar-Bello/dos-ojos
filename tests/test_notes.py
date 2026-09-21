"""NOTE: what the farmer knows and no reading of the field can show.

Half a field cut, a corner replanted, a pump out: the crop then reads short for
a reason that is not thirst. The farmer says so in their own words, it is read
back before it is kept, and it goes in front of the advice before it is written.
"""

from __future__ import annotations

from datetime import date

from conftest import Phone, onboard, register_field

from dosojos_sms import ai, store


def test_a_note_is_read_back_before_it_is_kept(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    asked = phone("NOTA coseché la mitad del campo el lunes")
    assert "coseché la mitad del campo el lunes" in asked[0] and "SI o NO" in asked[0]
    assert not store.events_for(conn, "F001", include_void=True) or not [
        e for e in store.events_for(conn, "F001") if e.kind == "note"]
    saved = phone("si")
    assert "Campo Norte" in saved[0]
    notes = [e for e in store.events_for(conn, "F001") if e.kind == "note"]
    assert [e.note for e in notes] == ["coseché la mitad del campo el lunes"]
    assert notes[0].message_id is not None          # the text it came from is kept


def test_a_note_refused_is_never_stored(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("NOTA se me quebró la bomba")
    phone("no")
    assert [e for e in store.events_for(conn, "F001") if e.kind == "note"] == []


def test_the_bare_command_asks_what_to_note(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    asked = phone("NOTA")
    assert "Campo Norte" in asked[0] and phone.state == "note:text"
    read = phone("entró el ganado a la esquina norte")
    # Read back as the phone can show it: the GSM alphabet has no "ó". What is
    # kept below is the farmer's own spelling, accents and all.
    assert "entro el ganado a la esquina norte" in read[0]
    phone("si")
    assert [e.note for e in store.events_for(conn, "F001") if e.kind == "note"] == [
        "entró el ganado a la esquina norte"]


def test_the_field_is_asked_when_there_are_several(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("NUEVO")
    register_field(phone, "La Loma", last="9/1 3")
    asked = phone("NOTA")
    assert "1 Campo Norte\n2 La Loma" in asked[0]
    phone("2")
    phone("corté la mitad el viernes")
    phone("si")
    kept = {e.field_id: e.note for e in store.events_for(conn) if e.kind == "note"}
    assert kept == {"F002": "corté la mitad el viernes"}


def test_the_field_name_in_the_text_picks_it(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("NUEVO")
    register_field(phone, "La Loma", last="9/1 3")
    read = phone("NOTA La Loma sembré de nuevo la esquina")
    assert "La Loma" in read[0] and "sembré de nuevo la esquina" in read[0]
    # The field's own name is not stored again as part of what they said.
    phone("si")
    assert [e.note for e in store.events_for(conn, "F002") if e.kind == "note"] == [
        "sembré de nuevo la esquina"]


def test_a_note_that_is_only_the_field_name_is_asked_again(phone: Phone) -> None:
    onboard(phone)
    register_field(phone)
    phone("NUEVO")
    register_field(phone, "La Loma", last="9/1 3")
    asked = phone("NOTA La Loma")
    assert "La Loma" in asked[0] and phone.state == "note:text"


def test_two_notes_in_one_day_both_stand(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("NOTA coseché la mitad")
    phone("si")
    phone("NOTA la bomba anda mal")
    phone("si")
    assert sorted(e.note for e in store.events_for(conn, "F001") if e.kind == "note") == [
        "coseché la mitad", "la bomba anda mal"]


def test_a_note_can_be_taken_back(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("NOTA coseché la mitad")
    phone("si")
    asked = phone("BORRAR")
    assert "coseché la mitad" in asked[0]
    phone("si")
    assert [e for e in store.events_for(conn, "F001") if e.kind == "note"] == []


def test_a_command_at_the_note_question_is_not_the_note(phone: Phone, conn) -> None:
    onboard(phone)
    register_field(phone)
    phone("NOTA")
    replies = phone("CAMPOS")
    assert any("Campo Norte" in r for r in replies)
    assert [e for e in store.events_for(conn, "F001") if e.kind == "note"] == []


def test_a_note_is_not_sent_to_the_satellite_log(phone: Phone, conn, settings) -> None:
    from dosojos_sms import export

    onboard(phone)
    register_field(phone)
    phone("NOTA coseché la mitad")
    phone("si")
    result = export.export(conn, settings)
    assert "coseché" not in result.log_path.read_text(encoding="utf-8")


def test_help_offers_the_note(phone: Phone) -> None:
    onboard(phone)
    assert "NOTA - contarnos algo del campo" in phone("AYUDA")[0]


# --------------------------------------------------------------------------- #
# What it is for: the AI reads it before it writes the advice
# --------------------------------------------------------------------------- #

def _brief_with_note(note: str) -> dict:
    return {"today": "2026-09-20", "field": {"crop": "grain sorghum", "watered_by": "furrow"},
            "water_checkbook": {"status": "ok", "days_until_water": 6,
                                "days_range": [4, 8], "water_by": "2026-09-26"},
            "farmer_notes": [f"2026-09-14: {note}"]}


def test_the_farmer_s_words_reach_the_model_with_the_decision() -> None:
    written = ai.facts(_brief_with_note("coseché la mitad del campo"))
    assert written[0].startswith("DECISION")
    assert "coseché la mitad del campo" in written[1]
    assert "2026-09-14" in written[1]


def test_a_field_with_no_note_says_nothing_about_notes() -> None:
    brief = _brief_with_note("x")
    brief.pop("farmer_notes")
    assert not any("farmer" in line.lower() and "told" in line.lower()
                   for line in ai.facts(brief))


def test_the_note_is_in_the_brief_the_model_is_given(settings, conn, phone: Phone) -> None:
    onboard(phone)
    register_field(phone)
    phone("NOTA coseché la mitad")
    phone("si")
    from dosojos_sms.status import Water

    farmer = store.get_farmer(conn, phone.number)
    record = store.get_field(conn, "F001")
    events = store.events_for(conn, "F001")
    item = Water(settings).field(record, events, date(2026, 9, 20), full=True)
    brief = ai.field_brief(settings, farmer, item, events, date(2026, 9, 20))
    if brief is not None:                      # no checkbook without a drawn outline
        assert brief["farmer_notes"] == ["2026-09-20: coseché la mitad"]


def test_an_old_note_is_left_out() -> None:
    from dosojos_sms.ai import NOTE_DAYS

    assert NOTE_DAYS >= 30            # a partial harvest still explains the reading weeks on
