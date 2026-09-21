"""Into the files the two halves read: only what the farmer confirmed, traceable to its text."""

from __future__ import annotations

import json
from datetime import date

from conftest import Phone, draw, onboard, register_field

from dosojos_sat.fields import load_fields
from dosojos_sat.water import read_field_log

from dosojos_sms import export, store


def test_fields_and_log_the_satellite_half_accepts(phone: Phone, conn, settings) -> None:
    onboard(phone)
    register_field(phone)
    phone("NUEVO")
    register_field(phone, "La Loma", last="9/1 3")
    draw(conn, "F001")            # La Loma has no map yet
    phone("REGUE campo norte ayer 3")
    phone("si")
    phone("REGUE campo norte hoy")
    phone("no")                   # a read-back refused leaves nothing behind

    result = export.export(conn, settings)

    assert result.field_ids == ["F001"]
    assert result.skipped == [("F002", "no map yet")]
    fields = load_fields(result.fields_path)
    assert [(f.field_id, f.name, f.crop, f.irrigation, f.water_enters, f.acres_declared)
            for f in fields] == [("F001", "Campo Norte", "grain sorghum", "furrow", "N", 40.0)]
    log = read_field_log(result.log_path)
    assert [(e.field_id, e.day, e.event, e.inches) for e in log] == [
        ("F001", date(2026, 7, 20), "planted", None),
        ("F001", date(2026, 8, 18), "irrigated", 4.0),
        ("F001", date(2026, 9, 11), "irrigated", 3.0),
    ]
    assert all(e.notes.startswith("sms message ") for e in log)


def test_voided_events_and_photos_stay_out(phone: Phone, conn, settings, tmp_path) -> None:
    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    phone("REGUE hoy 2")
    phone("si")
    phone("BORRAR")
    phone("si")
    photo = tmp_path / "leaf.jpg"
    photo.write_bytes(b"jpeg")
    phone(photo=str(photo))
    phone("2")

    result = export.export(conn, settings)

    kinds = [e.event for e in read_field_log(result.log_path)]
    assert kinds == ["planted", "irrigated"]


def test_a_ticket_names_its_photo(phone: Phone, conn, settings, tmp_path) -> None:
    onboard(phone)
    register_field(phone)
    draw(conn, "F001")
    photo = tmp_path / "ticket.jpg"
    photo.write_bytes(b"jpeg")
    phone(photo=str(photo))
    phone("1")
    phone("9/10 4")
    phone("si")
    log = read_field_log(export.export(conn, settings).log_path)
    assert "photo ticket.jpg" in log[-1].notes


def test_nothing_to_export_removes_a_stale_fields_file(conn, settings) -> None:
    stale = settings.sat_workspace / "fields.geojson"
    stale.write_text("{}", encoding="utf-8")
    result = export.export(conn, settings)
    assert not stale.exists() and result.field_ids == []


def test_a_finished_upload_becomes_a_flight(phone: Phone, conn, settings) -> None:
    onboard(phone, plan="2")
    register_field(phone)
    manifest = settings.drone_workspace / "flights.json"
    manifest.write_text(json.dumps({"flights": {"old-001": {"field_id": "F009"}}}),
                        encoding="utf-8")
    phone("DRON")
    phone("si")
    phone("hoy")
    upload = store.uploads(conn)[0]

    flight = export.register_flight(settings, upload, store.get_field(conn, "F001"))

    flights = json.loads(manifest.read_text(encoding="utf-8"))["flights"]
    assert flight == "F001-20260912" and "old-001" in flights
    assert flights[flight]["field_id"] == "F001" and flights[flight]["flown_on"] == "2026-09-12"
    assert "bare soil" in flights[flight]["notes"]
