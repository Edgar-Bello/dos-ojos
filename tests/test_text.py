"""Every message fits the GSM alphabet and stays short, in both languages."""

from __future__ import annotations

import string
from datetime import date

import pytest

from dosojos_sms import text


def test_gsm_safe_keeps_what_gsm_has_and_drops_what_it_lacks() -> None:
    assert text.gsm_safe("¿Cuándo regó? ¡Mañana! Sí, é ü ñ") == "¿Cuando rego? ¡Mañana! Si, é ü ñ"
    assert text.gsm_safe("“quotes” – dash…") == '"quotes" - dash...'


def test_segments() -> None:
    assert text.segments("a" * 160) == 1
    assert text.segments("a" * 161) == 2
    assert text.segments("a" * 306) == 2
    assert text.segments("ñ" * 160) == 1          # ñ is in the GSM alphabet
    assert text.segments("á" * 71) == 2           # á is not: the message goes UCS-2
    assert text.segments("[" * 80) == 1           # extension characters count twice
    assert text.segments("[" * 81) == 2


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


SAMPLE = {"link": "https://dosojos.example.com/f/AbCdEfGhIjKlMnOp", "field": "Campo Norte",
          "fields": "Campo Norte y La Loma", "name": "Juan Ejemplo", "crop": "sorgo",
          "day": "mar 18 ago", "date": "jue 17 sep", "inches": "4", "gross": "5.3",
          "method": "surcos", "days": "12", "low": "9", "high": "16", "acres": "38.6",
          "said": "40", "raw": "10 af", "hours": "6", "n": "412", "size": "3.1 GB",
          "lat": "26.14840", "lon": "-97.99400", "what": "riego Campo Norte mar 18 ago",
          "options": "1 Campo Norte, 2 La Loma, 3 Todos", "a": "mié 4 mar", "b": "vie 3 abr",
          "stage": "embuche y floración", "where": "en el lado oeste", "yd": "147",
          "contact": "Edgar 956-555-0100", "amount": ", 4 pulgadas",
          "about": "unos 12 días", "year": "2019", "plan": "satélite y dron",
          "chance": "65", "area": "1,240", "pct": "26", "threshold": "30",
          "maturity": "ciclo mediano", "minutes": "15", "problem": "2,940", "total": "9,040",
          "stressed": "33", "missing": "2,907",
          "soil": "Hidalgo franco arcillo arenoso", "left": "4.1", "capacity": "6.2",
          "root": "36", "stress": "2.5", "until": "1.6", "use": "0.28", "rain": "jue 3 sep (0.4 pulg.)",
          "acre_in": "203", "gallons": "5.5 millones", "image": "jue 10 sep",
          "weather": "vie 11 sep", "x": "91", "y": "200", "top": "2.7",
          "more": (", ETAPA (como va el sorgo), PULGON (contar pulgon amarillo), DRON (mandar "
                   "las fotos de su vuelo)"), "midge": " y mosquita (el umbral es 1 por panoja)",
          "rest": (" Sigue grano masoso hacia el mar 3 jun. Revise mosquita cada 3 dias, de 10 "
                   "a 2: el umbral es 1 por panoja.")}


@pytest.mark.parametrize("key", sorted(text.T))
@pytest.mark.parametrize("lang", text.LANGS)
def test_every_message_is_gsm_and_at_most_three_texts(key: str, lang: str) -> None:
    template = text.pick(text.T[key], lang)
    assert _fields(template) <= set(SAMPLE), f"{key}: unknown blank"
    original = template.format(**SAMPLE)
    rendered = text.gsm_safe(original)
    # A character GSM cannot even approximate would come out as an extra "?".
    assert rendered.count("?") == original.count("?"), f"{key}/{lang}: {rendered!r}"
    assert all(c in text._GSM for c in rendered), f"{key}/{lang}: {rendered!r}"
    assert text.segments(rendered) <= 3, f"{key}/{lang} is {text.segments(rendered)} texts"


def test_the_welcome_is_bilingual_and_gsm() -> None:
    welcome = text.gsm_safe(text.WELCOME)
    assert "1" in welcome and "2" in welcome and "STOP" in welcome
    assert all(c in text._GSM for c in welcome) and text.segments(welcome) <= 2


def test_days_carry_the_weekday_and_today() -> None:
    today = date(2026, 9, 12)
    assert text.day(today, "es", today) == "sáb 12 sep (hoy)"
    assert text.day(date(2026, 9, 11), "en", today) == "Fri Sep 11 (yesterday)"
    assert text.day(date(2026, 3, 4), "es", today) == "mié 4 mar"
    assert text.day(date(2025, 12, 20), "en", today) == "Sat Dec 20, 2025"


def test_inches_and_sizes_read_naturally() -> None:
    assert text.inches(4.0) == "4" and text.inches(3.5) == "3.5" and text.inches(0.25) == "0.25"
    assert text.size(3_100_000_000) == "3.1 GB" and text.size(512) == "512 bytes"
    assert text.listing(["A", "B", "C"], "es") == "A, B y C"
    assert text.listing(["A", "B"], "en") == "A and B"
    assert text.about_days(1, "es") == "1 día" and text.about_days(5, "en") == "about 5 days"
