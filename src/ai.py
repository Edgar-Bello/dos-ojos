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
        "date": {"type": "string"},
        "inches": {"type": ["number", "null"]},
        "crop": {"type": "string", "enum": list(CROPS)},
        "answer": {"type": "string"},
    },
    "required": ["intent", "field", "date", "inches", "crop", "answer"],
}

UNDERSTAND_SYSTEM = """You read text messages that farmers in the Rio Grande Valley send \
to Dos Ojos, a service that tells them when to water. Messages are in Spanish or English, \
often short, misspelled or informal. Decide what the farmer means and answer in JSON.

intent, pick one:
- irrigated: they watered a field (regue, regamos, le echamos agua, watered)
- rain: it rained (llovio, lluvia, rained); inches if said
- harvested: they harvested or cut a field
- planted: they planted or sowed a field
- status: they ask how their fields are or whether to water, with no other detail
- explain: they want the charts or the reasons (porque, why, graficas)
- fields: they want the list of their fields
- new_field: they want to add a field
- map: they want to draw or fix a field's map
- drone: they want to send drone photos
- stage: how far along the sorghum is
- aphid: sugarcane aphid (pulgon amarillo) counts or questions
- plan: change what they use (satellite, drone, thermal)
- crop: change which crop a field has
- undo: remove the last thing they sent
- help: they want the options
- question: any other question about their fields you can answer from FIELDS below
- other: anything else (greetings, thanks, unclear, off topic)

field: copy one name from FIELDS exactly, or "" when none is meant or you are unsure.
date: YYYY-MM-DD for the day it happened, worked out from TODAY ("ayer" = yesterday, \
"anoche" = last night = yesterday); "" when no day is said.
inches: the inches of water or rain as a number, or null when not said. Do not guess.
crop: for planted or crop, one of sorghum cotton corn sugarcane citrus soybean, else "".
answer: only for question, a short reply (under 240 characters) in the farmer's \
language using ONLY the facts in FIELDS; if FIELDS do not say, reply that you will pass \
the question to the team. Otherwise "".
Never invent a field, a date or an amount."""


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


def understand(model: LocalAI, message: str, *, lang: str, today: date,
               fields: list[dict]) -> Understood:
    """What a text the rules could not place is asking for."""
    listing = "\n".join(
        f"- {f['name']}: {f.get('summary') or ''}".rstrip(": ") for f in fields) or "(none)"
    prompt = (f"TODAY: {today.isoformat()} ({today.strftime('%A')})\n"
              f"FARMER'S LANGUAGE: {'Spanish' if lang == 'es' else 'English'}\n"
              f"FIELDS:\n{listing}\n\nMESSAGE: {message}")
    raw = model.json(UNDERSTAND_SYSTEM, prompt, UNDERSTAND_SCHEMA, max_tokens=200)
    intent = raw.get("intent") if raw.get("intent") in INTENTS else "other"
    names = {f["name"].lower(): f["name"] for f in fields}
    named = names.get(str(raw.get("field") or "").strip().lower())
    day = None
    try:
        day = date.fromisoformat(str(raw.get("date") or ""))
        if not today - timedelta(days=400) <= day <= today:
            day = None          # a day to come, or years ago: the rules ask again
    except ValueError:
        pass
    inches = raw.get("inches")
    inches = float(inches) if isinstance(inches, (int, float)) and 0 < inches <= 15 else None
    crop = raw.get("crop") if raw.get("crop") in CROPS[:-1] else None
    answer = str(raw.get("answer") or "").strip()[:MAX_MESSAGE_CHARS] or None
    return Understood(intent, named, day, inches, crop,
                      answer if intent == "question" else None)


# --------------------------------------------------------------------------- #
# The recommendation
# --------------------------------------------------------------------------- #

ACTIONS = ("water_now", "water_soon", "no_water_yet", "not_irrigated", "harvested")

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "water_in_days": {"type": "integer"},
        "message": {"type": "string"},
        "check_first": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["action", "water_in_days", "message", "check_first", "reasons",
                 "confidence"],
}

