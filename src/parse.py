"""Reading what farmers text: dates, inches, crops, methods, sides, places, yes and no.

People text the way they talk: "regué ayer como 4 pulgadas", "9/2 4", "15 de
marzo", a Google Maps link. Everything here is a pure function of the message
and today's date, so every rule can be tested without a phone.

Whatever is read is read back to the farmer for a yes or a no before it is
stored, so these functions have to be right most of the time and never quietly
wrong. A date that could be 4 March or 3 April comes back as both, to ask; a
number with no unit is only taken for inches once the date has been taken out.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable

from .text import CROP_MENU, METHOD_MENU, PLAN_MENU

# --------------------------------------------------------------------------- #
# Words
# --------------------------------------------------------------------------- #


def normalize(text: str | None) -> str:
    """Lower case, accents and most punctuation gone, spaces collapsed.

    'Sí, regué el 9/2!' becomes 'si, regue el 9/2'. Slashes, dots, commas,
    minus signs and colons stay, since dates, decimals and coordinates need them.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    plain = plain.replace(chr(0x2044), "/")            # the fraction slash in NFKD's ½
    plain = re.sub(r"[¿¡!?\"()*;#]|[‘’“”]", " ", plain)
    plain = plain.replace("'", "")
    return re.sub(r"\s+", " ", plain).strip()


def words(text: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize(text))


_YES = {"si", "s", "yes", "y", "yeah", "yep", "yup", "ok", "okay", "correcto", "correct",
        "claro", "sale", "simon", "afirmativo", "right", "exacto", "asi es", "eso", "sip",
        "si senor", "de acuerdo", "esta bien", "bien", "si es correcto", "that's right",
        "thats right"}
_NO = {"no", "n", "nope", "nel", "incorrecto", "wrong", "no es", "negativo", "nah",
       "no es correcto", "not correct"}
_YES_STARTS = {"si", "yes", "ok", "okay", "correcto", "correct", "claro", "sale", "yep"}
_NO_STARTS = {"no", "nope", "nel", "incorrecto", "wrong"}
_DONT_KNOW = {"no se", "nose", "no lo se", "no sabemos", "ni idea", "no sabe", "not sure",
              "dont know", "i dont know", "idk", "dk", "unknown", "desconocido", "no idea",
              "quien sabe", "?"}


def dont_know(text: str | None) -> bool:
    raw = (text or "").strip()
    return raw == "?" or normalize(raw).strip(" .,") in _DONT_KNOW


def yes_no(text: str | None) -> tuple[bool | None, str]:
    """``(True, rest)``, ``(False, rest)`` or ``(None, '')``.

    'no, fue el 3' is a no with a correction in ``rest``. 'no sé' is neither.
    """
    if dont_know(text):
        return None, ""
    norm = normalize(text).strip(" .,")
    if norm in _YES:
        return True, ""
    if norm in _NO:
        return False, ""
    first, _, rest = norm.partition(" ")
    first = first.strip(".,")
    if first in _YES_STARTS:
        return True, rest.strip(" ,.")
    if first in _NO_STARTS:
        return False, rest.strip(" ,.")
    return None, ""


#: Carriers and Twilio treat these as "stop texting me"; the Spanish ones are ours.
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke",
              "optout", "opt out", "alto", "parar", "para", "baja", "cancelar", "detener",
              "darme de baja", "fin", "basta"}
START_WORDS = {"start", "unstop", "alta", "volver", "reanudar", "iniciar", "subscribe",
               "yes", "si"}
HELP_WORDS = {"help", "info", "ayuda", "ayudame", "auxilio", "opciones", "options"}
GREETINGS = {"hola", "hi", "hello", "buenas", "buenos dias", "buenas tardes",
             "buenas noches", "que tal", "hey", "saludos", "good morning"}


def _bare(text: str | None) -> str:
    return normalize(text).strip(" .,:")


def is_stop(text: str | None) -> bool:
    return _bare(text) in STOP_WORDS


