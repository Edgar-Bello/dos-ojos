"""The conversation: what to ask next, and what an answer means.

A farmer's ``state`` is the question waiting for an answer. It is kept in the
database between texts, so a conversation can stop for a day and pick up where
it left off. There are two kinds of questions:

- Field questions, asked in order until a field is complete: its name, acres,
  where it is, the crop, the planting date, how it is watered, the side the
  water comes in from, and the irrigations since planting. Anything already
  known is skipped, so an interrupted farmer simply gets the next missing one.
- Actions a farmer starts with a word (REGUE, LLUVIA, COSECHA, SEMBRE, DRON,
  MAPA, a photo), whose blanks (which field, which day, how much) are filled
  from the message itself and asked for only when missing.

Every date and amount is read back, with the weekday, for a yes or a no before
it is stored. That read-back is what lets the numbers be trusted.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta

from dosojos_sat import stages as sat_stages

from . import parse, store, text
from . import status as status_mod
from .config import LINK_DAYS, Settings
from .store import Farmer, FieldRow
from .text import say

log = logging.getLogger(__name__)

#: Irrigations asked for when setting a field up, counting back from the last one.
MAX_HISTORY = 6
#: Largest believable amounts, in inches.
MAX_IRRIGATION_IN, MAX_RAIN_IN = 12.0, 15.0

#: The blanks each action needs, in the order they are asked for.
SLOTS: dict[str, tuple[str, ...]] = {
    "irrigated": ("fields", "day"),
    "rain": ("fields", "day", "inches"),
    "harvested": ("fields", "day"),
    "planted": ("fields", "crop", "day"),
    "ticket": ("fields", "day", "inches"),
    "photo": ("fields",),
    "drone": ("fields", "bare", "day"),
    "map": ("fields",),
}
#: Actions that may cover several fields at once ("Todos").
MULTI = ("irrigated", "rain")
#: Actions read back for a yes or a no before they are stored.
CONFIRMED = ("irrigated", "rain", "harvested", "planted", "ticket")

_NONE_WORDS = {"nada", "none", "no", "ninguno", "ninguna", "nunca", "never", "todavia no",
               "not yet", "no ha", "no hemos regado", "no he regado", "no se ha regado",
               "no se a regado", "aun no", "sin riego"}
_THANKS = {"gracias", "muchas gracias", "thanks", "thank you", "ok", "okay", "ok gracias",
           "listo", "va", "sale", "bien", "perfecto", "great", "👍"}


@dataclass
class Media:
    """A photo that came with a text, already saved to disk."""

    path: str
    content_type: str = "image/jpeg"


@dataclass
class Inbound:
    """One text from a farmer, however it arrived."""

    phone: str
    body: str = ""
    media: list[Media] = dc_field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    channel: str = "sms"
    message_id: int | None = None


class Bot:
    """Answers texts, one at a time, against the SMS database."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, *,
                 now: datetime | None = None, resolve: parse.Resolver | None = None,
                 water: status_mod.Water | None = None):
        self.conn = conn
        self.settings = settings
        self.now = now or settings.now()
        self.today = self.now.date()
        self.resolve = resolve if resolve is not None else parse.resolve_link
        self.water = water or status_mod.Water(settings)

    def handle(self, message: Inbound) -> list[str]:
        """The replies to one text, in the farmer's language, ready for GSM."""
        farmer = (store.get_farmer(self.conn, message.phone)
                  or store.add_farmer(self.conn, message.phone, channel=message.channel))
        replies = Turn(self, farmer, message).run()
        store.save_farmer(self.conn, farmer)
        return [text.gsm_safe(reply) for reply in replies]

    def prompt_for(self, farmer: Farmer) -> str | None:
        """The question a farmer is being asked, for reminders and the simulator."""
        return Turn(self, farmer, Inbound(farmer.phone))._prompt(farmer.state)

    def ask(self, farmer: Farmer, question: str, field_id: str, *,
            save: bool = True) -> str | None:
        """Put a field question to an idle farmer (a reminder); returns the prompt.

        With ``save=False`` it only shows what would be asked.
        """
        if not save:
            farmer = copy.deepcopy(farmer)
        turn = Turn(self, farmer, Inbound(farmer.phone))
        turn.ctx["field"] = field_id
        turn._ask_missing(question)
        if save:
            store.save_farmer(self.conn, farmer)
        return turn.out[-1] if turn.out else None

    def checkin(self, farmer: Farmer, field_id: str) -> None:
        """Wait for the answer to "have you watered?"."""
        farmer.state = "checkin"
        farmer.context = {k: v for k, v in farmer.context.items() if k == "resume"}
        farmer.context["checkin"] = field_id
        store.save_farmer(self.conn, farmer)


