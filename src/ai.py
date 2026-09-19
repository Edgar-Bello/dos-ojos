"""The language model on this computer: it reads farmers' own words and writes the advice.

Dos Ojos runs an open model (Llama 3.2 3B by default) through Ollama on the same
machine as the server: free, offline, and nothing a farmer says leaves the farm's
computer. It does two jobs.

**Reading.** A text the word lists cannot place ("we got like an inch of rain
last night on the grove") goes to the model, which turns it into one of the bot's
own commands. Everything it hears still goes through the read-back YES/NO before
anything is stored, so a misreading costs a NO, never a wrong record. A question
("do my trees need water this week?") gets an answer from the field's own numbers.

**The recommendation.** Once a field has been read, the model is given everything
there is: the water checkbook (soil, weather, crop, waterings), the satellite's
greenness against the field's own normal, the sorghum stage, the drone's and the
trained tree model's findings, thermal patches, and what the farmer has logged.
It writes what to do, in plain words, with its reasons. That is the text the
farmer gets.

A small model can be confidently wrong, so its advice is checked before it goes
out, and the checkbook's answer goes instead when it fails:

- its action must agree with the checkbook's (water now, soon, not yet...);
- the day it gives must sit inside the checkbook's range, give or take one;
- any number with a decimal point, or above 31, must come from the readings it was given;
- it must fit in two text messages.

Nothing here needs the model: with Ollama stopped, or DOSOJOS_AI=0, every answer
is the checkbook's, exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from . import text

log = logging.getLogger(__name__)

#: A CPU answer from a 3B model: a few seconds to read, ten or twenty to write.
TIMEOUT_S = 240
#: How long "is the model there?" is believed before asking again.
AVAILABLE_FOR_S = 30
#: What an answer may cost the farmer: two text messages.
MAX_MESSAGE_CHARS = 300


class AIError(RuntimeError):
    """The model could not be reached or gave nothing usable."""


# --------------------------------------------------------------------------- #
# Talking to the model
# --------------------------------------------------------------------------- #


Poster = Callable[[str, dict, float], dict]


def _post(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class LocalAI:
    """One model on one Ollama server."""

    def __init__(self, url: str, model: str, *, post: Poster | None = None,
                 get: Callable[[str, float], dict] | None = None):
        self.url, self.model = url.rstrip("/"), model
        self._post, self._get = post or _post, get or _get
        self._seen: tuple[float, bool] | None = None

    def available(self) -> bool:
        """True when the server answers and has the model pulled."""
        now = time.monotonic()
        if self._seen and now - self._seen[0] < AVAILABLE_FOR_S:
            return self._seen[1]
        try:
            tags = self._get(self.url + "/api/tags", 2)
            names = {m.get("name", "") for m in tags.get("models", [])}
            ok = self.model in names or f"{self.model}:latest" in names
            if not ok:
                log.warning("Ollama is running but has no %s; run: ollama pull %s",
                            self.model, self.model)
        except (OSError, ValueError, urllib.error.URLError):
            ok = False
        self._seen = (now, ok)
        return ok

    def json(self, system: str, prompt: str, schema: dict, *, max_tokens: int = 350) -> dict:
        """The model's answer to ``prompt``, held to ``schema``."""
        payload = {
            "model": self.model, "stream": False, "format": schema, "keep_alive": "30m",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "options": {"temperature": 0.1, "num_predict": max_tokens, "num_ctx": 4096},
        }
        started = time.monotonic()
        try:
            reply = self._post(self.url + "/api/chat", payload, TIMEOUT_S)
            content = reply["message"]["content"]
            answer = json.loads(content)
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
            self._seen = None
            raise AIError(f"no usable answer from {self.model}: {exc}") from exc
        if not isinstance(answer, dict):
            raise AIError(f"{self.model} answered with {type(answer).__name__}, not an object")
        log.info("AI answered in %.1f s", time.monotonic() - started)
        return answer