def is_start(text: str | None) -> bool:
    return _bare(text) in START_WORDS


def is_help(text: str | None) -> bool:
    return _bare(text) in HELP_WORDS or (text or "").strip() == "?"


def is_greeting(text: str | None) -> bool:
    return _bare(text) in GREETINGS


COMMANDS: dict[str, tuple[str, ...]] = {
    "status": ("agua", "water", "estado", "status", "como van", "como va", "como estan",
               "reporte", "report"),
    "irrigated": ("regue", "regamos", "rego", "regaron", "riego", "regado", "regando",
                  "eche agua", "echamos agua", "watered", "irrigated", "irrigate",
                  "irrigation", "watering"),
    "rain": ("lluvia", "llovio", "llueve", "rain", "rained", "rainfall"),
    "harvested": ("cosecha", "coseche", "cosechamos", "cosecho", "cosechado", "cosechar",
                  "corte", "cortamos", "harvest", "harvested"),
    "planted": ("sembre", "sembramos", "siembra", "sembrado", "sembro", "sembrar", "planted",
                "plant", "sowed", "seeded"),
    "new_field": ("nuevo", "nueva", "new", "otro campo", "agregar", "add"),
    "fields": ("campos", "fields", "mis campos", "lista", "list"),
    "map": ("mapa", "map"),
    "drone": ("dron", "drone", "vuelo", "flight", "volamos"),
    "plan": ("plan", "planes", "paquete", "servicio", "nivel"),
    # Not "info" or "opciones": those already mean HELP, and a farmer who wants
    # the menu should never get a file instead.
    "explain": ("porque", "por que", "detalle", "detalles", "explicacion", "explique",
                "explicame", "grafica", "graficas", "why", "detail", "details", "explain",
                "chart", "charts"),
    # Sorghum: how far along it is, a sugarcane aphid count, and the hybrid's maturity.
    "stage": ("etapa", "etapas", "fase", "stage", "stages", "growth stage"),
    "aphid": ("pulgon", "pulgones", "pulgon amarillo", "aphid", "aphids", "sugarcane aphid",
              "sca"),
    "maturity": ("ciclo", "hibrido", "maturity", "hybrid"),
    "undo": ("borrar", "borra", "undo", "deshacer", "quitar", "quita", "delete"),
    "menu": ("menu", "salir", "exit", "inicio"),
    "lang_es": ("espanol", "spanish", "en espanol"),
    "lang_en": ("english", "ingles", "en ingles"),
}
_FILLER = {"ya", "hoy", "ayer", "yo", "we", "i", "just", "le", "les", "acabo", "acabamos",
           "de", "ive", "weve", "already", "oye", "hola", "hi", "hello", "buenos", "dias",
           "buenas", "tardes", "ok", "pues", "si"}


def command(text: str | None) -> str | None:
    """The command a message opens with, skipping 'ya', 'hoy' and the like.

    'Ya regamos ayer' is ``irrigated``; 'No hemos regado' is nothing, since a
    command word after a no is not a report.
    """
    tokens = words(text)
    i = 0
    while i < len(tokens) - 1 and i < 3 and tokens[i] in _FILLER:
        i += 1
    if i < len(tokens) and tokens[i] in ("no", "not", "nunca", "never"):
        return None
    for n in (2, 1):
        phrase = " ".join(tokens[i:i + n])
        for name, stems in COMMANDS.items():
            if phrase in stems:
                return name
    return None


def menu_choice(text: str | None, options: int) -> int | None:
    """A bare menu number from 1 to ``options``: '3', '3.', '#3', 'opcion 3'."""
    match = re.fullmatch(r"(?:opcion|option|numero|number|el)?\s*(\d{1,2})\s*[.)]?",
                         normalize(text))
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= options else None


# --------------------------------------------------------------------------- #
# Sorghum: hybrid maturity and sugarcane aphid counts
# --------------------------------------------------------------------------- #

