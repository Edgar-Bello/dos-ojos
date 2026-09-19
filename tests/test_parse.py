"""Reading farmers' texts: every rule on its own, with today pinned to 12 Sep 2026."""

from __future__ import annotations

from datetime import date

import pytest

from dosojos_sms import parse

TODAY = date(2026, 9, 12)   # a Saturday


# ---- words -------------------------------------------------------------------------


def test_normalize_drops_accents_and_punctuation_but_keeps_dates() -> None:
    assert parse.normalize("¡Sí, regué el 9/2!") == "si, regue el 9/2"
    assert parse.normalize("  Caña   de Azúcar ") == "cana de azucar"


@pytest.mark.parametrize("text, expected", [
    ("Sí", (True, "")), ("SI", (True, "")), ("ok", (True, "")), ("yes", (True, "")),
    ("No", (False, "")), ("nel", (False, "")),
    ("no, fue el 3", (False, "fue el 3")), ("si gracias", (True, "gracias")),
    ("no sé", (None, "")), ("tal vez", (None, "")),
])
def test_yes_and_no(text: str, expected: tuple) -> None:
    assert parse.yes_no(text) == expected


@pytest.mark.parametrize("text", ["STOP", "Alto", "alto.", "PARAR", "cancelar", "unsubscribe"])
def test_opt_out_words(text: str) -> None:
    assert parse.is_stop(text)


def test_opt_out_needs_the_word_alone() -> None:
    """A stop word inside a sentence is not an opt-out."""
    assert not parse.is_stop("para el campo norte")
    assert not parse.is_stop("alto el agua")


@pytest.mark.parametrize("text, expected", [
    ("AGUA", "status"), ("Regué hoy 4", "irrigated"), ("Ya regamos ayer", "irrigated"),
    ("le eché agua ayer", "irrigated"), ("LLUVIA 1.2", "rain"), ("cosechamos el lunes", "harvested"),
    ("Sembré sorgo", "planted"), ("nuevo", "new_field"), ("MAPA", "map"), ("dron", "drone"),
    ("borrar", "undo"), ("watered today 4", "irrigated"), ("English", "lang_en"),
    ("No hemos regado", None), ("hola", None), ("¿cuándo riego?", None),
])
def test_commands(text: str, expected: str | None) -> None:
    assert parse.command(text) == expected


# ---- dates -------------------------------------------------------------------------


def test_month_first_date_inside_the_window_is_the_only_reading() -> None:
    """9/2 in September is 2 September; 9 February is too long ago for an irrigation."""
    found = parse.find_date("9/2", TODAY, window="irrigated")
    assert found.day == date(2026, 9, 2) and not found.options


def test_a_date_that_could_be_two_comes_back_as_both() -> None:
    """A US 3/4 is 4 March, a Mexican 3/4 is 3 April: ask, never guess."""
    found = parse.find_date("3/4", TODAY, window="planted")
    assert found.day is None
    assert found.options == (date(2026, 3, 4), date(2026, 4, 3))


@pytest.mark.parametrize("text, expected", [
    ("15 de marzo", date(2026, 3, 15)), ("15 marzo", date(2026, 3, 15)),
    ("march 15", date(2026, 3, 15)), ("Mar 15, 2026", date(2026, 3, 15)),
    ("2 sep", date(2026, 9, 2)), ("2026-08-18", date(2026, 8, 18)),
    ("12/20/25", date(2025, 12, 20)), ("12/20", date(2025, 12, 20)),
])
def test_written_dates(text: str, expected: date) -> None:
    assert parse.find_date(text, TODAY, window=400).day == expected


@pytest.mark.parametrize("text, expected", [
    ("hoy", TODAY), ("ayer", date(2026, 9, 11)), ("antier", date(2026, 9, 10)),
    ("antes de ayer", date(2026, 9, 10)), ("hace 3 días", date(2026, 9, 9)),
    ("hace tres dias", date(2026, 9, 9)), ("3 days ago", date(2026, 9, 9)),
    ("yesterday", date(2026, 9, 11)), ("hace una semana", date(2026, 9, 5)),
    ("el lunes", date(2026, 9, 7)), ("el sábado", TODAY), ("el sabado pasado", date(2026, 9, 5)),
    ("last friday", date(2026, 9, 11)), ("el 5", date(2026, 9, 5)), ("el 20", date(2026, 8, 20)),
])
def test_relative_dates(text: str, expected: date) -> None:
    assert parse.find_date(text, TODAY, window=400).day == expected


def test_a_day_still_to_come_is_a_problem() -> None:
    found = parse.find_date("9/20", TODAY, window="irrigated")
    assert found.day is None and found.problem == "future" and found.near == date(2026, 9, 20)


def test_a_day_too_long_ago_is_a_problem() -> None:
    found = parse.find_date("12/25/2020", TODAY, window="irrigated")
    assert found.problem == "old" and found.near == date(2020, 12, 25)


def test_last_week_is_too_vague() -> None:
    assert parse.find_date("la semana pasada", TODAY).problem == "vague"


def test_no_date_at_all() -> None:
    found = parse.find_date("4 pulgadas", TODAY)
    assert found.day is None and found.problem is None and not found.options


# ---- amounts -----------------------------------------------------------------------


@pytest.mark.parametrize("text, inches", [
    ("4 pulgadas", 4.0), ("4 pulg", 4.0), ("3.5 in", 3.5), ("4,5 pulgadas", 4.5),
    ("3 1/2", 3.5), ("media pulgada", 0.5), ("una pulgada y media", 1.5),
    ("cuatro pulgadas", 4.0), ("4", 4.0),
])
def test_inches(text: str, inches: float) -> None:
    assert parse.find_amount(parse.normalize(text)).inches == pytest.approx(inches)