_MODELS: dict[tuple[str, str], LocalAI] = {}


def for_settings(settings) -> LocalAI | None:
    """The configured model, or None when AI is switched off."""
    if not settings.ai_url:
        return None
    key = (settings.ai_url, settings.ai_model)
    if key not in _MODELS:
        _MODELS[key] = LocalAI(settings.ai_url, settings.ai_model)
    return _MODELS[key]


def ready(settings) -> LocalAI | None:
    """The model when it can be asked right now, else None."""
    model = for_settings(settings)
    return model if model is not None and model.available() else None


class Thinker:
    """One background thread for the model, apart from the satellite and drone jobs.

    A reading or a flight can take ten minutes; a farmer's question should not wait
    behind it. One at a time, since one model answers one question at a time.
    With ``hold=True`` (tests) work waits until :meth:`run_held`.
    """

    def __init__(self, *, hold: bool = False):
        self.hold = hold
        self.held: list[Callable[[], None]] = []
        self._queue: queue.Queue = queue.Queue()
        self._waiting: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def add(self, key: str, work: Callable[[], None]) -> bool:
        if self.hold:
            self.held.append(work)
            return True
        with self._lock:
            if key in self._waiting:
                return False
            self._waiting.add(key)
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="dosojos-ai",
                                                daemon=True)
                self._thread.start()
        self._queue.put((key, work))
        return True

    def run_held(self) -> None:
        while self.held:
            self.held.pop(0)()

    def _work(self) -> None:
        while True:
            key, work = self._queue.get()
            try:
                work()
            except Exception:           # one bad answer must not stop the thread
                log.exception("AI work %s failed", key)
            finally:
                with self._lock:
                    self._waiting.discard(key)


# --------------------------------------------------------------------------- #
# Reading a farmer's own words
# --------------------------------------------------------------------------- #

INTENTS = ("irrigated", "rain", "harvested", "planted", "status", "explain", "fields",
           "new_field", "map", "drone", "stage", "aphid", "plan", "crop", "undo", "help",
           "question", "other")
CROPS = ("sorghum", "cotton", "corn", "sugarcane", "citrus", "soybean", "")

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "field": {"type": "string"},
        "crop": {"type": "string", "enum": list(CROPS)},
        "answer": {"type": "string"},
    },
    "required": ["intent", "field", "crop", "answer"],
}

# Days and amounts are not the model's job: the rules read "antier", "anoche",
# "media pulgada" and "9/11" exactly, and a 3B model does not. It says what the
# farmer means and which field; the rules take the numbers from the farmer's text.
UNDERSTAND_SYSTEM = """You sort text messages that farmers in the Rio Grande Valley send to Dos Ojos, a service that tells them when to water. Messages are Spanish or English, short, informal, often misspelled. Answer in JSON: intent, field, crop, answer.

intent is one of:
irrigated = they watered (regar, regue, regamos, una regada, una mojada, le echamos agua, le dimos agua, watered, irrigated)
rain = it rained (llovio, cayo agua del cielo, lluvia, aguacero, rained)
harvested = they harvested or cut the crop (cosechamos, cortamos, trillamos, harvested, cut)
planted = they planted or sowed (sembramos, plantamos, planted, sowed)
status = they ask whether or when to water, or how the fields are doing
explain = they want the charts, graphs or the reasons
fields = they want the list of their fields
new_field = they want to add another field
map = they want to draw or fix a field's map
drone = they want to send drone photos
stage = how far along the sorghum is
aphid = sugarcane aphid (pulgon amarillo) counts
plan = change satellite, drone or thermal
crop = change which crop a field has
undo = remove the last thing they sent
help = they want to know what they can send
question = another question about their fields that FIELDS can answer
other = thanks, greetings, jokes, unclear or off topic

field: a name copied exactly from FIELDS when the message points to one, by its name or by its crop ("el citrico", "the sorghum", "la huerta" = the citrus); otherwise "".
crop: only for planted or crop: sorghum, cotton, corn, sugarcane, citrus or soybean; else "".
answer: only for question: under 240 characters, in the farmer's language, using only FIELDS; if FIELDS do not say, that you will pass it to the team. Otherwise "".

Examples (FIELDS: Norte = sorghum, Huerta = citrus):
"le dimos una mojada a la huerta ayer" -> irrigated, Huerta
"anoche cayo como una pulgada" -> rain, ""
"ya cortamos el sorgo" -> harvested, Norte
"we planted corn in norte last week" -> planted, Norte, corn
"cuanto le falta al sorgo pa regarlo" -> status, Norte
"quiero ver las graficas de la huerta" -> explain, Huerta
"gracias compa" -> other
"buenos dias" -> other
"cuando fue la ultima vez que regamos la huerta?" -> question, Huerta"""