_MATURITY_WORDS: dict[str, tuple[str, ...]] = {
    "short": ("corto", "corta", "temprano", "temprana", "precoz", "short", "early"),
    "medium": ("mediano", "mediana", "medio", "intermedio", "medium", "mid", "middle"),
    "long": ("largo", "larga", "tardio", "tardia", "long", "late", "full"),
}


def maturity(text: str | None) -> str | None:
    """``short``, ``medium``, ``long``, ``'?'`` when not known, or None."""
    choice = menu_choice(text, 4)
    if choice:
        return {1: "short", 2: "medium", 3: "long", 4: "?"}[choice]
    if dont_know(text):
        return "?"
    tokens = set(words(text))
    for key, names in _MATURITY_WORDS.items():
        if tokens & set(names):
            return key
    return None


_OUT_OF = re.compile(r"(\d{1,4})\s*(?:de|of|out of|/|entre|en)\s*(\d{1,4})")
_PERCENT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:%|por ?ciento|percent|pct)")
_NONE_FOUND = {"0", "cero", "ninguno", "ninguna", "nada", "none", "no hay", "no habia",
               "no encontre", "zero", "no"}


@dataclass(frozen=True)
class AphidCount:
    """A sugarcane aphid scouting result: percent of plants infested, and the counts."""

    percent: float
    infested: int | None = None
    checked: int | None = None


def aphid_count(text: str | None) -> AphidCount | None:
    """'12 de 80', '12/80', '15%', a bare '15', or 'ninguno'; None if unreadable.

    A bare number is a percent: a farmer who counted plants says out of how many.
    """
    norm = normalize(text)
    rest = re.sub(r"^(?:pulgon(?:es)?(?: amarillo)?|aphids?|sca)\s*", "", norm).strip(" .,:")
    if rest in _NONE_FOUND:
        return AphidCount(0.0)
    match = _OUT_OF.search(rest)
    if match:
        infested, checked = int(match.group(1)), int(match.group(2))
        if checked == 0 or infested > checked:
            return None
        return AphidCount(100.0 * infested / checked, infested, checked)
    match = _PERCENT.search(rest) or re.fullmatch(r"(\d{1,3}(?:[.,]\d+)?)", rest)
    if match:
        value = float(match.group(1).replace(",", "."))
        return AphidCount(value) if 0 <= value <= 100 else None
    return None


# --------------------------------------------------------------------------- #
# Crops, methods, sides, acres, names
# --------------------------------------------------------------------------- #

_CROP_WORDS: dict[str, tuple[str, ...]] = {
    "sorghum": ("sorgo", "milo", "maicillo", "sorghum", "grano"),
    "cotton": ("algodon", "cotton"),
    "corn": ("maiz", "elote", "corn", "maize"),
    "sugarcane": ("cana", "sugarcane", "cane", "azucar"),
    "citrus": ("citricos", "citrico", "citrus", "naranja", "naranjas", "toronja", "toronjas",
               "limon", "limones", "mandarina", "mandarinas", "huerta", "orange", "oranges",
               "grapefruit", "lemon", "lemons", "lime", "limes", "orchard", "grove"),
    "soybean": ("soya", "soja", "soybean", "soybeans", "soy"),
    "none": ("nada", "barbecho", "pelon", "vacio", "descanso", "fallow", "bare", "nothing",
             "empty", "none", "sin cultivo", "sin sembrar"),
    "other": ("otro", "otra", "other"),
}


def crop(text: str | None) -> tuple[str | None, str | None]:
    """``(key, name)``: ``('sorghum', None)``, ``('other', 'chile')`` or ``(None, None)``.

    A crop the list lacks ('chile', 'cebolla') is taken as another crop by that name.
    """
    if dont_know(text):
        return None, None
    choice = menu_choice(text, len(CROP_MENU))
    if choice:
        return CROP_MENU[choice - 1], None
    tokens = words(text)
    joined = " ".join(tokens)
    for key, stems in _CROP_WORDS.items():
        for stem in stems:
            if (" " in stem and stem in joined) or stem in tokens:
                if key == "other":
                    rest = " ".join(t for t in (text or "").split()
                                    if normalize(t).strip(".,:") not in stems).strip(" :,.-")
                    return "other", (rest[:30] or None)
                return key, None
    if 0 < len(tokens) <= 3 and not any(t.isdigit() for t in tokens):
        return "other", (text or "").strip()[:30]
    return None, None