def test_acre_feet_become_inches_over_the_field() -> None:
    """10 acre-feet spread over 40 acres is 3 inches deep."""
    amount = parse.find_amount(parse.normalize("10 af"), 40.0)
    assert amount.inches == pytest.approx(3.0) and amount.per_acre == "af"
    assert parse.find_amount("20 acre-pies", 40.0).inches == pytest.approx(6.0)


def test_acre_feet_without_acres_are_kept_as_written() -> None:
    amount = parse.find_amount("10 af", None)
    assert amount.inches is None and amount.raw == "10 af"


def test_hours_of_water_are_not_inches() -> None:
    amount = parse.find_amount("6 horas")
    assert amount.hours == 6 and amount.inches is None


def test_the_day_is_taken_out_before_the_amount() -> None:
    norm = parse.normalize("9/2 4")
    found = parse.find_date(norm, TODAY, window="irrigated")
    assert parse.find_amount(parse.without(norm, found.span)).inches == 4.0


# ---- crops, methods, sides, acres ----------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("1", ("sorghum", None)), ("sorgo", ("sorghum", None)), ("Milo", ("sorghum", None)),
    ("caña de azúcar", ("sugarcane", None)), ("toronjas", ("citrus", None)),
    ("algodón", ("cotton", None)), ("8", ("none", None)), ("nada ahorita", ("none", None)),
    ("chile", ("other", "chile")), ("7", ("other", None)), ("otro: cebolla", ("other", "cebolla")),
    ("no sé", (None, None)),
])
def test_crops(text: str, expected: tuple) -> None:
    assert parse.crop(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("1", "furrow"), ("surcos", "furrow"), ("melgas", "border"), ("inundación", "flood"),
    ("goteo", "drip"), ("cinta", "drip"), ("aspersión", "sprinkler"), ("pivote", "pivot"),
    ("6", "none"), ("temporal", "none"), ("no se riega", "none"), ("qué?", None),
])
def test_methods(text: str, expected: str | None) -> None:
    assert parse.method(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("N", "N"), ("norte", "N"), ("del lado sur", "S"), ("O", "W"), ("oeste", "W"),
    ("poniente", "W"), ("east", "E"), ("no sé", "?"), ("norte o sur", None), ("el canal", None),
])
def test_sides(text: str, expected: str | None) -> None:
    assert parse.side(text) == expected


def test_acres() -> None:
    assert parse.acres("40") == 40.0
    assert parse.acres("como 38.5 acres") == 38.5
    assert parse.acres("no sé") == "?"
    assert parse.acres("muchos") is None


def test_names_lose_their_preamble() -> None:
    assert parse.name("Me llamo Juan Pérez") == "Juan Pérez"
    assert parse.name("  'La Loma'. ") == "La Loma"
    assert parse.name("1234") is None


# ---- places ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "26.1484, -97.9940",
    "26.1484 -97.994",
    "https://www.google.com/maps/search/26.148400,+-97.994000?entry=tts",
    "https://www.google.com/maps/place/Field/@26.1484,-97.994,17z/data=!3m1!4b1",
    "https://www.google.com/maps/place/x/data=!4m6!3m5!1s0x0:0x0!8m2!3d26.1484!4d-97.994",
    "https://maps.google.com/?q=26.1484,-97.994",
    "https://maps.apple.com/?ll=26.1484,-97.994&q=Dropped%20Pin",
    "Mi campo: geo:26.1484,-97.994",
    "26°08'54.2\"N 97°59'38.4\"W",
])
def test_places_from_links_and_numbers(text: str) -> None:
    place = parse.find_place(text)
    assert isinstance(place, parse.Place)
    assert place.lat == pytest.approx(26.1484, abs=1e-3)
    assert place.lon == pytest.approx(-97.994, abs=1e-3)
    assert place.in_conus


def test_a_longitude_without_its_minus_is_put_west() -> None:
    place = parse.find_place("26.1484, 97.9940")
    assert place.lon == pytest.approx(-97.994) and place.fixed_sign


def test_swapped_coordinates_are_put_right() -> None:
    place = parse.find_place("-97.9940, 26.1484")
    assert place.lat == pytest.approx(26.1484) and place.lon == pytest.approx(-97.994)


def test_a_whatsapp_location_comes_as_numbers() -> None:
    place = parse.find_place("", lat=26.1484, lon=-97.994)
    assert place.lat == 26.1484


def test_a_short_link_is_followed() -> None:
    final = "https://www.google.com/maps/search/26.148400,+-97.994000?entry=tts"
    place = parse.find_place("https://maps.app.goo.gl/AbC123", resolve=lambda url: [final])
    assert isinstance(place, parse.Place) and place.lat == pytest.approx(26.1484)


def test_a_short_link_that_will_not_open_asks_for_the_numbers() -> None:
    def broken(url: str):
        raise OSError("no network")

    assert parse.find_place("https://maps.app.goo.gl/AbC123", resolve=broken) == "unresolved"


def test_mexico_is_outside_the_weather_grid() -> None:
    place = parse.find_place("19.4326, -99.1332")
    assert isinstance(place, parse.Place) and not place.in_conus


def test_words_are_not_a_place() -> None:
    assert parse.find_place("por la carretera 107 y milla 10") is None


def test_a_field_name_taken_out_before_the_date_leaves_the_amount_alone() -> None:
    """'watered Campo Norte 9/11 3' was once read as 11 inches: the name's gap
    shifted the date's position, and the day of the month was left behind."""
    norm = parse.without(parse.normalize("watered Campo Norte 9/11 3"), (8, 19))
    found = parse.find_date(norm, date(2026, 9, 12), window="irrigated")
    assert found.day == date(2026, 9, 11)
    assert parse.find_amount(parse.without(norm, found.span)).inches == 3.0