@dataclass
class Understood:
    intent: str
    field: str | None = None
    day: date | None = None
    inches: float | None = None
    crop: str | None = None
    answer: str | None = None

    def command_text(self, original: str) -> str | None:
        """The same request in the bot's own words, for the rules to read and confirm.

        English command words work in either language; the read-back that follows
        is in the farmer's own. None when there is no command to run.
        """
        name = self.field or ""
        day = f"{self.day.month}/{self.day.day}" if self.day else ""
        amount = f"{self.inches:g}" if self.inches is not None else ""
        words = {
            "irrigated": f"watered {name} {day} {amount}",
            "rain": f"rain {day} {amount}",
            "harvested": f"harvested {name} {day}",
            "planted": f"planted {self.crop or ''} {name} {day}",
            "status": "water", "explain": f"why {name}", "fields": "fields",
            "new_field": "new", "map": f"map {name}", "drone": f"drone {name}",
            "stage": "stage", "aphid": f"aphid {original}", "plan": f"plan {name}",
            "crop": f"crop {name}", "undo": "undo", "help": "help",
        }.get(self.intent)
        return re.sub(r"\s+", " ", words).strip() if words else None


#: Kinds of message where a day and an amount matter.
_DATED = {"irrigated": "irrigated", "rain": "rain", "harvested": "harvested",
          "planted": "planted"}


def understand(model: LocalAI, message: str, *, lang: str, today: date,
               fields: list[dict]) -> Understood:
    """What a text the rules could not place is asking for."""
    from . import parse

    listing = "\n".join(
        f"- {f['name']}: {f.get('summary') or ''}".rstrip(": ") for f in fields) or "(none)"
    prompt = (f"TODAY: {today.isoformat()} ({today.strftime('%A')})\n"
              f"FARMER'S LANGUAGE: {'Spanish' if lang == 'es' else 'English'}\n"
              f"FIELDS:\n{listing}\n\nMESSAGE: {message}")
    raw = model.json(UNDERSTAND_SYSTEM, prompt, UNDERSTAND_SCHEMA, max_tokens=120)
    intent = raw.get("intent") if raw.get("intent") in INTENTS else "other"
    names = {f["name"].lower(): f["name"] for f in fields}
    named = names.get(str(raw.get("field") or "").strip().lower())
    crop = raw.get("crop") if raw.get("crop") in CROPS[:-1] else None
    answer = str(raw.get("answer") or "").strip()[:MAX_MESSAGE_CHARS] or None
    if named is None:
        # "a la huerta", "el sorgo": the one field with that crop, as the rules read it.
        key, _ = parse.crop_word(message)
        crops = {f["name"]: f.get("crop") for f in fields}
        matches = [name for name, c in crops.items() if key and c == key]
        named = matches[0] if len(matches) == 1 else None
    heard = Understood(intent, named, crop=crop,
                       answer=answer if intent == "question" else None)
    if intent in _DATED:
        norm = parse.normalize(message)
        found = parse.find_date(norm, today, window=_DATED[intent])
        heard.day = found.day
        amount = parse.find_amount(parse.without(norm, found.span))
        if amount and amount.inches and intent in ("irrigated", "rain"):
            heard.inches = amount.inches
    return heard