ADVICE_SYSTEM = """You are the farm advisor of Dos Ojos, writing to a farmer in the Rio \
Grande Valley by text message. You get every reading about one field as JSON: the water \
checkbook (a soil-water balance from the soil survey, daily weather and the crop), the \
satellite's greenness against the field's own normal, the growth stage, what the drone and \
the trained tree model found, thermal camera patches, and what the farmer logged.

Write the recommendation. Rules:
- The checkbook's days until water are the backbone: keep your action and day consistent \
with it. Use the other readings to say what to check or to add a caution, not to override it.
- Name the one thing to do first. Mention a drone, tree, thermal or satellite finding only \
when it changes what the farmer should do or check.
- Use only numbers that appear in the readings. Round them the way they appear.
- message: at most 280 characters, in LANGUAGE, plain words a farmer uses, no jargon, no \
emoji, do not start with the field's name.
- action: water_now (0 days), water_soon (1-3 days), no_water_yet (more than 3 days), \
not_irrigated (rainfed), harvested.
- water_in_days: the day count your message gives, or -1 when not watering.
- check_first: one short thing to go and look at in the field, in LANGUAGE, or "".
- reasons: two to four short reasons in LANGUAGE, each naming the reading it comes from.
- confidence: how sure you are, from the checkbook's own confidence and how much the \
readings agree."""


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
    """The action the checkbook alone calls for."""
    book = brief.get("water_checkbook") or {}
    if book.get("status") == "harvested":
        return "harvested"
    if (brief.get("field") or {}).get("watered_by") == "rainfed (not irrigated)":
        return "not_irrigated"
    days = book.get("days_until_water")
    if days is None:
        return None
    return "water_now" if days <= 0 else "water_soon" if days <= 3 else "no_water_yet"


def check(advice: dict, brief: dict) -> list[str]:
    """Why an answer cannot go out; empty when it can."""
    problems = []
    action = advice.get("action")
    expected = expected_action(brief)
    book = brief.get("water_checkbook") or {}
    days = book.get("days_until_water")
    low, high = (book.get("days_range") or [days, days]) if days is not None else (None, None)
    if expected and action != expected:
        near = {expected, action} == {"water_soon", "no_water_yet"} and days in (3, 4)
        if not near:
            problems.append(f"action {action} disagrees with the checkbook ({expected})")
    given = advice.get("water_in_days")
    if action in ("water_now", "water_soon", "no_water_yet") and days is not None:
        if not isinstance(given, int) or not (min(low, days) - 1 <= given <= max(high, days) + 1):
            problems.append(f"water_in_days {given} is outside the checkbook's "
                            f"{low}-{high} days")
    message = str(advice.get("message") or "").strip()
    if not message:
        problems.append("no message")
    if len(message) > MAX_MESSAGE_CHARS:
        problems.append(f"message is {len(message)} characters; at most {MAX_MESSAGE_CHARS}")
    known = _numbers(json.dumps(brief))
    for number in _numbers(message + " " + str(advice.get("check_first") or "")):
        if (number.is_integer() and number <= 31) or _near_any(number, known):
            continue
        problems.append(f"the number {number:g} is not in the readings")
    return problems


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
              f"READINGS:\n{json.dumps(brief, indent=1, ensure_ascii=False, default=str)}")
    for attempt in (1, 2):
        try:
            raw = model.json(ADVICE_SYSTEM, prompt, ADVICE_SCHEMA, max_tokens=450)
        except AIError as exc:
            log.warning("AI advice: %s", exc)
            return None
        problems = check(raw, brief)
        if not problems:
            days = raw.get("water_in_days")
            return Advice(raw["action"], days if isinstance(days, int) and days >= 0 else None,
                          text.gsm_safe(str(raw["message"]).strip()),
                          text.gsm_safe(str(raw.get("check_first") or "").strip()),
                          [text.gsm_safe(str(r)) for r in (raw.get("reasons") or [])][:4],
                          raw.get("confidence") or "medium", model.model, now,
                          checked=["action agrees with the checkbook",
                                   "day within the checkbook's range",
                                   "every number comes from the readings",
                                   "fits in two text messages"])
        log.info("AI advice attempt %s refused: %s", attempt, "; ".join(problems))
        prompt += ("\n\nYOUR LAST ANSWER WAS REFUSED: " + "; ".join(problems)
                   + ". Answer again, fixing that.")
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