def crop_word(text: str | None) -> tuple[str | None, str | None]:
    """A crop named inside a longer message ('sembré sorgo ayer'): known words only."""
    tokens = words(text)
    for key, stems in _CROP_WORDS.items():
        if key not in ("none", "other") and any(stem in tokens for stem in stems):
            return key, None
    return None, None


_METHOD_WORDS: dict[str, tuple[str, ...]] = {
    "none": ("temporal", "secano", "no se riega", "no riego", "no la riego", "no lo riego",
             "rainfed", "dryland", "lluvia", "not irrigated", "no irrigation", "none"),
    "furrow": ("surco", "surcos", "furrow", "furrows", "rodado", "gravedad", "gravity"),
    "border": ("melga", "melgas", "bordo", "bordos", "border", "borders"),
    "flood": ("inundacion", "inundado", "tablas", "tabla", "cuadros", "flood", "flooding"),
    "basin": ("cajete", "cajetes", "basin", "basins", "cepa", "cepas"),
    "drip": ("goteo", "cinta", "gotero", "goteros", "drip", "tape", "micro"),
    "sprinkler": ("aspersion", "aspersor", "aspersores", "microaspersion", "sprinkler",
                  "sprinklers"),
    "pivot": ("pivote", "pivot", "center pivot"),
}


def method(text: str | None) -> str | None:
    """How a field is watered, as the checkbook's method key."""
    choice = menu_choice(text, len(METHOD_MENU))
    if choice:
        return METHOD_MENU[choice - 1]
    tokens = words(text)
    joined = " ".join(tokens)
    for key, stems in _METHOD_WORDS.items():
        if any((" " in s and s in joined) or s in tokens for s in stems):
            return key
    return None


#: Words for each plan, for farmers who answer the question in words.
_PLAN_WORDS: dict[str, tuple[str, ...]] = {
    "thermal": ("termica", "termico", "thermal", "calor", "heat", "plagas", "plaga", "pest",
                "pests"),
    "drone": ("dron", "drone", "dos", "both"),
    "satellite": ("satelite", "satellite", "solo satelite", "satellite only", "nada",
                  "ninguno", "nothing", "none"),
}


def plan(text: str | None) -> str | None:
    """Which service a farmer picked: the menu number, or the words for it.

    Thermal is looked for first: "dron y termica" names both, and the larger of
    the two is what they meant.
    """
    choice = menu_choice(text, len(PLAN_MENU))
    if choice:
        return PLAN_MENU[choice - 1]
    tokens = words(text)
    joined = " ".join(tokens)
    for key, stems in _PLAN_WORDS.items():
        if any((" " in stem and stem in joined) or stem in tokens for stem in stems):
            return key
    return None


_SIDE_WORDS: dict[str, tuple[str, ...]] = {
    "N": ("n", "norte", "north", "nte"), "S": ("s", "sur", "south"),
    "E": ("e", "este", "east", "oriente"), "W": ("w", "o", "oeste", "west", "poniente"),
}


def side(text: str | None) -> str | None:
    """'N', 'S', 'E' or 'W'; '?' when the farmer does not know; None if unreadable.

    Two sides at once ('norte o sur') is unreadable, and so asked again.
    """
    if dont_know(text):
        return "?"
    tokens = words(text)
    found = {key for key, stems in _SIDE_WORDS.items() for t in tokens if t in stems}
    return found.pop() if len(found) == 1 else None


def acres(text: str | None) -> float | str | None:
    """Acres as a number, '?' when not known, None when unreadable or implausible."""
    if dont_know(text):
        return "?"
    match = re.search(r"(\d+(?:[.,]\d+)?)", normalize(text))
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value if 0.2 <= value <= 5000 else None