# --------------------------------------------------------------------------- #
# The recommendation
# --------------------------------------------------------------------------- #

ACTIONS = ("water_now", "water_soon", "no_water_yet", "not_irrigated", "harvested")

# The water balance decides when and how much: that is arithmetic, and a 3B model
# gets arithmetic wrong (it once told a rainfed field to water "now", in 27 days).
# The model's job is the part arithmetic cannot do: read everything else there
# is about the field and write what the farmer should do and look at, and why.
ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "check_first": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["message", "check_first", "reasons", "confidence"],
}

ADVICE_SYSTEM = """You are the farm advisor of Dos Ojos. You write one text message to a farmer in the Rio Grande Valley about one field. You get FACTS: first the DECISION from the field's water balance, then everything else known: soil, weather, satellite greenness, crop stage, drone and tree findings, thermal camera, and what the farmer logged.

Write JSON:
message: the recommendation, in LANGUAGE, at most 260 characters, plain words a farmer uses. Start with the DECISION, keeping its day and amount exactly. Then add the one other fact that most changes what the farmer should do or watch (a drone or tree finding, low greenness, a stage that cannot go dry, pests, a thermal patch). Do not repeat the field's name. No emoji.
check_first: one short thing to go and look at in the field, in LANGUAGE, taken from the FACTS, or "" if nothing needs a look.
reasons: two to four short reasons in LANGUAGE, each saying which fact it comes from.
confidence: high, medium or low, from the water balance's confidence and whether the facts agree.

Use only numbers written in FACTS, written the same way. Never change the DECISION."""


@dataclass
class Advice:
    action: str
    water_in_days: int | None
    message: str
    check_first: str
    reasons: list[str]
    confidence: str
    model: str
    made: str
    checked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Advice":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


def expected_action(brief: dict) -> str | None:
    """The action the water balance calls for."""
    book = brief.get("water_checkbook") or {}
    if book.get("status") == "harvested":
        return "harvested"
    if (brief.get("field") or {}).get("watered_by") == "rainfed (not irrigated)":
        return "not_irrigated"
    days = book.get("days_until_water")
    if days is None:
        return None
    return "water_now" if days <= 0 else "water_soon" if days <= 3 else "no_water_yet"


_WEEKDAYS = {"en": ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                    "sunday"),
             "es": ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")}
_NOW_WORDS = ("hoy", "ahora", "ya ", "cuanto antes", "today", "now", "right away")
_WATER_WORDS = ("riegue", "regar", "riego", "irrigate", "water it", "water now", "water in",
                "water the", "water by")


