"""The record: ids, events that correct each other, duplicates and expiring links."""

from __future__ import annotations

from datetime import date

import pytest

from dosojos_sms import store


@pytest.mark.parametrize("raw", ["(956) 555-0123", "956-555-0123", "9565550123",
                                 "+1 956 555 0123", "19565550123", "whatsapp:+19565550123"])
def test_phone_numbers_become_e164(raw: str) -> None:
    assert store.normalize_phone(raw) == "+19565550123"


def test_a_phone_number_needs_digits() -> None:
    with pytest.raises(ValueError):
        store.normalize_phone("hola")


def test_field_ids_count_up(conn) -> None:
    store.add_farmer(conn, "+19565550123")
    assert store.add_field(conn, "+19565550123", "A").id == "F001"
    assert store.add_field(conn, "+19565550123", "B").id == "F002"


def test_a_second_report_of_the_same_day_replaces_the_first(conn) -> None:
    store.add_farmer(conn, "+19565550123")
    store.add_field(conn, "+19565550123", "A")
    store.add_event(conn, "F001", date(2026, 9, 1), "irrigated", inches=4)
    store.add_event(conn, "F001", date(2026, 9, 1), "irrigated", inches=3)
    standing = store.events_for(conn, "F001")
    assert [(e.day, e.inches) for e in standing] == [(date(2026, 9, 1), 3)]
    assert len(store.events_for(conn, "F001", include_void=True)) == 2


def test_an_unknown_event_is_refused(conn) -> None:
    with pytest.raises(ValueError):
        store.add_event(conn, "F001", date(2026, 9, 1), "fertilized")


def test_a_redelivered_text_is_logged_once(conn) -> None:
    assert store.log_in(conn, "+19565550123", "hola", provider_id="SM1") is not None
    assert store.log_in(conn, "+19565550123", "hola", provider_id="SM1") is None


def test_links_expire(conn) -> None:
    store.add_farmer(conn, "+19565550123")
    store.add_field(conn, "+19565550123", "A")
    token = store.new_link(conn, "map", "F001", days=14)
    assert store.get_link(conn, token, "map") is not None
    assert store.get_link(conn, token, "upload") is None
    conn.execute("UPDATE links SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert store.get_link(conn, token, "map") is None


def test_farmers_round_trip(conn) -> None:
    farmer = store.add_farmer(conn, "+19565550123", channel="sim")
    farmer.name, farmer.lang, farmer.state = "Juan", "es", "idle"
    farmer.context = {"field": "F001"}
    farmer.alerts = True
    store.save_farmer(conn, farmer)
    loaded = store.get_farmer(conn, "+19565550123")
    assert (loaded.name, loaded.lang, loaded.state, loaded.context, loaded.alerts,
            loaded.channel) == ("Juan", "es", "idle", {"field": "F001"}, True, "sim")
