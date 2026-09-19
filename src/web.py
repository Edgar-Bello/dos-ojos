"""The web side: Twilio's webhook, the map and upload pages, and the simulator.

Standard library only. One lock runs the bot one text at a time, which is
plenty for a pilot: a reply takes milliseconds.

- ``POST /sms/twilio``: a farmer's text, signed by Twilio; the answer is TwiML.
- ``POST /sms/status``: Twilio reporting a text delivered or lost.
- ``/f/<token>``: tap the field's corners on aerial imagery; saving texts back.
- ``/u/<token>``: upload a drone flight's photos, one file at a time, resumable.
- ``/r/<token>``: why a field got the answer it got, charts and all; ``/r/<token>/file``
  is the same page as a download.
- ``/sim``: a phone on screen, for demos and testing. Off unless ``--sim``, and
  refused to anything arriving through a tunnel, since it can pose as any number.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shlex
import shutil
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shapely.geometry import mapping, shape
from shapely.validation import make_valid

from dosojos_sat.fields import compute_acres, utm_epsg_for

from . import ai, explain, export, outbox, parse, store, text, twilio
from .bot import Bot, Inbound, Media, ai_text, link_token, map_token
from .config import LINK_DAYS, Settings
from .jobs import Jobs
from .status import (Water, drone_wait, latest_flags, latest_terrain, latest_thermal,
                     pest_line)
from .status import message as status_message

log = logging.getLogger(__name__)

PAGES = Path(__file__).resolve().parent / "pages"
#: Photos, video and its flight log, finished maps (GeoTIFF), and the point
#: clouds a drone's laser or a mapping service delivers - a laser flight is
#: nothing but .las files, and refusing them refuses the flight.
UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng", ".mp4", ".mov",
                     ".srt", ".zip", ".las", ".laz"}
MAX_FILE_BYTES = 8 * 1024 ** 3
MAX_FORM_BYTES = 1024 ** 2
MAX_PHOTO_BYTES = 15 * 1024 ** 2
#: Room left on the disk after an upload, so the drone pipeline still has space to work.
DISK_RESERVE_BYTES = 5 * 1024 ** 3
FIELD_ACRES = (0.2, 5000.0)
FORWARDED = ("X-Forwarded-For", "X-Forwarded-Host", "Cf-Connecting-Ip", "X-Real-Ip",
             "Forwarded", "Ngrok-Trace-Id")
SIM_PHONE = "+19565550123"      # 555-01xx numbers are reserved for fiction
#: A first reading that fails (a government service down) is tried again this
#: often, this many times in all, before the team is told.
READ_RETRY_MINUTES = 15
#: USDA Soil Data Access goes down every night from 12:30 to 12:45 AM Central; a
#: reading that fails then is tried again the minute it is back.
SOIL_DOWN = ((0, 30), (0, 46))
READ_ATTEMPTS = 3

PAGE_TEXT = {
    "map": {
        "help": ("Toque las esquinas de {field}, una tras otra, dando la vuelta. Arrastre un "
                 "punto para moverlo.",
                 "Tap the corners of {field}, one after another, going around. Drag a point "
                 "to move it."),
        "tap": ("Toque la primera esquina", "Tap the first corner"),
        "acres": ("{acres} acres", "{acres} acres"),
        "undo": ("Deshacer", "Undo"), "clear": ("Borrar", "Clear"),
        "me": ("Mi ubicación", "My location"), "save": ("Guardar", "Save"),
        "check": ("Usted dijo {said} acres y el dibujo mide {acres}. ¿Guardar así?",
                  "You said {said} acres and the drawing measures {acres}. Save it anyway?"),
        "saved": ("¡Guardado! {acres} acres. Ya puede cerrar esta página.",
                  "Saved! {acres} acres. You can close this page."),
        "failed": ("No se pudo guardar: {error}", "Could not save: {error}"),
        "pin": ("Su pin", "Your pin"),
        "no_gps": ("No se pudo leer su ubicación.", "Couldn't read your location."),
        "imagery": ("Foto aérea", "Aerial photo"), "roads": ("Con caminos", "With roads"),
        "no_tiles": ("No cargan las fotos del mapa. Revise su internet, o toque las esquinas "
                     "guiándose por el punto rojo y guarde; nosotros lo revisamos.",
                     "The map pictures aren't loading. Check your internet, or tap the corners "
                     "using the red dot as a guide and save; we'll check it."),
    },
    "upload": {
        "title": ("Fotos del dron: {field}", "Drone photos: {field}"),
        "info": ("Vuelo del {day}. Elija todas las fotos del vuelo (o el video). Mejor con "
                 "WiFi: son muchas. Si se corta, vuelva a elegirlas; lo que ya subió no se "
                 "repite.",
                 "Flight of {day}. Choose all the photos from the flight (or the video). Best "
                 "on WiFi: there are many. If it stops, choose them again; what is already up "
                 "is skipped."),
        "choose": ("Elegir fotos", "Choose photos"), "done": ("Terminé", "I'm done"),
        # A camera writes its own folder, and a thermal scan writes thousands of
        # files in folders inside it. Picking those one by one is not a thing to
        # ask of anybody.
        "folder": ("Elegir una carpeta", "Choose a folder"),
        "progress": ("{done} de {total} archivos, {mb} MB", "{done} of {total} files, {mb} MB"),
        "finished": ("¡Listo! Recibimos {n} archivos. Ya puede cerrar esta página.",
                     "Done! We got {n} files. You can close this page."),
        "failed": ("Falló {name}: {error}", "{name} failed: {error}"),
        "shrinking": ("Achicando el mapa {name} para que suba rápido: {pct}%",
                      "Making the map {name} smaller so it uploads fast: {pct}%"),
        "nothing": ("Todavía no ha subido nada.", "Nothing uploaded yet."),
    },
    "gone": ("Este enlace ya no sirve. Mande MAPA, DRON o PORQUE por mensaje para recibir "
             "otro.",
             "This link no longer works. Text MAP, DRONE or WHY to get a new one."),
}


def _strings(group: dict, lang: str) -> dict[str, str]:
    return {key: text.pick(pair, lang) for key, pair in group.items()}


def _page(name: str, data: dict) -> bytes:
    """A page from pages/, with its data dropped in as JSON."""
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (PAGES / name).read_text(encoding="utf-8").replace("__DATA__", blob).encode("utf-8")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# The application: everything the handler asks for
# --------------------------------------------------------------------------- #


class _Counted:
    """A request body that remembers how much of it was read."""

    def __init__(self, stream):
        self.stream, self.read_bytes = stream, 0

    def read(self, n: int) -> bytes:
        chunk = self.stream.read(n)
        self.read_bytes += len(chunk)
        return chunk


def _covers(pieces: dict[str, int], total: int) -> bool:
    """True when the pieces, by their offsets and lengths, fill 0..total with no gap."""
    reached = 0
    for start, length in sorted((int(k), v) for k, v in pieces.items()):
        if start > reached:
            return False
        reached = max(reached, start + length)
    return reached >= total


class App:
    def __init__(self, settings: Settings, *, sim: bool = False, verify: bool = True,
                 resolve: parse.Resolver | None = None, water: Water | None = None):
        self.settings = settings
        self.sim = sim
        self.verify = verify
        self.resolve = resolve
        self.water = water or Water(settings)
        self.lock = threading.Lock()
        #: A pinned "now" for demos (``--as-of``); None means the real time.
        self.now: datetime | None = None
        self.jobs = Jobs()
        #: The local AI's own thread: a farmer's question never waits behind a flight.
        self.thinker = ai.Thinker()
        #: What each field looked like when it was last sent to be read, so a
        #: corrected map or planting date reads it again and nothing else does.
        self._read_as: dict[str, str] = {}
        #: The newest request writing each partly uploaded file: a piece the page
        #: gave up on can still be arriving when its retry starts, and must stop.
        self._writer: dict[str, object] = {}
        self.upload_lock = threading.Lock()

    def db(self):
        return store.session(self.settings.db_path)

    def _bot(self, conn) -> Bot:
        return Bot(conn, self.settings, now=self.now, resolve=self.resolve, water=self.water,
                   think=self._think, advise=self._advise_later)

    # ---- the local AI ----------------------------------------------------------------

    def _think(self, farmer, message: Inbound) -> bool:
        """Hand a text the rules could not place to the AI; answered when it has read it."""
        if ai.ready(self.settings) is None:
            return False
        phone, body, lang = farmer.phone, message.body, farmer.language
        return self.thinker.add(f"read:{phone}:{message.message_id}",
                                lambda: self._read_words(phone, body, lang, message.channel))

    def _read_words(self, phone: str, body: str, lang: str, channel: str) -> None:
        model = ai.ready(self.settings)
        today = self._today()
        with self.db() as conn:
            fields = [{"name": r.name, "crop": r.crop,
                       "summary": self._summary_for_ai(conn, r, today)}
                      for r in store.fields_of(conn, phone)]
        heard = None
        if model is not None:
            try:
                heard = ai.understand(model, body, lang=lang, today=today, fields=fields)
            except ai.AIError as exc:
                log.warning("AI could not read a text: %s", exc)
        with self.lock, self.db() as conn:
            farmer = store.get_farmer(conn, phone)

            def send(reply: str) -> None:
                outbox.deliver(conn, self.settings, farmer, reply, now=self.now, urgent=True)

            command = heard.command_text(body) if heard else None
            if heard and heard.intent == "question" and heard.answer:
                send(text.gsm_safe(heard.answer))
                return
            if not command:
                send(text.say("not_understood", lang))
                return
            log.info("AI read %r as %r", body, command)
            send(text.say("ai_understood", lang, what=command))
            bot = self._bot(conn)
            for reply in bot.handle(Inbound(phone, command, channel=channel, from_ai=True)):
                send(reply)

    def _summary_for_ai(self, conn, record, today) -> str:
        """A line per field for the AI reading a text: crop, and days until water."""
        crop = text.crop_name(record.crop, "en", record.crop_name) if record.crop else "?"
        item = self.water.field(record, store.events_for(conn, record.id), today)
        s = item.status
        if s is None or s.days_left is None:
            return crop
        return f"{crop}, water in {s.days_left} days (by {s.water_by})"

    def _advise_later(self, farmer, field_id: str) -> bool:
        """Ask the AI for a field's recommendation and text it when it is written."""
        if ai.ready(self.settings) is None:
            return False
        return self.thinker.add(f"advise:{field_id}",
                                lambda: self._prepare_advice(field_id, send=True))

    def _prepare_advice(self, field_id: str, *, send: bool = False) -> "ai.Advice | None":
        """Write (or find) the AI's recommendation for a field's readings as they are now.

        Runs without the app's lock: the model can take a minute on a laptop.
        """
        model = ai.ready(self.settings)
        if model is None:
            return None
        today = self._today()
        with self.db() as conn:
            record = store.get_field(conn, field_id)
            farmer = store.get_farmer(conn, record.phone)
            events = store.events_for(conn, field_id)
        item = self.water.field(record, events, today, full=True)
        brief = ai.field_brief(self.settings, farmer, item, events, today)
        if brief is None:
            return None
        key = ai.brief_key(brief, self.settings.ai_model)
        advice = ai.kept(self.settings, field_id, key)
        if advice is None:
            advice = ai.recommend(model, brief, lang=farmer.language,
                                  now=(self.now or self.settings.now()).isoformat(
                                      timespec="minutes"))
            if advice is None:
                return None
            ai.keep(self.settings, field_id, key, advice, brief)
        if send:
            with self.lock, self.db() as conn:
                farmer = store.get_farmer(conn, record.phone)
                outbox.deliver(conn, self.settings, farmer,
                               ai_text(record.name, advice, farmer.language),
                               now=self.now, urgent=True)
        return advice

    def _advice_now(self, record, farmer, item, events) -> "ai.Advice | None":
        """The kept recommendation for these exact readings, without asking the model."""
        if ai.for_settings(self.settings) is None:
            return None
        brief = ai.field_brief(self.settings, farmer, item, events, self._today())
        if brief is None:
            return None
        return ai.kept(self.settings, record.id, ai.brief_key(brief, self.settings.ai_model))

    # ---- texts in ------------------------------------------------------------------

    def receive(self, message: Inbound, *, provider_id: str | None = None,
                reply_status: str = "sent") -> list[str]:
        """Log a text, let the bot answer it, log the answers; returns them."""
        with self.lock, self.db() as conn:
            message_id = store.log_in(conn, message.phone, message.body,
                                      media=[m.path for m in message.media],
                                      provider_id=provider_id)
            if message_id is None:
                log.info("duplicate delivery of %s ignored", provider_id)
                return []
            message.message_id = message_id
            replies = self._bot(conn).handle(message)
            # Telegram has no webhook answer to put replies in; the console's
            # trial number throws webhook answers away.
            by_api = ((message.channel == "telegram" and self.settings.telegram_token)
                      or (message.channel == "sms" and self.settings.reply_by_api
                          and self.settings.twilio_ready))
            for reply in replies:
                if by_api:
                    reply_id = store.log_out(conn, message.phone, reply, status="sending")
                    outbox._send(conn, self.settings, message.phone, reply, reply_id)
                else:
                    store.log_out(conn, message.phone, reply, status=reply_status)
            if by_api:
                replies = []           # already sent; the webhook answers with nothing
            for record in store.fields_of(conn, message.phone):
                self._maybe_read(conn, record)
            return replies

    def twilio_webhook(self, params: dict[str, str], signature_header: str | None) -> bytes:
        settings = self.settings
        if self.verify:
            if not settings.twilio_token:
                raise HttpError(503, "Twilio keys are missing from this server's sms.env")
            url = settings.link("sms/twilio")
            if not twilio.valid_signature(url, params, settings.twilio_token, signature_header):
                log.warning("refused a webhook call with a bad signature. If it came from "
                            "Twilio, DOSOJOS_PUBLIC_URL must be the exact address in the "
                            "Twilio console (now %s)", url)
                raise HttpError(403, "bad signature")
        phone = store.normalize_phone(params.get("From", ""))
        sid = params.get("MessageSid") or params.get("SmsSid")
        media = []
        for index in range(int(params.get("NumMedia") or 0)):
            url = params.get(f"MediaUrl{index}")
            if not url:
                continue
            folder = settings.media_dir / phone.lstrip("+")
            stem = f"{datetime.now():%Y%m%d-%H%M%S}_{sid or 'mms'}_{index}"
            try:
                path = twilio.fetch_media(settings, url, folder, stem)
            except twilio.TwilioError as exc:
                log.warning("photo from %s not saved: %s", phone, exc)
                continue
            media.append(Media(str(path), params.get(f"MediaContentType{index}", "")))
        lat, lon = params.get("Latitude"), params.get("Longitude")
        message = Inbound(phone, params.get("Body", ""), media,
                          float(lat) if lat else None, float(lon) if lon else None, "sms")
        replies = self.receive(message, provider_id=sid)
        if params.get("OptOutType"):
            replies = []       # Twilio answered STOP, START or HELP itself
        return twilio.twiml(replies).encode("utf-8")

    def twilio_status(self, params: dict[str, str], signature_header: str | None) -> None:
        if self.verify and not twilio.valid_signature(
                self.settings.link("sms/status"), params, self.settings.twilio_token or "",
                signature_header):
            raise HttpError(403, "bad signature")
        state = params.get("MessageStatus")
        if state in ("failed", "undelivered"):
            code = params.get("ErrorCode")
            hint = twilio.HINTS.get(int(code)) if code and code.isdigit() else None
            with self.db() as conn:
                conn.execute("UPDATE messages SET status = ?, error = ? WHERE provider_id = ?",
                             (state, f"code {code}: {hint or 'see the Twilio console'}",
                              params.get("MessageSid")))
            log.warning("text %s %s (code %s): %s", params.get("MessageSid"), state, code, hint)

    # ---- the map page ------------------------------------------------------------------

    def _link(self, conn, token: str, kind: str):
        link = store.get_link(conn, token, kind, now=self.now)
        if link is None:
            raise HttpError(404, "gone")
        record = store.get_field(conn, link["field_id"])
        farmer = store.get_farmer(conn, record.phone)
        return link, record, farmer

    def map_page(self, token: str) -> bytes:
        with self.db() as conn:
            link, record, farmer = self._link(conn, token, "map")
        lang = farmer.language
        strings = _strings(PAGE_TEXT["map"], lang)
        strings["help"] = strings["help"].format(field=record.name)
        return _page("map.html", {
            "lang": lang, "field": record.name, "strings": strings,
            "pin": [record.lat, record.lon] if record.lat is not None else None,
            "outline": record.outline, "acres_said": record.acres_said,
            "banner": self.settings.banner,
        })

    def save_outline(self, token: str, payload: dict) -> dict:
        ring = payload.get("coordinates")
        if not isinstance(ring, list) or not 3 <= len(ring) <= 500:
            raise HttpError(400, "tap at least three corners")
        try:
            ring = [[float(lon), float(lat)] for lon, lat in ring]
        except (TypeError, ValueError) as exc:
            raise HttpError(400, "the corners are not numbers") from exc
        geometry = shape({"type": "Polygon", "coordinates": [ring + [ring[0]]]})
        if not geometry.is_valid:
            repaired = make_valid(geometry)
            parts = [g for g in getattr(repaired, "geoms", [repaired]) if g.geom_type in
                     ("Polygon", "MultiPolygon")]
            if not parts:
                raise HttpError(400, "the lines cross; tap the corners in order around the field")
            geometry = max(parts, key=lambda g: g.area)
        acres = compute_acres(geometry, utm_epsg_for(geometry))
        if not FIELD_ACRES[0] <= acres <= FIELD_ACRES[1]:
            raise HttpError(400, f"{acres:.1f} acres does not look like a field")
        with self.lock, self.db() as conn:
            link, record, farmer = self._link(conn, token, "map")
            team = json.loads(link["meta"] or "{}").get("by") == "team"
            record.outline = mapping(geometry)
            record.acres = round(acres, 2)
            record.outline_by = "team" if team else "farmer"
            record.outline_at = store.now_iso()
            store.save_field(conn, record)
            store.use_link(conn, token)
            lang = farmer.language
            if team:
                body = text.say("map_by_team", lang, field=record.name,
                                acres=f"{acres:.1f}",
                                link=self.settings.link(f"f/{map_token(conn, record.id, self.now)}"))
            else:
                said = (text.say("map_said", lang, said=f"{record.acres_said:g}")
                        if record.acres_said else "")
                body = text.say("map_saved", lang, field=record.name, acres=f"{acres:.1f}",
                                said=said)
            outbox.deliver(conn, self.settings, farmer, body, now=self.now, urgent=not team)
            self._maybe_read(conn, record)
        return {"ok": True, "acres": round(acres, 1)}

    # ---- the upload page -----------------------------------------------------------------

    def _upload(self, conn, token: str):
        link, record, farmer = self._link(conn, token, "upload")
        upload = store.get_upload(conn, token)
        if upload is None:
            raise HttpError(404, "gone")
        return upload, record, farmer

    def upload_page(self, token: str) -> bytes:
        with self.db() as conn:
            upload, record, farmer = self._upload(conn, token)
        lang = farmer.language
        strings = _strings(PAGE_TEXT["upload"], lang)
        flown = (text.day(datetime.fromisoformat(upload["flown_on"]).date(), lang)
                 if upload["flown_on"] else "?")
        strings["title"] = strings["title"].format(field=record.name)
        strings["info"] = strings["info"].format(day=flown)
        return _page("upload.html", {
            "lang": lang, "strings": strings, "done": bool(upload["done_at"]),
            "files": upload["files"], "max_file": MAX_FILE_BYTES,
            "extensions": sorted(UPLOAD_EXTENSIONS), "banner": self.settings.banner,
        })

    def upload_list(self, token: str) -> dict:
        with self.db() as conn:
            upload, _, _ = self._upload(conn, token)
        folder = Path(upload["folder"])
        files = {p.name: p.stat().st_size for p in folder.iterdir()
                 if p.is_file() and not p.name.endswith((".part", ".part.json"))} if folder.exists() else {}
        return {"files": files}

    def upload_file(self, token: str, raw_name: str, length: int, stream, *,
                    offset: int | None = None, total: int | None = None) -> dict:
        """Save one file, or one piece of it.

        A quick tunnel caps a request at 100 MB and about 100 s, and each connection
        through it is slow (~0.12 MB/s) while several together are not, so the page
        sends big files as pieces, several at once and in any order: each ``length``
        bytes at ``offset`` of a file ``total`` bytes long. The pieces that arrived
        are listed next to the partial file; when they cover it, it is finished.
        """
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(urllib.parse.unquote(raw_name)).name)
        if not name or name.startswith(".") or Path(name).suffix.lower() not in UPLOAD_EXTENSIONS:
            raise HttpError(415, f"{raw_name}: only photos, videos (with .srt), maps, "
                                 "laser point clouds or a zip")
        whole = length if total is None else total
        start = offset or 0
        if not 0 < whole <= MAX_FILE_BYTES:
            raise HttpError(413, f"{name}: too big or empty")
        if not (0 < length and 0 <= start and start + length <= whole):
            raise HttpError(400, f"{name}: this piece does not fit the file")
        with self.db() as conn:
            upload, _, _ = self._upload(conn, token)
        folder = Path(upload["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        partial = folder / (name + ".part")
        ledger = folder / (name + ".part.json")
        key = f"{partial}@{start}"
        with self.upload_lock:
            pieces = self._pieces(ledger, whole) if offset is not None else {}
            if not pieces and shutil.disk_usage(folder).free < whole + DISK_RESERVE_BYTES:
                raise HttpError(507, "the server's disk is full; tell the Dos Ojos team")
            if offset is None or not partial.exists():
                partial.open("wb").close()
                if offset is not None:
                    ledger.write_text(json.dumps({"total": whole, "pieces": {}}), "utf-8")
            me = object()
            self._writer[key] = me              # a piece sent again stops the one before
        remaining = length
        with partial.open("r+b") as handle:
            handle.seek(start)
            while remaining:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    raise HttpError(400, f"{name}: the upload was cut off")
                if self._writer.get(key) is not me:
                    raise HttpError(409, f"{name}: this piece was sent again")
                handle.write(chunk)
                remaining -= len(chunk)
        with self.upload_lock:
            if self._writer.get(key) is me:
                del self._writer[key]
            if offset is not None:
                pieces = self._pieces(ledger, whole)
                pieces[str(start)] = length
                ledger.write_text(json.dumps({"total": whole, "pieces": pieces}), "utf-8")
                if not _covers(pieces, whole):
                    return {"ok": True, "name": name,
                            "received": sum(pieces.values())}
                ledger.unlink(missing_ok=True)
            if not partial.exists():
                return {"ok": True, "name": name, "bytes": whole}      # a twin piece finished it
            existed = target.exists()
            partial.replace(target)
        if not existed:
            with self.lock, self.db() as conn:
                store.count_upload(conn, token, whole)
        return {"ok": True, "name": name, "bytes": whole}

    @staticmethod
    def _pieces(ledger: Path, total: int) -> dict[str, int]:
        """The pieces of this file that arrived, or none when it is a new file."""
        try:
            saved = json.loads(ledger.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return saved.get("pieces", {}) if saved.get("total") == total else {}

    def upload_done(self, token: str) -> dict:
        with self.lock, self.db() as conn:
            upload, record, farmer = self._upload(conn, token)
            if not upload["files"]:
                raise HttpError(400, "nothing uploaded yet")
            first_time = upload["done_at"] is None
            store.finish_upload(conn, token)
            flight_id = export.register_flight(self.settings, store.get_upload(conn, token),
                                               record)
            if first_time:
                record.answers["flight"] = "working"
                store.save_field(conn, record)
                body = text.say("upload_done", farmer.language, n=upload["files"],
                                size=text.size(upload["bytes"]), field=record.name)
                outbox.deliver(conn, self.settings, farmer, body, now=self.now, urgent=True)
                self._process_flight(record.id, flight_id)
        log.info("upload %s finished: %s files for %s", flight_id, upload["files"], record.id)
        return {"ok": True, "files": upload["files"], "flight": flight_id}

    # ---- the explanation page ----------------------------------------------------------

    def explain_page(self, token: str, *, download: bool = False) -> bytes:
        """Why one field got the answer it got. Slow by the standards of this
        server, a second or two, because it redraws the charts; it is asked for
        by hand, at most a few times a day."""
        with self.db() as conn:
            link, record, farmer = self._link(conn, token, "explain")
            events = store.events_for(conn, record.id)
            store.use_link(conn, token)
        settings = self.settings
        today = (self.now or settings.now()).date()
        item = self.water.field(record, events, today, full=True)
        advice = self._advice_now(record, farmer, item, events)
        page = explain.build(
            settings, farmer, item, events, today=today, advice=advice,
            terrain=latest_terrain(settings, record.id),
            thermal=(latest_thermal(settings, record.id)
                     if store.plan_of(farmer, record) == "thermal" else None),
            flags=(latest_flags(settings, record.id)
                   if store.plan_of(farmer, record) in text.FLYING_PLANS else None),
            download=None if download else f"{token}/file",
        )
        return page.encode("utf-8")

    # ---- the simulator ---------------------------------------------------------------------

    def sim_page(self) -> bytes:
        with self.db() as conn:
            phones = [f.phone for f in store.farmers(conn) if f.channel == "sim"]
        return _page("sim.html", {"phones": phones or [SIM_PHONE], "banner": self.settings.banner,
                                  "twilio": self.settings.twilio_ready})

    def sim_send(self, payload: dict) -> dict:
        phone = store.normalize_phone(str(payload.get("phone") or SIM_PHONE))
        media = []
        photo = payload.get("photo")
        if photo:
            header, _, blob = str(photo).partition(",")
            data = base64.b64decode(blob)
            if len(data) > MAX_PHOTO_BYTES:
                raise HttpError(413, "photo too big for a text message")
            kind = re.search(r"data:([\w/+.-]+)", header)
            extension = {"image/png": ".png", "image/gif": ".gif"}.get(
                kind.group(1) if kind else "", ".jpg")
            folder = self.settings.media_dir / phone.lstrip("+")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{datetime.now():%Y%m%d-%H%M%S}_sim{extension}"
            path.write_bytes(data)
            media.append(Media(str(path), kind.group(1) if kind else "image/jpeg"))
        lat, lon = payload.get("lat"), payload.get("lon")
        message = Inbound(phone, str(payload.get("body") or ""), media,
                          float(lat) if lat not in (None, "") else None,
                          float(lon) if lon not in (None, "") else None, "sim")
        self.receive(message, reply_status="kept")
        return {"ok": True, "phone": phone}

    # ---- work done right away, in the background ---------------------------------------

    def _today(self):
        return (self.now or self.settings.now()).date()

    def _sms_command(self, *arguments: str) -> list[str]:
        """This same program on this same farm folder, pinned to the same day."""
        pinned = ["--as-of", self._today().isoformat()] if self.now else []
        return [sys.executable, "-m", "dosojos_sms", "--data", str(self.settings.data_dir),
                *pinned, *arguments]

    def _maybe_read(self, conn, record) -> None:
        """Read a field from the satellite the moment it has all it needs.

        That is a map, a crop and (for all but trees) a planting date, from a farmer who has finished
        answering questions (the waterings they give last count too). Only with
        ``DOSOJOS_READ_NOW``; otherwise the daily run does it overnight.
        """
        if not self.settings.read_now or record.outline is None or record.crop in (None, "none"):
            return
        farmer = store.get_farmer(conn, record.phone)
        if farmer is None or farmer.state != "idle":
            return
        events = store.events_for(conn, record.id)
        planted = sorted(e.day.isoformat() for e in events
                         if e.kind == "planted" and e.voided_at is None)
        if not planted and record.crop != "citrus":
            return          # trees are never asked a planting date: the season is the year
        shape_now = json.dumps([record.outline, record.crop, planted], sort_keys=True)
        seen = self._read_as.get(record.id)
        if seen == shape_now:
            return
        self._read_as[record.id] = shape_now
        if seen is None and self.water.field(record, events, self._today()).status is not None:
            return          # read before this server started
        command = self._sms_command("daily", "--fields", record.id, "--since-planting",
                                    "--skip-baseline")
        if self.jobs.add(f"read:{record.id}", command,
                         lambda ok, attempt: self._read_done(record.id, ok, attempt)) \
                and drone_wait(self.settings, farmer, record) is None:
            outbox.deliver(conn, self.settings, farmer,
                           text.say("reading_now", farmer.language, field=record.name),
                           now=self.now, urgent=True)

    def _read_done(self, field_id: str, ok: bool, attempt: int) -> float | None:
        """Text the answer a reading came to, or say it will be tried again."""
        today = self._today()
        if ok:
            self._prepare_advice(field_id)
        with self.lock, self.db() as conn:
            record = store.get_field(conn, field_id)
            farmer = store.get_farmer(conn, record.phone)
            lang = farmer.language

            def send(body: str) -> None:
                outbox.deliver(conn, self.settings, farmer, body, now=self.now, urgent=True)

            item = (self.water.field(record, store.events_for(conn, record.id), today)
                    if ok else None)
            if item is not None and item.status is not None:
                wait = drone_wait(self.settings, farmer, record)
                if wait == "photos":
                    send(text.say("sat_ready_wait", lang, field=record.name))
                elif wait is None:
                    self._answer(conn, farmer, record)
                # "working": the flight's own finish sends everything together
                return None
            if attempt < READ_ATTEMPTS:
                wait = self.retry_seconds()
                # A drone field's answer is on hold anyway: nothing to apologise for yet.
                if drone_wait(self.settings, farmer, record) is None:
                    send(text.say("reading_retry", lang, field=record.name,
                                  minutes=max(1, round(wait / 60))))
                return wait
            send(text.say("reading_gave_up", lang, field=record.name))
            log.warning("gave up reading %s after %s tries; see the server window", field_id,
                        attempt)
            self._read_as.pop(field_id, None)
            return None

    def retry_seconds(self, local: datetime | None = None) -> float:
        """Fifteen minutes, or less when the soil service is due back sooner."""
        local = local or datetime.now(self.settings.tz)
        (h0, m0), (h1, m1) = SOIL_DOWN
        start = local.replace(hour=h0, minute=m0, second=0, microsecond=0)
        back = local.replace(hour=h1, minute=m1, second=0, microsecond=0)
        if start <= local < back:
            return min(READ_RETRY_MINUTES * 60, (back - local).total_seconds())
        return READ_RETRY_MINUTES * 60

    def _answer(self, conn, farmer, record, flight_id: str | None = None) -> bool:
        """The water answer, the page's link when a drone flew, and the menu.

        Sent once the satellite, and for a drone field its photos too, are done.
        False when the satellite reading is not in yet.
        """
        today, lang = self._today(), farmer.language
        events = store.events_for(conn, record.id)
        with_ai = ai.for_settings(self.settings) is not None
        item = self.water.field(record, events, today, full=with_ai)
        if item.status is None:
            return False

        def send(body: str) -> None:
            outbox.deliver(conn, self.settings, farmer, body, now=self.now, urgent=True)

        advice = self._advice_now(record, farmer, item, events) if with_ai else None
        if advice is not None:
            send(ai_text(record.name, advice, lang))
        else:
            send(status_message(item, lang, today, map_link=lambda r: self.settings.link(
                f"f/{map_token(conn, r.id, self.now)}")))
        if store.plan_of(farmer, record) in text.FLYING_PLANS:
            out = self.settings.drone_workspace / "out"
            found = latest_thermal(self.settings, record.id) or latest_flags(
                self.settings, record.id) or latest_terrain(self.settings, record.id)
            folder = out / (flight_id or (found or {}).get("flight_id") or "-")
            extra = ("all_ready_3d" if (folder / "model3d.json").exists() else
                     "all_ready_heat" if (folder / "thermal.json").exists() else
                     "all_ready_drone" if found or flight_id else None)
            if extra:
                token = link_token(conn, "explain", record.id, self.now)
                send(text.say("all_ready", lang, field=record.name,
                              extra=text.say(extra, lang),
                              link=self.settings.link(f"r/{token}")))
        send(self._menu(farmer, record))
        return True

    def _menu(self, farmer, record) -> str:
        """What else there is to ask, for this farmer's crop and plan."""
        lang = farmer.language
        more = text.say("menu_sorghum", lang) if record.crop == "sorghum" else ""
        if store.plan_of(farmer, record) in text.FLYING_PLANS:
            more += text.say("menu_drone", lang)
        return text.say("ready_menu", lang, more=more)

    def _process_flight(self, field_id: str, flight_id: str) -> None:
        """Run the team's step on a finished upload, when this server is told how."""
        if not self.settings.on_upload:
            return
        try:
            command = shlex.split(self.settings.on_upload) + ["--flight", flight_id]
        except ValueError as exc:            # the upload is safe; only the hook is wrong
            log.error("DOSOJOS_ON_UPLOAD cannot be read as a command (%s): %s", exc,
                      self.settings.on_upload)
            return
        self.jobs.add(f"flight:{flight_id}", command,
                      lambda ok, attempt: self._flight_done(field_id, flight_id, ok))

    def _flight_done(self, field_id: str, flight_id: str, ok: bool) -> None:
        """Text what the flight found, with its picture."""
        out = self.settings.drone_workspace / "out" / flight_id
        if ok:
            self._prepare_advice(field_id)      # it reads the flight too
        with self.lock, self.db() as conn:
            record = store.get_field(conn, field_id)
            farmer = store.get_farmer(conn, record.phone)
            lang = farmer.language
            record.answers["flight"] = "done" if ok else "failed"
            store.save_field(conn, record)
            if not ok:
                outbox.deliver(conn, self.settings, farmer,
                               text.say("flight_failed", lang, field=record.name),
                               now=self.now, urgent=True)
                self._answer(conn, farmer, record)     # the satellite answer, on its own
                return None
            extra, picture = None, None
            if (out / "thermal.json").exists():
                body = text.say("flight_ready_heat", lang, field=record.name)
                picture = out / "thermal.png"
                extra = pest_line(json.loads((out / "thermal.json").read_text(encoding="utf-8")),
                                  lang, record.name)
            elif (out / "trees_ai.json").exists() and (out / "trees_ai.png").exists():
                s = json.loads((out / "trees_ai.json").read_text(encoding="utf-8"))
                body = text.say("flight_ready_trees_ai", lang, field=record.name,
                                trees=f"{s['trees']:,}", look=f"{s['needs_a_look']:,}",
                                gaps=f"{s['gaps']:,}",
                                height=f"{(s.get('height_m') or {}).get('median') or 0:.1f}")
                picture = out / "trees_ai.png"
            elif (out / "block_summary.json").exists():
                s = json.loads((out / "block_summary.json").read_text(encoding="utf-8"))
                stressed = (s.get("n_stressed") or 0) + (s.get("n_dead") or 0)
                missing = s.get("n_missing") or 0
                key = {"cell": "flight_ready_cells",
                       "crown": "flight_ready_trees"}.get(s.get("unit_type"), "flight_ready_rows")
                body = text.say(key, lang, field=record.name,
                                problem=f"{stressed + missing:,}",
                                total=f"{s.get('n_judged') or 0:,}",
                                stressed=f"{stressed:,}", missing=f"{missing:,}")
                picture = out / "flag_overlay.png"
            else:
                body = text.say("flight_ready", lang, field=record.name)
            media = ([self._picture_link(conn, record.id, picture)]
                     if picture is not None and picture.exists() else [])
            outbox.deliver(conn, self.settings, farmer, body, now=self.now, urgent=True,
                           media=media)
            if extra:
                outbox.deliver(conn, self.settings, farmer, extra, now=self.now, urgent=True)
            if (out / "model3d.json").exists() and (out / "model3d.png").exists():
                model = json.loads((out / "model3d.json").read_text(encoding="utf-8"))
                size = model.get("size_m") or [0, 0]
                outbox.deliver(conn, self.settings, farmer,
                               text.say("flight_ready_3d", lang, field=record.name,
                                        x=f"{size[0]:.0f}", y=f"{size[1]:.0f}",
                                        top=f"{model.get('plants_top_m') or 0:.1f}"),
                               now=self.now, urgent=True,
                               media=[self._picture_link(conn, record.id, out / "model3d.png")])
            # Everything is in: the water answer and the page, all together. A reading
            # still under way sends them itself when it ends.
            self._answer(conn, farmer, record, flight_id)
        return None

    def _picture_link(self, conn, field_id: str, path: Path) -> str:
        """A link to one picture, for a text that carries it."""
        token = store.new_link(conn, "picture", field_id, days=LINK_DAYS,
                               meta={"path": str(path)}, now=self.now)
        return self.settings.link(f"p/{token}")

    def picture(self, token: str) -> bytes:
        with self.db() as conn:
            link = store.get_link(conn, token, "picture", now=self.now)
        if link is None:
            raise HttpError(404, "gone")
        path = Path(json.loads(link["meta"] or "{}").get("path", ""))
        if not path.is_file():
            raise HttpError(404, "gone")
        return path.read_bytes()

    def sim_messages(self, phone: str, after: int) -> dict:
        phone = store.normalize_phone(phone or SIM_PHONE)
        with self.db() as conn:
            rows = store.messages(conn, phone, after=after)
            farmer = store.get_farmer(conn, phone)
        return {"state": farmer.state if farmer else "new", "messages": [
            {"id": r["id"], "direction": r["direction"], "body": r["body"],
             "media": [p if p.startswith("http") else Path(p).name
                       for p in json.loads(r["media"] or "[]")],
             "status": r["status"], "at": r["created_at"],
             "segments": text.segments(r["body"]) if r["direction"] == "out" else None}
            for r in rows]}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    app: App          # set on the subclass made by make_server
    server_version = "DosOjosSMS/0.1"

    def log_message(self, fmt: str, *args) -> None:     # quieter than stderr per request
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---- plumbing -------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, kind: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _body(self, limit: int = MAX_FORM_BYTES) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise HttpError(413, "too big")
        return self.rfile.read(length) if length else b""

    def _form(self) -> dict[str, str]:
        parsed = urllib.parse.parse_qs(self._body().decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def _payload(self, limit: int = MAX_FORM_BYTES) -> dict:
        try:
            payload = json.loads(self._body(limit) or b"{}")
        except json.JSONDecodeError as exc:
            raise HttpError(400, "not JSON") from exc
        if not isinstance(payload, dict):
            raise HttpError(400, "expected an object")
        return payload

    def _sim_allowed(self) -> None:
        if not self.app.sim:
            raise HttpError(404, "not found")
        if any(self.headers.get(h) for h in FORWARDED):
            raise HttpError(403, "the simulator only answers on this computer")

    def _gone(self) -> None:
        lang = "en" if "en" in (self.headers.get("Accept-Language") or "")[:2] else "es"
        message = text.pick(PAGE_TEXT["gone"], lang)
        self._send(404, f"<!doctype html><meta charset=utf-8><meta name=viewport "
                        f"content='width=device-width'><body style='font:18px system-ui;"
                        f"padding:24px'><p>{message}</p>".encode("utf-8"))

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path)
        parts = [p for p in path.path.split("/") if p]
        query = dict(urllib.parse.parse_qsl(path.query))
        app = self.app
        try:
            if method in ("GET", "HEAD"):
                if not parts:
                    self._send(200, b"Dos Ojos SMS is running.", "text/plain; charset=utf-8")
                elif parts == ["health"]:
                    self._send(200, b"ok", "text/plain")
                elif len(parts) == 2 and parts[0] == "f":
                    self._send(200, app.map_page(parts[1]))
                elif len(parts) == 2 and parts[0] == "u":
                    self._send(200, app.upload_page(parts[1]))
                elif len(parts) == 3 and parts[0] == "u" and parts[2] == "files":
                    self._json(app.upload_list(parts[1]))
                elif len(parts) == 2 and parts[0] == "r":
                    self._send(200, app.explain_page(parts[1]))
                elif len(parts) == 3 and parts[0] == "r" and parts[2] == "file":
                    body = app.explain_page(parts[1], download=True)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Disposition",
                                     'attachment; filename="dos-ojos.html"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                elif len(parts) == 2 and parts[0] == "p":
                    self._send(200, app.picture(parts[1]), "image/png")
                elif parts == ["sim"]:
                    self._sim_allowed()
                    self._send(200, app.sim_page())
                elif parts == ["sim", "messages"]:
                    self._sim_allowed()
                    self._json(app.sim_messages(query.get("phone", ""),
                                                int(query.get("after") or 0)))
                else:
                    raise HttpError(404, "not found")
            elif method == "POST":
                if parts == ["sms", "twilio"]:
                    body = app.twilio_webhook(self._form(), self.headers.get("X-Twilio-Signature"))
                    self._send(200, body, "text/xml; charset=utf-8")
                elif parts == ["sms", "status"]:
                    app.twilio_status(self._form(), self.headers.get("X-Twilio-Signature"))
                    self._send(204, b"")
                elif len(parts) == 2 and parts[0] == "f":
                    self._json(app.save_outline(parts[1], self._payload()))
                elif len(parts) == 3 and parts[0] == "u" and parts[2] == "done":
                    self._json(app.upload_done(parts[1]))
                elif parts == ["sim", "send"]:
                    self._sim_allowed()
                    self._json(app.sim_send(self._payload(limit=2 * MAX_PHOTO_BYTES)))
                else:
                    raise HttpError(404, "not found")
            elif method == "PUT":
                if len(parts) == 4 and parts[0] == "u" and parts[2] == "file":
                    length = int(self.headers.get("Content-Length") or 0)
                    try:
                        piece = {k: int(query[k]) for k in ("offset", "total") if k in query}
                    except ValueError as exc:
                        raise HttpError(400, "offset and total must be numbers") from exc
                    counted = _Counted(self.rfile)
                    try:
                        self._json(app.upload_file(parts[1], parts[3], length, counted, **piece))
                    except HttpError:
                        # Refused before reading it all: take in the rest of a small body,
                        # or Windows cuts the line before the browser reads why.
                        if length - counted.read_bytes <= MAX_FORM_BYTES:
                            self.rfile.read(max(0, length - counted.read_bytes))
                        raise
                else:
                    raise HttpError(404, "not found")
        except HttpError as exc:
            if exc.status == 404 and str(exc) == "gone" and method == "GET":
                self._gone()
            elif path.path.startswith(("/f/", "/u/", "/sim")) and method != "GET":
                self._json({"ok": False, "error": str(exc)}, exc.status)
            else:
                self._send(exc.status, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
        except ConnectionError:
            raise                              # nobody left to answer; see do_GET/do_PUT
        except Exception:                      # a bug: say so, and keep the server up
            log.exception("error answering %s %s", method, self.path)
            self._send(500, b"internal error; see the server log", "text/plain")

    def do_GET(self) -> None:
        try:
            self._dispatch("GET")
        except ConnectionError:
            # The other side (a phone, Telegram, the tunnel) stopped listening.
            log.info("%s was cut off by the other side", self.path.split("?")[0])
            self.close_connection = True

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        try:
            self._dispatch("POST")
        except ConnectionError:
            log.info("%s was cut off by the other side", self.path.split("?")[0])
            self.close_connection = True

    def do_PUT(self) -> None:
        try:
            self._dispatch("PUT")
        except ConnectionError:
            # The browser or tunnel hung up mid-upload; the page tries that piece again.
            log.info("an upload to %s was cut off by the other side", self.path.split("?")[0])
            self.close_connection = True


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def start_flusher(app: App, every_s: float = 60.0) -> threading.Thread:
    """Send the texts held overnight once it is morning, while the server runs."""
    def loop() -> None:
        while True:
            try:
                with app.lock, app.db() as conn:
                    sent = outbox.flush(conn, app.settings)
                if sent:
                    log.info("sent %d text(s) held for the morning", sent)
            except Exception:
                log.exception("sending held texts failed")
            time.sleep(every_s)

    thread = threading.Thread(target=loop, name="outbox", daemon=True)
    thread.start()
    return thread