def facts(brief: dict) -> list[str]:
    """The brief as plain sentences, the decision first: a small model reads these far
    better than nested JSON, and every number it may use is written here once."""
    book = brief.get("water_checkbook") or {}
    info = brief.get("field") or {}
    action = expected_action(brief)
    days, rng = book.get("days_until_water"), book.get("days_range") or []
    refill = book.get("refill_in")
    by = book.get("water_by")
    when = ""
    if by:
        d = date.fromisoformat(by)
        when = f"{_WEEKDAYS['en'][d.weekday()].title()} {d.strftime('%B')} {d.day}"
    give = f"; give about {refill:g} inches" if refill else ""
    decision = {
        "water_now": f"water now, today{give}.",
        "water_soon": f"water in about {days} days, by {when}{give}.",
        "no_water_yet": (f"no water needed yet: water in about {days} days"
                         + (f" (between {rng[0]} and {rng[1]})" if len(rng) == 2 else "")
                         + f", by {when}{give}."),
        "not_irrigated": "this field is rainfed, so there is nothing to water; only watch it.",
        "harvested": "the crop is harvested; nothing to water until the next planting.",
    }.get(action, "no water decision yet.")
    lines = [f"DECISION (from the water balance, do not change it): {decision}"]
    lines.append(f"Field: {info.get('crop')}"
                 + (f", {info['acres']:g} acres" if info.get("acres") else "")
                 + f", {info.get('watered_by')}"
                 + (f", planted {info['planted']}" if info.get("planted") else "") + ".")
    soil = []
    if book.get("soil"):
        soil.append(f"soil {book['soil']}")
    if book.get("soil_water_left_pct") is not None:
        soil.append(f"{book['soil_water_left_pct']}% of the soil's water is left")
    if book.get("crop_use_in_per_day"):
        soil.append(f"the crop uses {book['crop_use_in_per_day']:g} inches a day")
    if book.get("stressed_days_last_30"):
        soil.append(f"{book['stressed_days_last_30']} days short of water in the last 30")
    if soil:
        lines.append("Water balance: " + "; ".join(soil) + f" (confidence {book.get('confidence')}).")
    lines.append(f"Last watering: {book.get('last_irrigation') or 'none on record'}; "
                 f"last rain: {book.get('last_rain') or 'none on record'}.")
    stage = brief.get("sorghum_stage")
    if stage:
        lines.append(f"Sorghum stage: {stage.get('stage')}, {stage.get('days_after_planting')} "
                     f"days after planting; next: {stage.get('next_stage')} around "
                     f"{stage.get('next_date')}. Watch for: "
                     f"{', '.join(w.replace('_', ' ') for w in stage.get('watch_for') or []) or 'nothing special'}."
                     + (f" Sugarcane aphid threshold now: {stage['aphid_threshold_pct']}% of "
                        f"plants." if stage.get("aphid_threshold_pct") else ""))
    green = (brief.get("satellite_greenness") or {}).get("latest") or []
    if green:
        last = green[-1]
        line = f"Satellite greenness (NDVI) {last['ndvi']:g} on {last['date']}"
        if last.get("field_normal") is not None:
            gap = last["ndvi"] - last["field_normal"]
            word = ("about normal" if abs(gap) < 0.05 else
                    "greener than normal" if gap > 0 else "less green than normal")
            line += f", {word} for this field then ({last['field_normal']:g})"
        if len(green) > 1:
            trend = green[-1]["ndvi"] - green[0]["ndvi"]
            line += (f"; {'rising' if trend > 0.03 else 'falling' if trend < -0.03 else 'steady'}"
                     f" since {green[0]['date']}")
        lines.append(line + ".")
    trees = brief.get("drone_trees_ai")
    if trees:
        lines.append(f"Drone, trained tree model ({trees.get('flown')}): {trees.get('trees')} "
                     f"trees, {trees.get('need_a_look')} need a look, "
                     f"{trees.get('gaps_missing_trees')} gaps where a tree is missing, typical "
                     f"height {trees.get('typical_height_m')} m.")
    flags = brief.get("drone_flags")
    if flags:
        lines.append(f"Drone ({flags.get('flown')}): {flags.get('stressed')} of "
                     f"{flags.get('judged')} parts of the field look stressed, "
                     f"{flags.get('missing_plants')} have plants missing.")
    for finding in brief.get("drone_ground") or []:
        lines.append(f"Drone ground: {str(finding).rstrip('.')}.")
    thermal = brief.get("thermal_camera")
    if thermal:
        for patch in thermal.get("warm_patches") or []:
            lines.append(f"Thermal camera ({thermal.get('flown')}): a warm patch in the "
                         f"{patch.get('where')}, {patch.get('area_m2')} m2, "
                         f"{patch.get('hotter_than_canopy_c')} C hotter than the rest; "
                         f"{patch.get('chance_pest_or_disease_pct')}% chance it is a pest or "
                         f"disease.")
    aphid = brief.get("last_aphid_count")
    if stage and "sugarcane_aphid" in (stage.get("watch_for") or []) and not aphid:
        lines.append("No sugarcane aphid count has been logged yet, so nobody knows if the "
                     "threshold is reached.")
    if aphid:
        lines.append(f"Last sugarcane aphid count ({aphid.get('day')}): "
                     f"{aphid.get('percent_infested')}% of plants, {aphid.get('verdict')}.")
    if brief.get("farmer_log"):
        lines.append("Farmer's log: " + "; ".join(brief["farmer_log"]) + ".")
    return lines