def name(text: str | None, limit: int = 40) -> str | None:
    """A person's or a field's name as typed, tidied: None if there is nothing to it."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip().strip("\"'.,"))
    cleaned = re.sub(r"^(?:me llamo|mi nombre es|soy|se llama|le decimos|my name is|"
                     r"i'm|im|it's called|we call it)\s+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned or not re.search(r"[A-Za-zÀ-ÿ]", cleaned):
        return None
    return cleaned[:limit]


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

_MONTHS = {
    "ene": 1, "enero": 1, "jan": 1, "january": 1, "feb": 2, "febrero": 2, "february": 2,
    "mar": 3, "marzo": 3, "march": 3, "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "may": 5, "mayo": 5, "jun": 6, "junio": 6, "june": 6, "jul": 7, "julio": 7, "july": 7,
    "ago": 8, "agosto": 8, "aug": 8, "august": 8, "sep": 9, "sept": 9, "septiembre": 9,
    "setiembre": 9, "september": 9, "oct": 10, "octubre": 10, "october": 10, "nov": 11,
    "noviembre": 11, "november": 11, "dic": 12, "diciembre": 12, "dec": 12, "december": 12,
}
_WEEKDAYS = {
    "lunes": 0, "monday": 0, "martes": 1, "tuesday": 1, "miercoles": 2, "wednesday": 2,
    "jueves": 3, "thursday": 3, "viernes": 4, "friday": 4, "sabado": 5, "saturday": 5,
    "domingo": 6, "sunday": 6,
}
_NUMBER_WORDS = {
    "un": 1, "una": 1, "uno": 1, "one": 1, "dos": 2, "two": 2, "tres": 3, "three": 3,
    "cuatro": 4, "four": 4, "cinco": 5, "five": 5, "seis": 6, "six": 6, "siete": 7,
    "seven": 7, "ocho": 8, "eight": 8, "nueve": 9, "nine": 9, "diez": 10, "ten": 10,
    "once": 11, "eleven": 11, "doce": 12, "twelve": 12,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_RE = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_COUNT_RE = r"\d{1,3}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))

#: How far back each kind of date may reach. A last irrigation seven months ago
#: is not what anyone means by 9/2 in September, so it does not count as a reading.
WINDOWS = {
    "planted": 400, "irrigated": 45, "last_irrigation": 120, "history": 400,
    "rain": 45, "harvested": 90, "flight": 90, "ticket": 180,
}


@dataclass(frozen=True)
class DateFound:
    """A date read from a message, the choices when it could be two, or why not.

    ``problem`` is ``future``, ``old`` or ``vague``; ``near`` is the date it is about.
    ``span`` is where the date sat in the normalized text, so the rest can be read
    for inches without mistaking the day for an amount.
    """

    day: date | None = None
    options: tuple[date, ...] = ()
    problem: str | None = None
    near: date | None = None
    span: tuple[int, int] | None = None


def _count(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


#: A date without a year up to this far ahead is a slip for a day to come, not last year.
_AHEAD_DAYS = 31


def _make(year: int | None, month: int, day_: int, today: date) -> date | None:
    """A date; the year left out means the last time that day came round.

    Except just ahead of today: '9/20' typed on 12 September is taken as this
    year's, to be refused as not yet come, rather than as last year's.
    """
    try:
        if year is not None:
            return date(year + 2000 if year < 100 else year, month, day_)
        candidate = date(today.year, month, day_)
        if candidate <= today or (candidate - today).days <= _AHEAD_DAYS:
            return candidate
        return date(today.year - 1, month, day_)
    except ValueError:
        return None


def _numeric(norm: str, today: date):
    match = (re.search(r"(?<![\d.,/])(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{4}|\d{2}))?(?![\d/])",
                       norm)
             or re.search(r"(?<![\d.,-])(\d{1,2})-(\d{1,2})-(\d{4}|\d{2})(?![\d-])", norm))
    if not match:
        return None
    a, b = int(match.group(1)), int(match.group(2))
    year = int(match.group(3)) if match.group(3) else None
    # Month first is the US habit and day first the Mexican one; both are read,
    # and the caller asks when both land inside the window.
    found = [d for d in (_make(year, a, b, today), _make(year, b, a, today)) if d]
    return found, match.span()


def _iso(norm: str, today: date):
    match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", norm)
    if not match:
        return None
    found = _make(int(match.group(1)), int(match.group(2)), int(match.group(3)), today)
    return ([found] if found else []), match.span()


def _month_names(norm: str, today: date):
    match = re.search(rf"\b(\d{{1,2}})\s*(?:de\s+)?({_MONTH_RE})\b\.?"
                      rf"(?:\s*(?:de|del)?\s*(\d{{4}}))?", norm)
    if match:
        day_, month, year = int(match.group(1)), _MONTHS[match.group(2)], match.group(3)
    else:
        match = re.search(rf"\b({_MONTH_RE})\b\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?\b"
                          rf"(?:\s*,?\s*(\d{{4}}))?", norm)
        if not match:
            return None
        month, day_, year = _MONTHS[match.group(1)], int(match.group(2)), match.group(3)
    found = _make(int(year) if year else None, month, day_, today)
    return ([found] if found else []), match.span()


def _relative(norm: str, today: date):
    rules = (
        (r"\b(?:antier|anteayer|antes de ayer|day before yesterday)\b", lambda m: 2),
        (r"\b(?:hoy|today|esta manana|this morning|ahorita)\b", lambda m: 0),
        (r"\b(?:ayer|yesterday|anoche|last night)\b", lambda m: 1),
        (rf"\bhace\s+({_COUNT_RE})\s+dias?\b", lambda m: _count(m.group(1))),
        (rf"\b({_COUNT_RE})\s+days?\s+ago\b", lambda m: _count(m.group(1))),
        (rf"\bhace\s+({_COUNT_RE})\s+semanas?\b", lambda m: 7 * _count(m.group(1))),
        (rf"\b({_COUNT_RE}|a)\s+weeks?\s+ago\b",
         lambda m: 7 * (1 if m.group(1) == "a" else _count(m.group(1)))),
    )
    for pattern, days_back in rules:
        match = re.search(pattern, norm)
        if match:
            return [today - timedelta(days=days_back(match))], match.span()
    return None


def _weekday(norm: str, today: date):
    match = re.search(rf"\b(?:el\s+|last\s+|on\s+)?({_WEEKDAY_RE})\b(\s+pasado)?", norm)
    if not match:
        return None
    back = (today.weekday() - _WEEKDAYS[match.group(1)]) % 7
    if back == 0 and (match.group(2) or match.group(0).startswith("last")):
        back = 7
    return [today - timedelta(days=back)], match.span()


def _day_only(norm: str, today: date):
    match = re.search(r"\b(?:el|dia|the)\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*/)", norm)
    if not match:
        return None
    day_ = int(match.group(1))
    for back in range(0, 3):
        month, year = today.month - back, today.year
        while month < 1:
            month, year = month + 12, year - 1
        try:
            candidate = date(year, month, day_)
        except ValueError:
            continue
        if candidate <= today:
            return [candidate], match.span()
    return [], match.span()


_VAGUE = re.compile(r"\b(?:semana pasada|la otra semana|last week|mes pasado|last month|"
                    rf"en ({_MONTH_RE})|in ({_MONTH_RE}))\b")


def find_date(text: str | None, today: date, *, window: int | str = 400) -> DateFound:
    """The day a message names, judged against how far back it may reach.

    ``window`` is a number of days or a key of :data:`WINDOWS`. Nothing in the
    future counts: every date asked about here is something that already happened.
    """
    max_age = WINDOWS[window] if isinstance(window, str) else int(window)
    norm = normalize(text)
    for finder in (_iso, _numeric, _month_names, _relative, _weekday, _day_only):
        found = finder(norm, today)
        if found is None:
            continue
        candidates, span = found
        inside = sorted({d for d in candidates if today - timedelta(days=max_age) <= d <= today})
        if len(inside) == 1:
            return DateFound(day=inside[0], span=span)
        if len(inside) > 1:
            return DateFound(options=tuple(inside), span=span)
        future = [d for d in candidates if d > today]
        if future:
            return DateFound(problem="future", near=min(future), span=span)
        if candidates:
            return DateFound(problem="old", near=max(candidates), span=span)
        return DateFound(span=span)
    if _VAGUE.search(norm):
        return DateFound(problem="vague")
    return DateFound()


def without(norm: str, span: tuple[int, int] | None) -> str:
    """The normalized text with one stretch blanked out."""
    if not span:
        return norm
    return norm[:span[0]] + " " + norm[span[1]:]


# --------------------------------------------------------------------------- #
# Amounts of water
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Amount:
    """Water read from a message.

    ``inches`` is depth over the field. Acre-feet and acre-inches need the
    field's acres to become inches; without them ``per_acre`` holds the unit and
    ``raw`` the number. Hours of water cannot be turned into inches without the
    flow, so they are kept as a note.
    """

    inches: float | None = None
    raw: str | None = None
    per_acre: str | None = None
    quantity: float | None = None
    hours: float | None = None


_NUM = rf"(\d+(?:[.,]\d+)?(?:\s+[13]/[248])?|[13]/[248]|media|half|{_COUNT_RE})"
_INCH = r"(?:pulgadas?|pulgs?|pulg\.?|pul|in\b|inch(?:es)?|\"|'')"
_ACRE_FEET = r"(?:acre[-\s]?(?:pies?|feet|foot|ft)|acres?[-\s]pie|ac[-\s]?ft|af\b|a/f)"
_ACRE_INCH = r"(?:acre[-\s]?(?:pulgadas?|inch(?:es)?|in\b)|ac[-\s]?in\b|ai\b)"
_HOURS = r"(?:horas?|hrs?\b|hours?)"
_HALF = r"(?:\s+y\s+media|\s+and\s+a\s+half)?"


def _number(token: str) -> float:
    token = token.strip()
    if token in ("media", "half"):
        return 0.5
    if token in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[token])
    whole, _, fraction = token.partition(" ")
    if "/" in whole:
        whole, fraction = "0", whole
    value = float(whole.replace(",", "."))
    if fraction:
        top, bottom = fraction.split("/")
        value += int(top) / int(bottom)
    return value


def find_amount(norm: str, acres_: float | None = None) -> Amount | None:
    """Inches (or acre-feet, acre-inches, hours) in already-normalized text."""
    for unit_re, kind in ((_ACRE_FEET, "af"), (_ACRE_INCH, "ai"), (_HOURS, "hours"),
                          (_INCH, "in")):
        match = re.search(rf"(?<![\d./]){_NUM}\s*{unit_re}{_HALF}", norm)
        if not match:
            continue
        value = _number(match.group(1)) + (0.5 if re.search(r"media|half", match.group(0)[
            len(match.group(1)):]) else 0.0)
        if kind == "hours":
            return Amount(hours=value)
        if kind == "in":
            return Amount(inches=value)
        label = "af" if kind == "af" else "ac-in"
        raw = f"{value:g} {label}"
        if not acres_:
            return Amount(raw=raw, per_acre=kind, quantity=value)
        inches_ = value * 12.0 / acres_ if kind == "af" else value / acres_
        return Amount(inches=round(inches_, 2), raw=raw, per_acre=kind, quantity=value)
    match = re.search(r"(?<![\d./,])(\d+(?:[.,]\d+)?(?:\s+[13]/[248])?)(?![\d/])", norm)
    if match:
        return Amount(inches=_number(match.group(1)))
    return None


# --------------------------------------------------------------------------- #
# Places
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Place:
    lat: float
    lon: float
    #: True when the farmer left the minus sign off a US longitude.
    fixed_sign: bool = False

    @property
    def in_conus(self) -> bool:
        """Inside the lower 48, where gridMET weather exists."""
        return 24.3 <= self.lat <= 49.5 and -125.0 <= self.lon <= -66.5


_COORD = r"(-?\d{1,3}\.\d+)"
_PLACE_PATTERNS = (
    rf"!3d{_COORD}!4d{_COORD}",
    rf"[?&](?:q|query|ll|sll|center|daddr|destination|coordinate|near|cp)="
    rf"{_COORD}\s*,\s*\+?{_COORD}",
    rf"\bgeo:{_COORD},{_COORD}",
    rf"/(?:place|search|dir)/{_COORD},\s*\+?{_COORD}",
    rf"@{_COORD},{_COORD}",
    r"(?<![\d.])(-?\d{1,2}\.\d{2,})\s*[,;/\s]\s*(-?\d{1,3}\.\d{2,})(?![\d.])",
)
_DMS = re.compile(
    r"(\d{1,2})\s*[°º]\s*(\d{1,2})\s*['′]\s*(\d{1,2}(?:\.\d+)?)\s*(?:\"|″|'')?\s*([NS])"
    r"[\s,]*(\d{1,3})\s*[°º]\s*(\d{1,2})\s*['′]\s*(\d{1,2}(?:\.\d+)?)\s*(?:\"|″|'')?\s*([EWO])",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"]+")
_SHORT = re.compile(r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|g\.co/kgs|share\.google|"
                    r"maps\.apple/p)/\S+", re.IGNORECASE)


def _checked(lat: float, lon: float) -> Place | None:
    """A plausible point, with the usual slips put right: swapped, or west without a minus."""
    if abs(lat) > 90 or (-125 <= lat <= -66 and 24 <= lon <= 50):
        lat, lon = lon, lat
    fixed = False
    if 24 <= lat <= 50 and 66 <= lon <= 125:
        lon, fixed = -lon, True
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return Place(round(lat, 6), round(lon, 6), fixed)


def _coords_in(text: str) -> Place | None:
    for pattern in _PLACE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            place = _checked(float(match.group(1)), float(match.group(2)))
            if place:
                return place
    match = _DMS.search(text)
    if match:
        g = match.groups()
        lat = int(g[0]) + int(g[1]) / 60 + float(g[2]) / 3600
        lon = int(g[4]) + int(g[5]) / 60 + float(g[6]) / 3600
        lat = -lat if g[3].upper() == "S" else lat
        lon = -lon if g[7].upper() in ("W", "O") else lon
        return _checked(lat, lon)
    return None


Resolver = Callable[[str], Iterable[str]]


def find_place(text: str | None, *, lat: float | None = None, lon: float | None = None,
               resolve: Resolver | None = None) -> Place | str | None:
    """A :class:`Place`, ``'unresolved'`` for a map link that would not open, or None.

    WhatsApp sends a shared location as numbers beside the text (``lat``/``lon``).
    Google's short links (maps.app.goo.gl) carry no coordinates until followed;
    ``resolve`` follows one and returns every address and page it passed through.
    """
    if lat is not None and lon is not None:
        return _checked(float(lat), float(lon))
    raw = text or ""
    texts = [urllib.parse.unquote_plus(raw)]
    short = _SHORT.findall(raw)
    for url in short:
        if resolve is None:
            continue
        try:
            texts.extend(urllib.parse.unquote_plus(t) for t in resolve(url))
        except Exception:     # a dead link, no network: ask for the numbers instead
            return "unresolved"
    for candidate in texts:
        place = _coords_in(candidate)
        if place:
            return place
    return "unresolved" if short else None


def resolve_link(url: str, *, timeout: float = 10.0) -> list[str]:
    """Follow a short map link; returns each address on the way and the page's start."""
    import requests

    response = requests.get(url, allow_redirects=True, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0 (DosOjos SMS; field locator)"})
    seen = [r.headers.get("Location", "") for r in response.history] + [response.url]
    return seen + [response.text[:200_000]]