class Turn:
    """One incoming text and everything it changes."""

    def __init__(self, bot: Bot, farmer: Farmer, message: Inbound):
        self.bot = bot
        self.conn = bot.conn
        self.today = bot.today
        self.f = farmer
        self.msg = message
        self.ctx = farmer.context
        self.body = (message.body or "").strip()
        self.norm = parse.normalize(self.body)
        self.out: list[str] = []
        self._fields: list[FieldRow] | None = None

    # ---- small helpers -----------------------------------------------------

    @property
    def lang(self) -> str:
        return self.f.language

    def say(self, key: str, **values: object) -> None:
        self.out.append(say(key, self.lang, **values))

    def fields(self) -> list[FieldRow]:
        if self._fields is None:
            self._fields = store.fields_of(self.conn, self.f.phone)
        return self._fields

    def _field(self, field_id: str | None = None) -> FieldRow | None:
        field_id = field_id or self.ctx.get("field")
        return store.get_field(self.conn, field_id) if field_id else None

    def _save(self, record: FieldRow) -> None:
        store.save_field(self.conn, record)
        self._fields = None

    def _names(self, ids: list[str]) -> str:
        by_id = {f.id: f.name for f in self.fields()}
        return text.listing([by_id.get(i, i) for i in ids], self.lang)

    def _day(self, iso: str) -> str:
        return text.day(date.fromisoformat(iso), self.lang, self.today)

    def _to_idle(self) -> None:
        self.f.state = "idle"
        for key in ("action", "pick", "photos", "checkin", "field", "first", "aphid",
                    "sorghum", "maturity_only"):
            self.ctx.pop(key, None)

    def _flag_for_team(self) -> None:
        if self.msg.message_id:
            store.mark_unread(self.conn, self.msg.message_id)

    # ---- the way in ---------------------------------------------------------

    def run(self) -> list[str]:
        if parse.is_stop(self.body):
            if not self.f.opted_out_at:
                self.f.opted_out_at = store.now_iso()
                self.say("stopped")
            return self.out
        if self.f.opted_out_at:
            if parse.is_start(self.body):
                self.f.opted_out_at = None
                self.say("restarted")
                if self.f.state not in ("new", "idle"):
                    self._reask()
            return self.out
        if self.f.state == "new":
            self.out.append(text.WELCOME)
            self.f.state = "lang"
            return self.out
        if parse.is_help(self.body):
            self._help()
            self._reask()
            return self.out
        command = parse.command(self.body)
        if command in ("lang_es", "lang_en") and self.f.state != "lang":
            self.f.lang = "es" if command == "lang_es" else "en"
            self.say("lang_set")
            self._reask()
            return self.out
        if command == "menu" and self.f.state != "lang":
            self._to_idle()
            self.say("menu")
            return self.out
        handler = getattr(self, "_on_" + self.f.state.replace(":", "_"), None)
        if handler is None:
            log.warning("%s was in unknown state %r; starting over from idle",
                        self.f.phone, self.f.state)
            self._to_idle()
            handler = self._on_idle
        handler()
        return self.out

    def _help(self) -> None:
        contact = self.bot.settings.team_contact
        self.say("help", contact=say("contact", self.lang, contact=contact) if contact else "")

    def _reask(self) -> None:
        prompt = self._prompt(self.f.state)
        if prompt:
            self.out.append(prompt)

    def _retry(self, hint: str | None = None, *, reask: bool = True) -> None:
        """An answer that does not fit the question: a command, or ask again."""
        command = parse.command(self.body)
        if command and command not in ("menu", "lang_es", "lang_en"):
            self._interrupt(command)
            return
        if self.msg.media:
            self._flag_for_team()
            self.say("photo_saved")
        if hint:
            self.say(hint)
        if reask:
            self._reask()

    def _interrupt(self, command: str) -> None:
        """A command in the middle of a question: answer it, then carry on.

        A quick look (AGUA, CAMPOS) is answered and the question asked again. An
        action (REGUE, ...) takes over; the field being set up is resumed after.
        """
        if command in ("status", "fields"):
            self._command(command)
            self._reask()
            return
        if self.ctx.get("field"):
            self.ctx["resume"] = self.ctx["field"]
        self._to_idle()
        self._command(command)

    # ---- the farmer ---------------------------------------------------------

    def _on_lang(self) -> None:
        choice = parse.menu_choice(self.body, 2)
        lang = {1: "es", 2: "en"}.get(choice or 0) or (
            "es" if self.norm in ("es", "espanol", "spanish", "castellano") else
            "en" if self.norm in ("en", "english", "ingles") else None)
        if lang is None:
            self.out.append(text.WELCOME)
            return
        self.f.lang = lang
        self._ask("name")

    def _on_name(self) -> None:
        name = parse.name(self.body)
        if not name:
            self._reask()
            return
        self.f.name = name
        self._ask("consent")

    def _on_consent(self) -> None:
        answer, _ = parse.yes_no(self.body)
        if answer is None:
            self._retry("yes_no", reask=False)
            return
        self.f.alerts = answer
        self.f.consent_at = store.now_iso() if answer else None
        self.say("consent_yes" if answer else "consent_no")
        self._ask("plan")

    def _on_plan(self) -> None:
        chosen = parse.plan(self.body)
        if chosen is None:
            self._retry("menu_number")
            return
        self._set_plan(chosen)
        if self.fields():
            self._to_idle()
            return
        self.ctx["first"] = True
        self._ask("field_name")

    def _set_plan(self, chosen: str) -> None:
        """Record the plan and say what it means, licence included.

        A farmer picking a drone is told what the law asks before they spend
        anything. How they get the photos is theirs to decide: fly it themselves
        with a licence, or have someone who holds one fly it for them.
        """
        self.f.plan = chosen
        self.say("plan_" + chosen, plan=text.plan_name(chosen, self.lang))
        if chosen in text.FLYING_PLANS:
            self.say("plan_license")

    # ---- a field ------------------------------------------------------------

    def _ask(self, state: str) -> None:
        self.f.state = state
        self._reask()

    def _missing(self, record: FieldRow) -> list[str]:
        """The field questions still open, in the order they are asked."""
        answers = record.answers
        todo = []
        if record.acres_said is None and record.acres is None and "acres" not in answers:
            todo.append("acres")
        if record.lat is None and not record.place and record.outline is None:
            todo.append("location")
        if not record.crop:
            todo.append("crop")
        elif record.crop == "other" and not record.crop_name and "crop_other" not in answers:
            todo.append("crop_other")
        growing = bool(record.crop) and record.crop != "none"
        if (growing and record.crop != "citrus" and "planted" not in answers
                and self._planted(record) is None):
            todo.append("planted")
        # How long a sorghum hybrid runs moves black layer by weeks: worth one question.
        if record.crop == "sorghum" and "maturity" not in answers:
            todo.append("maturity")
        if growing and not record.irrigation:
            todo.append("method")
        if (record.irrigation in text.SURFACE and not record.water_enters
                and "side" not in answers):
            todo.append("side")
        if (growing and record.irrigation and record.irrigation != "none"
                and "last_irrigation" not in answers and not self._irrigated_lately(record)):
            todo.append("last_irrigation")
        return todo

    def missing_all(self, record: FieldRow) -> list[str]:
        """Everything still missing, the map included."""
        todo = self._missing(record)
        if record.outline is None and "location" not in todo:
            todo.insert(0, "map")
        return todo

    def _planted(self, record: FieldRow) -> date | None:
        days = [e.day for e in store.events_for(self.conn, record.id)
                if e.kind == "planted" and 0 <= (self.today - e.day).days <= 400]
        return max(days) if days else None

    def _irrigated_lately(self, record: FieldRow) -> bool:
        since = self._planted(record) or (self.today - timedelta(days=120))
        return any(e.kind == "irrigated" and e.day >= since
                   for e in store.events_for(self.conn, record.id))

    def _next_question(self) -> None:
        record = self._field()
        if record is None:
            self._to_idle()
            return
        todo = self._missing(record)
        if todo:
            self._ask_missing(todo[0])
            return
        self.say("field_done", field=record.name)
        if record.outline is None and record.lat is not None:
            self.say("field_done_map", link=self._map_link(record))
        if self.ctx.get("resume") == record.id:
            self.ctx.pop("resume")
        self._to_idle()
        self._resume()

    def _ask_missing(self, question: str) -> None:
        record = self._field()
        if question == "planted":
            cane = record.crop == "sugarcane"
            self._begin({"kind": "planted", "fields": [record.id], "crop": record.crop,
                         "crop_name": record.crop_name, "window": "planted",
                         "prompt": "planted_cane" if cane else "planted", "then": "next",
                         "unknown_ok": "planted",
                         "note": "planted or last cut" if cane else ""})
            self._ask("a:day")
        elif question == "last_irrigation":
            self._begin({"kind": "irrigated", "fields": [record.id], "window": "last_irrigation",
                         "prompt": "last_irrigation", "then": "history",
                         "none_ok": "last_irrigation", "unknown_ok": "last_irrigation"})
            self._ask("a:day")
        else:
            self._ask("f:" + question)

    def _resume(self) -> None:
        """Back to the field whose questions an action interrupted."""
        field_id = self.ctx.pop("resume", None)
        record = self._field(field_id) if field_id else None
        if record and self._missing(record):
            self.ctx["field"] = record.id
            self.say("resume", field=record.name)
            self._next_question()

    def _on_field_name(self) -> None:
        name = parse.name(self.body)
        if not name or (parse.command(self.body) and len(parse.words(self.body)) == 1):
            self._retry()
            return
        record = store.add_field(self.conn, self.f.phone, name)
        self._fields = None
        self.ctx["field"] = record.id
        self._next_question()

    def _on_f_acres(self) -> None:
        record = self._field()
        command = parse.command(self.body)
        if command:        # "REGUE campo norte 3" is not 3 acres
            self._interrupt(command)
            return
        value = parse.acres(self.body)
        if value is None:
            self._retry()
            return
        if value == "?":
            record.answers["acres"] = "unknown"
        else:
            record.acres_said = float(value)
        self._save(record)
        self._next_question()

    def _on_f_location(self) -> None:
        record = self._field()
        place = parse.find_place(self.body, lat=self.msg.lat, lon=self.msg.lon,
                                 resolve=self.bot.resolve)
        if place == "unresolved":
            self.say("location_link_failed")
            return
        if isinstance(place, parse.Place):
            if not place.in_conus:
                self.say("location_far", lat=f"{place.lat:.4f}", lon=f"{place.lon:.4f}")
                return
            record.lat, record.lon = place.lat, place.lon
            self._save(record)
            self.say("location_pin", lat=f"{place.lat:.5f}", lon=f"{place.lon:.5f}",
                     link=self._map_link(record))
            self._next_question()
            return
        if len(parse.words(self.body)) >= 2 and not parse.command(self.body):
            record.place = self.body[:300]
            self._save(record)
            self._flag_for_team()
            self.say("location_described")
            self._next_question()
            return
        self._retry("location_again", reask=False)

    def _on_f_crop(self) -> None:
        record = self._field()
        key, other = parse.crop(self.body)
        if key is None:
            self._retry("menu_number")
            return
        record.crop, record.crop_name = key, other
        self._save(record)
        self._next_question()

    def _on_f_crop_other(self) -> None:
        record = self._field()
        name = parse.name(self.body, limit=30)
        if not name:
            self._retry()
            return
        record.crop_name = name
        self._save(record)
        self._next_question()

    def _on_f_maturity(self) -> None:
        record = self._field()
        command = parse.command(self.body)
        value = parse.maturity(self.body)
        if value is None:
            if command and command != "maturity":
                self._interrupt(command)
                return
            self._retry("menu_number")
            return
        record.answers["maturity"] = "unknown" if value == "?" else value
        self._save(record)
        only = self.ctx.pop("maturity_only", False)
        if value == "?":
            self.say("maturity_unknown")
        elif only:
            self.say("maturity_saved", field=record.name,
                     maturity=text.pick(text.MATURITIES[value], self.lang))
        if only:
            self._to_idle()
            return
        self._next_question()

    def _on_f_method(self) -> None:
        record = self._field()
        method = parse.method(self.body)
        if method is None:
            self._retry("menu_number")
            return
        record.irrigation = method
        self._save(record)
        self._next_question()

    def _on_f_side(self) -> None:
        record = self._field()
        command = parse.command(self.body)
        if command:
            self._interrupt(command)
            return
        side = parse.side(self.body)
        if side is None:
            self._retry()
            return
        if side == "?":
            record.answers["side"] = "unknown"
        else:
            record.water_enters = side
        self._save(record)
        self._next_question()

    # ---- actions --------------------------------------------------------------

    def _begin(self, action: dict) -> dict:
        action.setdefault("message_id", self.msg.message_id)
        self.ctx["action"] = action
        return action

    def _start(self, kind: str, **preset: object) -> None:
        """An action from a command word, with whatever the message already says."""
        if not self.fields():
            self.say("no_fields")
            self.ctx["first"] = True
            self._ask("field_name")
            return
        action = self._begin({"kind": kind, "window": kind if kind in parse.WINDOWS else 400,
                              **preset})
        if kind == "planted":
            crop, _ = parse.crop_word(self.body)
            if crop:
                action["crop"] = crop
        found = self._read(action)
        if kind == "rain" and not action.get("fields"):
            action["fields"] = [f.id for f in self.fields()]   # rain falls on them all
        if "day" not in action and kind in CONFIRMED:
            if found.options or found.problem:
                self._date_trouble(found)
                return
            # Said without a day, it happened today; the read-back shows it.
            action["day"] = self.today.isoformat()
        self._advance()

    def _read(self, action: dict) -> parse.DateFound:
        """Fill an action's blanks from this message: field names, the day, inches."""
        norm = self.norm
        if not action.get("fields"):
            ids, span = self._named_fields(norm, multi=action["kind"] in MULTI)
            if ids:
                action["fields"] = ids
                norm = parse.without(norm, span)
        found = parse.find_date(norm, self.today, window=action.get("window", 400))
        if found.day:
            action["day"] = found.day.isoformat()
            action["message_id"] = self.msg.message_id
        rest = parse.without(norm, found.span)
        if action["kind"] in ("irrigated", "rain", "ticket"):
            amount = parse.find_amount(rest, self._acres(action.get("fields") or []))
            if amount:
                self._take_amount(action, amount)
        return found

    def _take_amount(self, action: dict, amount: parse.Amount) -> None:
        if amount.hours is not None:
            action["hours"] = amount.hours
        if amount.raw:
            action["raw"] = amount.raw
            action["raw_acres"] = self._acres(action.get("fields") or [])
        if amount.inches is None:
            if action["kind"] != "rain" and (amount.hours is not None or amount.raw):
                # Hours, or acre-feet on a field of unknown size: kept as a note, and
                # the irrigation counted as a full one.
                action["inches"] = None
            return
        limit = MAX_RAIN_IN if action["kind"] == "rain" else MAX_IRRIGATION_IN
        if not 0 < amount.inches <= limit:
            # Asked again rather than stored: 40 is more likely acres or a typo than inches.
            action["odd"] = True
            self.say("inches_odd", inches=text.inches(amount.inches))
            return
        action["inches"] = amount.inches

    def _acres(self, ids: list[str]) -> float | None:
        """Acres under these fields, measured if drawn, else as the farmer said."""
        acres = [f.acres or f.acres_said for f in self.fields() if f.id in ids]
        if not acres or not all(acres):
            return None
        return float(sum(acres))

    def _named_fields(self, norm: str, *, multi: bool) -> tuple[list[str], tuple | None]:
        best = None
        for record in self.fields():
            name = parse.normalize(record.name)
            match = re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", norm) if name else None
            if match and (best is None or len(name) > best[2]):
                best = (record.id, match.span(), len(name))
        if best:
            return [best[0]], best[1]
        match = re.search(r"\b(todos|todas|all|ambos|ambas|both)\b", norm)
        if multi and match:
            return [f.id for f in self.fields()], match.span()
        return [], None

    def _advance(self) -> None:
        """Ask for the next blank, or finish the action."""
        action = self.ctx["action"]
        if action.pop("odd", False):
            self.f.state = "a:inches" if action["kind"] in ("rain", "ticket") else "a:day"
            return
        for slot in SLOTS[action["kind"]]:
            if slot == "fields" and not action.get("fields"):
                if len(self.fields()) == 1:
                    action["fields"] = [self.fields()[0].id]
                    continue
                self._ask("a:fields")
                return
            if slot != "fields" and slot not in action:
                self._ask("a:" + slot)
                return
        self._finish()

    def _finish(self) -> None:
        action = self.ctx["action"]
        kind = action["kind"]
        if kind in CONFIRMED:
            self.f.state = "confirm"
            self._reask()
            return
        self.ctx.pop("action")
        record = self._field(action["fields"][0])
        if kind == "map":
            self.say("map_link", field=record.name, link=self._map_link(record))
        elif kind == "photo":
            for field_id in action["fields"]:
                for path in action.get("evidence_all") or [action.get("evidence")]:
                    store.add_event(self.conn, field_id, self.today, "photo", note="problem photo",
                                    message_id=action.get("message_id"), evidence=path)
            self._flag_for_team()
            self.say("photo_saved")
        elif kind == "drone":
            self._upload_link(record, date.fromisoformat(action["day"]), action.get("bare"))
        self._to_idle()
        self._resume()

    def _on_a_fields(self) -> None:
        action = self.ctx.get("action") or {}
        fields = self.fields()
        multi = action.get("kind") in MULTI and len(fields) > 1
        choice = parse.menu_choice(self.body, len(fields) + (1 if multi else 0))
        if choice:
            action["fields"] = ([f.id for f in fields] if choice > len(fields)
                                else [fields[choice - 1].id])
        else:
            ids, _ = self._named_fields(self.norm, multi=multi)
            if not ids:
                self._retry("menu_number")
                return
            action["fields"] = ids
        self._advance()

    def _on_a_day(self) -> None:
        action = self.ctx.get("action") or {}
        if action.get("unknown_ok") and parse.dont_know(self.body):
            record = self._field(action["fields"][0])
            record.answers[action["unknown_ok"]] = "unknown"
            self._save(record)
            self.ctx.pop("action")
            self._next_question()
            return
        if action.get("none_ok") and self._is_none():
            self._none_answer(action)
            return
        if self.msg.media and action.get("kind") in ("irrigated", "ticket"):
            action["evidence"] = self.msg.media[0].path
            action["kind"] = "ticket"
            self.say("ask_ticket")
            return
        found = self._read(action)
        if "day" not in action:
            if not found.options and not found.problem and parse.command(self.body):
                self._interrupt(parse.command(self.body))
                return
            self._date_trouble(found)
            return
        self._advance()

    def _on_a_inches(self) -> None:
        action = self.ctx.get("action") or {}
        amount = parse.find_amount(self.norm, self._acres(action.get("fields") or []))
        if amount is None:
            self._retry()
            return
        self._take_amount(action, amount)
        if "inches" not in action and not action.get("odd"):
            self._reask()
            return
        self._advance()

    def _on_a_crop(self) -> None:
        action = self.ctx.get("action") or {}
        key, other = parse.crop(self.body)
        if key is None or key == "none":
            self._retry("menu_number")
            return
        action["crop"], action["crop_name"] = key, other
        self._advance()

    def _on_a_bare(self) -> None:
        action = self.ctx.get("action") or {}
        answer, _ = parse.yes_no(self.body)
        if answer is None and not parse.dont_know(self.body):
            self._retry("yes_no", reask=False)
            return
        action["bare"] = answer
        self._advance()

    def _is_none(self) -> bool:
        answer, rest = parse.yes_no(self.body)
        return self.norm.strip(" .,") in _NONE_WORDS or (answer is False and not rest)

    def _none_answer(self, action: dict) -> None:
        record = self._field(action["fields"][0])
        if action["none_ok"] == "last_irrigation":
            record.answers["last_irrigation"] = "none"
            self._save(record)
        self.ctx.pop("action", None)
        self._next_question()

    def _date_trouble(self, found: parse.DateFound) -> None:
        """Two readings, a day to come, a day too long ago, or no day at all."""
        if found.options:
            self.ctx["pick"] = [d.isoformat() for d in found.options[:2]]
            self._ask("pick_date")
            return
        self.f.state = "a:day"
        key = {"future": "date_future", "old": "date_old", "vague": "date_vague"}.get(
            found.problem or "", "date_missing")
        near = text.day(found.near, self.lang, self.today) if found.near else ""
        self.say(key, day=near)

    def _on_pick_date(self) -> None:
        options = self.ctx.get("pick") or []
        action = self.ctx.get("action") or {}
        choice = parse.menu_choice(self.body, len(options))
        if choice:
            action["day"] = options[choice - 1]
            action["message_id"] = self.msg.message_id
        else:
            self._read(action)          # the date typed again, more clearly
            if "day" not in action:
                self._retry("menu_number")
                return
        self.ctx.pop("pick", None)
        self._advance()

    # ---- read-back and confirmation -------------------------------------------

    def _readback(self, action: dict) -> str:
        kind = action["kind"]
        if kind == "undo":
            return say("readback_undo", self.lang, what=action["what"])
        names = self._names(action["fields"])
        when = self._day(action["day"])
        if kind in ("irrigated", "ticket"):
            if action.get("raw") and action.get("inches") is not None:
                amount = say("amount_converted", self.lang, raw=action["raw"],
                             inches=text.inches(action["inches"]),
                             acres=text.inches(round(action.get("raw_acres") or 0, 1)))
            elif action.get("raw"):
                amount = say("amount_raw", self.lang, raw=action["raw"])
            elif action.get("inches") is not None:
                amount = say("amount", self.lang, inches=text.inches(action["inches"]))
            else:
                amount = say("amount_full", self.lang)
            if action.get("hours"):
                amount += say("hours_note", self.lang, hours=f"{action['hours']:g}")
            return say("readback_irrigated", self.lang, fields=names, day=when, amount=amount)
        if kind == "rain":
            return say("readback_rain", self.lang, fields=names, day=when,
                       inches=text.inches(action["inches"]))
        if kind == "harvested":
            return say("readback_harvested", self.lang, fields=names, day=when)
        record = self._field(action["fields"][0])
        crop = text.crop_name(action.get("crop") or record.crop, self.lang,
                              action.get("crop_name") or record.crop_name)
        return say("readback_planted", self.lang, crop=crop, fields=names, day=when)

    def _on_confirm(self) -> None:
        action = self.ctx.get("action")
        if not action:
            self._to_idle()
            self.say("menu")
            return
        answer, rest = parse.yes_no(self.body)
        if answer is True:
            self.ctx.pop("action")
            self._commit(action)
            return
        if action["kind"] == "undo":
            self._to_idle()
            self.say("menu")
            return
        if answer is False and not rest:
            for slot in ("day", "inches", "hours", "raw"):
                action.pop(slot, None)
            self._ask("a:day")
            return
        if answer is False:
            self.body, self.norm = rest, parse.normalize(rest)
        found = self._read(action)
        if found.day or found.options or found.problem or parse.find_amount(self.norm):
            if found.options or found.problem:
                action.pop("day", None)
                self._date_trouble(found)
                return
            self._advance()
            return
        self._retry("yes_no", reask=False)

    def _commit(self, action: dict) -> None:
        kind = action["kind"]
        if kind == "undo":
            store.void_event(self.conn, action["event"])
            self.say("undone")
            self._to_idle()
            return
        day_ = date.fromisoformat(action["day"])
        notes = [action.get("note") or ""]
        if action.get("raw"):
            notes.append(f"{action['raw']} on {action.get('raw_acres') or '?'} acres")
        if action.get("hours"):
            notes.append(f"{action['hours']:g} hours of water")
        note = "; ".join(n for n in notes if n)
        event_kind = "irrigated" if kind == "ticket" else kind
        for field_id in action["fields"]:
            store.add_event(self.conn, field_id, day_, event_kind, inches=action.get("inches"),
                            note=note, source="ticket" if action.get("evidence") else "sms",
                            message_id=action.get("message_id"), evidence=action.get("evidence"))
            if kind == "planted":
                record = self._field(field_id)
                record.crop = action.get("crop") or record.crop
                record.crop_name = action.get("crop_name") or record.crop_name
                if action.get("then") != "next":
                    record.answers["last_irrigation"] = "none"
                self._save(record)
        then = action.get("then")
        if then == "next":
            self._next_question()
            return
        if then == "history":
            count = action.get("count", 0) + 1
            if count > MAX_HISTORY:
                self._next_question()
                return
            self._begin({"kind": "irrigated", "fields": action["fields"], "window": "history",
                         "prompt": "more_irrigation", "then": "history", "none_ok": "history",
                         "count": count})
            self._ask("a:day")
            return
        self.say("saved")
        if kind in ("irrigated", "ticket") and len(action["fields"]) == 1:
            self._after_irrigation(action["fields"][0])
        if kind == "harvested":
            self.say("bare_tip")
        if kind == "planted":
            self.ctx["field"] = action["fields"][0]
            if self._missing(self._field()):
                self._next_question()
                return
        self._to_idle()
        self._resume()

    def _after_irrigation(self, field_id: str) -> None:
        record = self._field(field_id)
        result = self.bot.water.field(record, store.events_for(self.conn, field_id), self.today)
        if result.status is not None and result.status.days_left:
            self.say("after_irrigation", field=record.name,
                     about=text.about_days(result.status.days_left, self.lang))

    # ---- idle: commands and photos ----------------------------------------------

    def _on_idle(self) -> None:
        if self.msg.media:
            self._photo_received()
            return
        command = parse.command(self.body)
        if command:
            self._command(command)
            return
        place = parse.find_place(self.body, lat=self.msg.lat, lon=self.msg.lon)
        if place == "unresolved" or (isinstance(place, parse.Place) and place.in_conus):
            self.say("location_idle")
            return
        if parse.is_greeting(self.body):
            self._help()
            return
        if self.norm.strip(" .,") in _THANKS or not self.body:
            return
        self._flag_for_team()
        self.say("not_understood")

    def _command(self, command: str) -> None:
        if command == "status":
            self._status()
        elif command == "fields":
            self._list_fields()
        elif command == "new_field":
            self.ctx.pop("field", None)
            self._ask("field_name")
        elif command == "irrigated":
            self._start("irrigated", prompt="ask_irrigation_day")
        elif command == "rain":
            self._start("rain", prompt="ask_rain")
        elif command == "harvested":
            self._start("harvested", prompt="ask_harvest_day")
        elif command == "planted":
            self._start("planted", prompt="planted", then="idle")
        elif command == "drone":
            if self.f.plan not in text.FLYING_PLANS:
                self.say("drone_not_in_plan")
                return
            self._start("drone", window="flight", prompt="ask_flight_day")
        elif command == "plan":
            self.say("plan_now", plan=text.plan_name(self.f.plan, self.lang))
            self._ask("plan")
        elif command == "explain":
            self._explain()
        elif command == "map":
            self._start("map")
        elif command == "undo":
            self._undo()
        elif command == "stage":
            self._stage_report()
        elif command == "aphid":
            count = parse.aphid_count(self.norm)
            self._for_sorghum("aphid", count=count.__dict__ if count else None)
        elif command == "maturity":
            self._for_sorghum("maturity")
        else:
            self._help()

    def _status(self) -> None:
        records = self.fields()
        if not records:
            self.say("no_fields")
            return
        results = [self.bot.water.field(r, store.events_for(self.conn, r.id), self.today)
                   for r in records]
        for item in sorted(results, key=status_mod.urgency):
            self.out.append(status_mod.message(item, self.lang, self.today,
                                               map_link=self._map_link))
            s = item.status
            # How to water it matters when a watering is coming: then, and only then,
            # the drone's ground report follows in a text of its own. Not for rainfed.
            if (s is not None and s.method != "none" and s.days_left is not None
                    and s.days_left <= 7):
                report = status_mod.latest_terrain(self.bot.settings, item.field.id)
                lines = status_mod.ground_lines(report, self.lang, intake=item.intake)
                if lines:
                    self.out.append(say("ground_label", self.lang) + " ".join(lines))
            # A thermal camera is the farmer's own, and only they have one.
            if self.f.plan == "thermal":
                pest = status_mod.pest_line(
                    status_mod.latest_thermal(self.bot.settings, item.field.id),
                    self.lang, item.field.name)
                if pest:
                    self.out.append(pest)
        # Offered, never sent: a file of charts is a lot to push at someone who
        # only asked whether to water. They ask for it by replying PORQUE.
        if any(item.status is not None for item in results):
            self.say("explain_offer")

    def _explain(self) -> None:
        """A link to the page showing how each field's answer was worked out.

        One link per field with a real checkbook, since the working is per field.
        """
        explained = [r for r in self.fields()
                     if self.bot.water.field(r, store.events_for(self.conn, r.id),
                                             self.today).status is not None]
        if not explained:
            self.say("explain_none")
            return
        for record in explained:
            token = link_token(self.conn, "explain", record.id, self.bot.now)
            self.say("explain_link", field=record.name,
                     link=self.bot.settings.link(f"r/{token}"), days=LINK_DAYS)

    def _list_fields(self) -> None:
        records = self.fields()
        if not records:
            self.say("no_fields")
            return
        lines = [say("fields_header", self.lang)]
        for number, record in enumerate(records, start=1):
            crop = text.crop_name(record.crop, self.lang, record.crop_name) if record.crop else "?"
            size = (f", {record.acres:.0f} ac" if record.acres else
                    f", ~{record.acres_said:g} ac" if record.acres_said else "")
            missing = [text.pick(text.MISSING[m], self.lang) for m in self.missing_all(record)]
            lines.append(f"{number}) {record.name}: {crop}{size}"
                         + (say("fields_missing", self.lang, what=", ".join(missing))
                            if missing else ""))
        self.out.append("\n".join(lines))

    # ---- sorghum ----------------------------------------------------------------

    def _sorghum_fields(self) -> list[FieldRow]:
        return [r for r in self.fields() if r.crop == "sorghum"]

    def _sorghum_stage(self, record: FieldRow) -> tuple[dict | None, str]:
        """The field's heat-unit stage, or None and why: ``no_planting`` or ``no_weather``."""
        events = store.events_for(self.conn, record.id)
        item = self.bot.water.field(record, events, self.today)
        if item.status is not None and item.status.stage:
            return item.status.stage, ""
        planted = any(e.kind == "planted" and e.voided_at is None for e in events)
        return None, "no_weather" if planted else "no_planting"

    def _stage_name(self, key: str) -> str:
        return text.pick(text.SORGHUM_STAGES[key], self.lang)

    def _stage_report(self) -> None:
        records = self._sorghum_fields()
        if not records:
            self.say("sorghum_none")
            return
        watch = {"midge": "watch_midge", "sugarcane_aphid": "watch_aphid",
                 "headworm": "watch_headworm", "harvest": "watch_harvest"}
        for record in records:
            stage, why = self._sorghum_stage(record)
            if stage is None:
                self.say("stage_" + why, field=record.name)
                continue
            rest = ""
            if stage["next_stage"] and stage["next_date"]:
                rest += say("stage_next", self.lang, stage=self._stage_name(stage["next_stage"]),
                            date=self._day(stage["next_date"]))
            if stage["critical"]:
                rest += say("stage_critical", self.lang)
            # Two things to look for fit a text; the WHY file lists them all.
            for item in stage["watch"][:2]:
                rest += say(watch[item], self.lang)
            if stage["maturity_assumed"]:
                rest += say("stage_assumed", self.lang)
            self.say("stage_report", field=record.name,
                     stage=self._stage_name(stage["stage"]),
                     day=stage["days_after_planting"], rest=rest)

    def _for_sorghum(self, then: str, **data: object) -> None:
        """Run ``then`` on the sorghum field the message names, the only one, or a chosen one."""
        records = self._sorghum_fields()
        if not records:
            self.say("sorghum_none")
            return
        ids, _ = self._named_fields(self.norm, multi=False)
        chosen = next((r for r in records if r.id in ids), None)
        if chosen is None and len(records) == 1:
            chosen = records[0]
        if chosen is None:
            self.ctx["sorghum"] = {"then": then, **data}
            self.f.state = "sorghum:field"
            self._reask()
            return
        self._sorghum_then(then, chosen, data)

    def _on_sorghum_field(self) -> None:
        pending = self.ctx.get("sorghum") or {}
        records = self._sorghum_fields()
        choice = parse.menu_choice(self.body, len(records))
        chosen = records[choice - 1] if choice else None
        if chosen is None:
            ids, _ = self._named_fields(self.norm, multi=False)
            chosen = next((r for r in records if r.id in ids), None)
        if chosen is None:
            self._retry("menu_number")
            return
        self.ctx.pop("sorghum", None)
        self.f.state = "idle"
        self._sorghum_then(pending.get("then", "aphid"), chosen, pending)

    def _sorghum_then(self, then: str, record: FieldRow, data: dict) -> None:
        if then == "maturity":
            self.ctx["field"] = record.id
            self.ctx["maturity_only"] = True
            self._ask("f:maturity")
            return
        count = data.get("count")
        if count is None:
            self.ctx["aphid"] = {"field": record.id}
            self.f.state = "aphid:count"
            self._reask()
            return
        self._aphid_answer(record, parse.AphidCount(**count))

    def _aphid_howto(self, record: FieldRow) -> str:
        stage, _ = self._sorghum_stage(record)
        if stage is None:
            return say("aphid_howto_nostage", self.lang, field=record.name)
        if stage["aphid_threshold_pct"] is None:
            return say("aphid_howto_mature", self.lang, field=record.name)
        return say("aphid_howto", self.lang, field=record.name,
                   stage=self._stage_name(stage["stage"]),
                   threshold=stage["aphid_threshold_pct"])

    def _on_aphid_count(self) -> None:
        count = parse.aphid_count(self.body)
        if count is None:
            command = parse.command(self.body)
            if command and command != "aphid":
                self._interrupt(command)
                return
            self.say("aphid_count_again")
            return
        record = self._field((self.ctx.get("aphid") or {}).get("field"))
        self._to_idle()
        if record is not None:
            self._aphid_answer(record, count)

    def _aphid_answer(self, record: FieldRow, count: parse.AphidCount) -> None:
        """Store a sugarcane aphid count and say where it stands against this stage's threshold."""
        stage, _ = self._sorghum_stage(record)
        pct = int(count.percent + 0.5)          # 32.5% is 33, the way a farmer rounds
        note = {"pest": "sugarcane_aphid", "percent": round(count.percent, 1),
                "infested": count.infested, "checked": count.checked}
        if stage is None:
            store.add_event(self.conn, record.id, self.today, "scouting", note=json.dumps(note),
                            message_id=self.msg.message_id)
            self.say("aphid_saved_nostage", field=record.name, pct=pct)
            return
        verdict = sat_stages.aphid_verdict(count.percent, stage["stage"])
        note.update(stage=stage["stage"], threshold=stage["aphid_threshold_pct"],
                    verdict=verdict)
        store.add_event(self.conn, record.id, self.today, "scouting", note=json.dumps(note),
                        message_id=self.msg.message_id)
        if count.percent == 0 and verdict != "harvest":
            self.say("aphid_none", field=record.name)
            return
        if verdict == "above":
            self._flag_for_team()
        self.say("aphid_" + verdict, field=record.name, pct=pct,
                 threshold=stage["aphid_threshold_pct"],
                 stage=self._stage_name(stage["stage"]))

    def _undo(self) -> None:
        event = store.last_event_of(self.conn, self.f.phone)
        if event is None:
            self.say("undo_none")
            return
        record = self._field(event.field_id)
        kind = {"irrigated": ("riego", "watering"), "rain": ("lluvia", "rain"),
                "harvested": ("cosecha", "harvest"), "planted": ("siembra", "planting"),
                "photo": ("foto", "photo"),
                "scouting": ("conteo de pulgón", "aphid count")}[event.kind]
        what = f"{text.pick(kind, self.lang)} {record.name} {self._day(event.day.isoformat())}"
        if event.inches is not None:
            what += f", {text.inches(event.inches)} {'pulg' if self.lang == 'es' else 'in'}"
        self._begin({"kind": "undo", "event": event.id, "what": what, "fields": [event.field_id]})
        self._ask("confirm")

    def _photo_received(self) -> None:
        self.ctx["photos"] = [m.path for m in self.msg.media]
        self.ctx["photo_message"] = self.msg.message_id
        self._ask("photo")

    def _on_photo(self) -> None:
        choice = parse.menu_choice(self.body, 3)
        words = set(parse.words(self.body))
        if not choice:
            choice = (1 if words & {"ticket", "recibo", "orden", "agua", "water"} else
                      2 if words & {"problema", "problem", "plaga", "enfermedad", "pest"} else
                      3 if words & {"otra", "otro", "other"} else None)
        if not choice:
            self._retry("menu_number")
            return
        photos = self.ctx.pop("photos", [])
        message_id = self.ctx.pop("photo_message", None)
        self.f.state = "idle"
        if choice == 1:
            self._begin({"kind": "ticket", "window": "ticket", "prompt": "ask_ticket",
                         "evidence": photos[0] if photos else None, "message_id": message_id})
        elif choice == 2:
            self._begin({"kind": "photo", "evidence": photos[0] if photos else None,
                         "evidence_all": photos, "message_id": message_id})
        else:
            self._flag_for_team()
            self.say("photo_saved")
            return
        self._advance()

    def _on_checkin(self) -> None:
        field_id = self.ctx.get("checkin")
        record = self._field(field_id) if field_id else None
        answer, rest = parse.yes_no(self.body)
        if record is None:
            self._to_idle()
            self._on_idle()
            return
        if answer is False and not rest:
            self.say("checkin_no", field=record.name)
            self._to_idle()
            return
        command = parse.command(self.body)
        found = parse.find_date(self.norm, self.today, window="irrigated")
        if answer is True or command == "irrigated" or found.day or found.options:
            self._to_idle()
            action = self._begin({"kind": "irrigated", "fields": [record.id],
                                  "window": "irrigated", "prompt": "ask_irrigation_day"})
            found = self._read(action)
            if "day" not in action and (found.options or found.problem):
                self._date_trouble(found)
                return
            self._advance()
            return
        self._to_idle()
        self._on_idle()

    # ---- links ----------------------------------------------------------------

    def _map_link(self, record: FieldRow) -> str:
        return self.bot.settings.link(f"f/{map_token(self.conn, record.id, self.bot.now)}")

    def _upload_link(self, record: FieldRow, flown_on: date, bare: bool | None) -> None:
        token = store.new_link(self.conn, "upload", record.id, days=LINK_DAYS, now=self.bot.now)
        flight_id = f"{record.id}-{flown_on:%Y%m%d}"
        taken = {row["flight_id"] for row in store.uploads(self.conn)}
        suffix = 2
        base = flight_id
        while flight_id in taken:
            flight_id, suffix = f"{base}-{suffix}", suffix + 1
        folder = self.bot.settings.drone_workspace / "data" / "raw" / flight_id
        store.new_upload(self.conn, token, flight_id=flight_id, folder=str(folder),
                         flown_on=flown_on, bare_soil=bare)
        self.say("upload_link", link=self.bot.settings.link(f"u/{token}"), days=LINK_DAYS)

    # ---- the question being asked ------------------------------------------------

    def _prompt(self, state: str) -> str | None:
        lang = self.lang
        record = self._field()
        name = record.name if record else ""
        action = self.ctx.get("action") or {}
        ids = action.get("fields") or ([record.id] if record else [])
        names = self._names(ids) if ids else ""
        if state == "lang":
            return text.WELCOME
        if state == "name":
            return say("name", lang)
        if state == "consent":
            return say("consent", lang, name=self.f.name or "")
        if state == "plan":
            return say("plan", lang)
        if state == "field_name":
            return say("field_name_first" if self.ctx.get("first") else "field_name", lang)
        if state in ("f:acres", "f:location", "f:crop", "f:crop_other", "f:maturity",
                     "f:method", "f:side"):
            return say(state[2:], lang, field=name)
        if state == "a:fields":
            options = [f"{i} {f.name}" for i, f in enumerate(self.fields(), start=1)]
            if action.get("kind") in MULTI and len(options) > 1:
                options.append(f"{len(options) + 1} {say('all_fields', lang)}")
            return say("pick_field", lang, options=", ".join(options))
        if state == "a:day":
            key = action.get("prompt") or "date_missing"
            if key in ("planted", "planted_cane"):
                first = self._field(ids[0]) if ids else record
                crop = text.crop_name(action.get("crop") or (first.crop if first else None),
                                      lang, action.get("crop_name"))
                return say(key, lang, crop=crop, field=first.name if first else name)
            return say(key, lang, field=names or name, fields=names or name)
        if state == "a:inches":
            return say("ticket_inches" if action.get("kind") == "ticket" else "inches_missing",
                       lang)
        if state == "a:crop":
            return say("crop", lang, field=names)
        if state == "a:bare":
            return say("ask_bare", lang, fields=names)
        if state == "pick_date":
            options = self.ctx.get("pick") or []
            if len(options) == 2:
                return say("date_pick", lang, a=self._day(options[0]), b=self._day(options[1]))
        if state == "confirm" and action:
            return self._readback(action)
        if state == "photo":
            return say("photo_kind", lang)
        if state == "sorghum:field":
            options = [f"{i} {f.name}" for i, f in enumerate(self._sorghum_fields(), start=1)]
            return say("pick_sorghum", lang, options=", ".join(options))
        if state == "aphid:count":
            target = self._field((self.ctx.get("aphid") or {}).get("field"))
            return self._aphid_howto(target) if target else None
        if state == "checkin":
            checked = self._field(self.ctx.get("checkin"))
            return say("checkin", lang, field=checked.name) if checked else None
        return None


def link_token(conn: sqlite3.Connection, kind: str, field_id: str,
               now: datetime | None = None) -> str:
    """One of a field's link tokens: the open one if there is one, else a new one.

    Asking twice should not leave two live links, and a farmer who kept the
    first text should find that link still works.
    """
    row = conn.execute(
        "SELECT token FROM links WHERE kind = ? AND field_id = ? AND expires_at > ? "
        "ORDER BY created_at DESC LIMIT 1", (kind, field_id, store.utc_iso(now))).fetchone()
    return row["token"] if row else store.new_link(conn, kind, field_id, days=LINK_DAYS,
                                                   now=now)


def map_token(conn: sqlite3.Connection, field_id: str, now: datetime | None = None) -> str:
    """The field's map link token."""
    return link_token(conn, "map", field_id, now)