def check(advice: dict, brief: dict, lang: str = "en") -> list[str]:
    """Why an answer cannot go out; empty when it can."""
    from .parse import normalize

    problems = []
    message = str(advice.get("message") or "").strip()
    said = normalize(message) + " "
    if not message:
        return ["no message"]
    if len(message) > MAX_MESSAGE_CHARS:
        problems.append(f"message is {len(message)} characters; at most {MAX_MESSAGE_CHARS}")
    action = expected_action(brief)
    book = brief.get("water_checkbook") or {}
    days, by = book.get("days_until_water"), book.get("water_by")
    if action == "water_now" and not any(w in said for w in _NOW_WORDS):
        problems.append("the DECISION is to water now and the message does not say so")
    if action in ("water_soon", "no_water_yet") and days is not None:
        marks = {str(days)}
        if by:
            d = date.fromisoformat(by)
            marks |= {str(d.day), _WEEKDAYS["en"][d.weekday()], _WEEKDAYS["es"][d.weekday()]}
        if not any(re.search(rf"(?<![\w.]){re.escape(m)}(?![\w.])", said) for m in marks):
            problems.append(f"the message does not give the DECISION's day ({days} days, by {by})")
    if action == "not_irrigated" and any(w in said for w in _WATER_WORDS):
        problems.append("the field is rainfed and the message says to water it")
    written = " ".join(facts(brief))
    known_days = _days_named(written)
    for day in _days_named(message + " " + str(advice.get("check_first") or "")):
        if day not in known_days:
            problems.append(f"the date {day[0]}/{day[1]} is not in the FACTS")
    known = _numbers(written)
    for number in _numbers(message + " " + str(advice.get("check_first") or "")):
        if (number.is_integer() and number <= 31) or _near_any(number, known):
            continue
        problems.append(f"the number {number:g} is not in the FACTS")
    return problems


_MONTHS = {name: number for number, names in enumerate((
    ("january", "jan", "enero", "ene"), ("february", "feb", "febrero"),
    ("march", "mar", "marzo"), ("april", "apr", "abril", "abr"), ("may", "mayo"),
    ("june", "jun", "junio"), ("july", "jul", "julio"), ("august", "aug", "agosto", "ago"),
    ("september", "sep", "sept", "septiembre", "setiembre"), ("october", "oct", "octubre"),
    ("november", "nov", "noviembre"), ("december", "dec", "diciembre", "dic")), start=1)
    for name in names}


def _days_named(value: str) -> set[tuple[int, int]]:
    """Every (month, day) a text names: 2026-09-23, 9/23, September 23, 23 de septiembre."""
    from .parse import normalize

    said = normalize(value)
    days = {(int(m), int(d)) for m, d in re.findall(r"\b\d{4}-(\d{1,2})-(\d{1,2})\b", said)}
    days |= {(int(m), int(d)) for m, d in re.findall(r"(?<![\d.-])(\d{1,2})/(\d{1,2})\b", said)}
    month = "|".join(sorted(_MONTHS, key=len, reverse=True))
    days |= {(_MONTHS[m], int(d)) for m, d in re.findall(rf"\b({month})\.? (\d{{1,2}})\b", said)}
    days |= {(_MONTHS[m], int(d)) for d, m in
             re.findall(rf"\b(\d{{1,2}}) (?:de )?({month})\b", said)}
    return {(m, d) for m, d in days if 1 <= m <= 12 and 1 <= d <= 31}


def _numbers(value: str) -> list[float]:
    found = []
    for token in re.findall(r"\d[\d,]*(?:\.\d+)?", value):
        try:
            found.append(float(token.replace(",", "")))
        except ValueError:
            pass
    return found


def _near_any(number: float, known: list[float]) -> bool:
    return any(abs(number - k) <= max(0.051, 0.03 * abs(k)) for k in known)


def brief_key(brief: dict, model: str) -> str:
    return hashlib.sha256((model + json.dumps(brief, sort_keys=True, default=str))
                          .encode("utf-8")).hexdigest()[:16]


def recommend(model: LocalAI, brief: dict, *, lang: str, now: str) -> Advice | None:
    """The model's advice for one field, checked; None when it fails the checks twice."""
    prompt = (f"LANGUAGE: {'Spanish' if lang == 'es' else 'English'}\n"
              "FACTS:\n" + "\n".join(f"- {line}" for line in facts(brief)))
    action = expected_action(brief)
    days = (brief.get("water_checkbook") or {}).get("days_until_water")
    for attempt in (1, 2):
        try:
            raw = model.json(ADVICE_SYSTEM, prompt, ADVICE_SCHEMA, max_tokens=300)
        except AIError as exc:
            log.warning("AI advice: %s", exc)
            return None
        problems = check(raw, brief, lang)
        if not problems:
            return Advice(action or "", days if action in ("water_soon", "no_water_yet",
                                                             "water_now") else None,
                          text.gsm_safe(str(raw["message"]).strip()),
                          text.gsm_safe(str(raw.get("check_first") or "").strip()),
                          [text.gsm_safe(str(r)) for r in (raw.get("reasons") or [])][:4],
                          raw.get("confidence") or "medium", model.model, now,
                          checked=["it keeps the water balance's decision and day",
                                   "every number comes from the readings",
                                   "it fits in two text messages"])
        log.info("AI advice attempt %s refused: %s", attempt, "; ".join(problems))
        prompt += ("\n\nYOUR LAST ANSWER WAS REFUSED: " + "; ".join(problems)
                   + ". Write it again, fixing that.")
    return None


# --------------------------------------------------------------------------- #
# Kept advice, so a text and the WHY page say the same thing
# --------------------------------------------------------------------------- #


def _cache_path(settings, field_id: str) -> Path:
    return settings.sms_dir / "ai" / f"{field_id}.json"


def kept(settings, field_id: str, key: str) -> Advice | None:
    """The advice written for exactly these readings, if there is one."""
    path = _cache_path(settings, field_id)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Advice.from_dict(saved["advice"]) if saved.get("key") == key else None


def keep(settings, field_id: str, key: str, advice: Advice, brief: dict) -> None:
    path = _cache_path(settings, field_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": key, "advice": advice.to_dict(), "brief": brief},
                               indent=1, ensure_ascii=False, default=str), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Everything known about a field, for the model to read
# --------------------------------------------------------------------------- #


def field_brief(settings, farmer, item, events, today: date) -> dict | None:
    """One field's readings as a short JSON document; None without a checkbook.

    Short on purpose: a 3B model on a laptop reads about a hundred words a second,
    and every reading here is one it should weigh.
    """
    from . import status as status_mod
    from .explain import trees_ai_report
    from .store import plan_of

    s = item.status
    if s is None:
        return None
    record = item.field
    planted = max((e.day for e in events if e.kind == "planted" and e.voided_at is None),
                  default=None)
    brief: dict = {
        "today": today.isoformat(),
        "field": {
            "name": record.name,
            "crop": text.crop_name(record.crop, "en", record.crop_name),
            "acres": round(record.acres or record.acres_said or 0, 1) or None,
            "watered_by": ("rainfed (not irrigated)" if s.method == "none"
                           else text.method_name(s.method, "en")),
            "planted": planted.isoformat() if planted else None,
        },
        "water_checkbook": {
            "status": s.status,
            "days_until_water": s.days_left,
            "days_range": s.days_range,
            "water_by": s.water_by,
            "soil_water_left_pct": None if s.pct_left is None else round(s.pct_left),
            "water_left_in": s.water_left_in,
            "crop_use_in_per_day": s.use_in_day,
            "refill_in": s.refill_gross_in,
            "last_irrigation": s.last_irrigation,
            "last_rain": s.last_rain,
            "stressed_days_last_30": s.stressed_days_30,
            "soil": (s.soil or {}).get("name"),
            "weather_through": s.weather_through,
            "confidence": s.confidence,
        },
    }
    if s.stage:
        st = s.stage
        brief["sorghum_stage"] = {
            "stage": st.get("stage_label"), "days_after_planting": st.get("days_after_planting"),
            "next_stage": st.get("next_stage"), "next_date": st.get("next_date"),
            "aphid_threshold_pct": st.get("aphid_threshold_pct"),
            "watch_for": st.get("watch"),
        }
    satellite = _greenness(item)
    if satellite:
        brief["satellite_greenness"] = satellite
    log_lines = [f"{e.day.isoformat()} {e.kind}"
                 + (f" {e.inches:g} in" if e.inches is not None else "")
                 for e in sorted(events, key=lambda e: e.day)
                 if e.voided_at is None and e.kind in ("planted", "irrigated", "rain",
                                                       "harvested")][-6:]
    if log_lines:
        brief["farmer_log"] = log_lines
    scouting = [e for e in events if e.kind == "scouting" and e.voided_at is None]
    if scouting:
        last = max(scouting, key=lambda e: e.day)
        try:
            note = json.loads(last.note or "{}")
        except ValueError:
            note = {}
        brief["last_aphid_count"] = {"day": last.day.isoformat(),
                                     "percent_infested": note.get("percent"),
                                     "verdict": note.get("verdict")}
    plan = plan_of(farmer, record)
    if plan in text.FLYING_PLANS:
        flags = status_mod.latest_flags(settings, record.id)
        trees = trees_ai_report(settings, flags)
        if trees:
            brief["drone_trees_ai"] = {
                "flown": trees.get("flown_on"), "trees": trees.get("trees"),
                "need_a_look": trees.get("needs_a_look"), "gaps_missing_trees": trees.get("gaps"),
                "typical_height_m": (trees.get("height_m") or {}).get("median")}
        elif flags:
            brief["drone_flags"] = {
                "flown": flags.get("flown_on"), "judged": flags.get("n_judged"),
                "stressed": (flags.get("n_stressed") or 0) + (flags.get("n_dead") or 0),
                "missing_plants": flags.get("n_missing"), "unit": flags.get("unit_type")}
        terrain = status_mod.latest_terrain(settings, record.id)
        if terrain and terrain.get("advice"):
            brief["drone_ground"] = [a.get("finding") for a in terrain["advice"][:2]]
    if plan == "thermal":
        thermal = status_mod.latest_thermal(settings, record.id)
        if thermal:
            patches = sorted(thermal.get("patches") or [], key=lambda p: -p.get("chance", 0))
            brief["thermal_camera"] = {
                "flown": thermal.get("flown_on"),
                "warm_patches": [{"chance_pest_or_disease_pct": round(p.get("chance", 0) * 100),
                                  "where": p.get("where"),
                                  "hotter_than_canopy_c": p.get("above_c"),
                                  "area_m2": round(p.get("area_m2") or 0)}
                                 for p in patches[:3]]}
    return brief


def _greenness(item) -> dict | None:
    """The last satellite readings, against what this field usually shows then."""
    frame = item.ndvi
    if frame is None or getattr(frame, "empty", True):
        return None
    recent = frame.sort_values("date").tail(3)
    readings = []
    for _, row in recent.iterrows():
        entry = {"date": str(row["date"])[:10], "ndvi": round(float(row["median"]), 2)}
        base = item.baseline
        if base is not None and not base.empty:
            near = base.iloc[(base["doy"] - int(row["doy"])).abs().argsort()[:1]]
            if not near.empty:
                entry["field_normal"] = round(float(near["median"].iloc[0]), 2)
        readings.append(entry)
    return {"latest": readings}
