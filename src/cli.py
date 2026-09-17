"""Click command line for the Dos Ojos SMS front door."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import webbrowser
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import click
from shapely.geometry import mapping, shape

from dosojos_sat.fields import compute_acres, utm_epsg_for

from . import export as export_mod
from . import outbox, store, text
from .bot import Bot, Inbound, Media, Turn, map_token
from .config import DEFAULT_PORT, LINK_DAYS, ConfigError, Settings, setup_logging
from .status import Water, latest_terrain
from .web import SIM_PHONE, App, make_server, start_flusher

log = logging.getLogger(__name__)


@dataclass
class Context:
    settings: Settings
    as_of: date | None

    def now(self) -> datetime:
        """Farm time, or noon on the pinned day for a demo that must not drift."""
        if self.as_of:
            return datetime.combine(self.as_of, time(12), self.settings.tz)
        return self.settings.now()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--data", "data_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Farm data folder  [default: Dos_Ojos/farm_data]")
@click.option("--as-of", "as_of", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Answer as if it were this day, for demos that must not drift  "
                   "[default: DOSOJOS_AS_OF in the folder's sms.env, else today]")
@click.option("-v", "--verbose", count=True, help="Show debug logging.")
@click.pass_context
def cli(ctx: click.Context, data_dir: Path | None, as_of: datetime | None, verbose: int) -> None:
    """Dos Ojos by text message: farmers register fields and irrigations by SMS."""
    setup_logging(verbose)
    try:
        settings = Settings.load(data_dir)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    settings.ensure_dirs()
    ctx.obj = Context(settings, as_of.date() if as_of else settings.as_of)


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    if not rows:
        return "(none)"
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    line = lambda cells: "  ".join(f"{c:<{w}}" for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join([line(headers), line(tuple("-" * w for w in widths)), *map(line, rows)])


def _app(ctx: Context, **options) -> App:
    app = App(ctx.settings, **options)
    app.now = ctx.now() if ctx.as_of else None
    return app


# --------------------------------------------------------------------------- #
# Talking
# --------------------------------------------------------------------------- #


@cli.command("serve")
@click.option("--port", type=int, default=DEFAULT_PORT, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True,
              help="127.0.0.1 keeps it on this computer; a tunnel reaches it there.")
@click.option("--sim", is_flag=True, help="Also serve the phone simulator at /sim.")
@click.option("--no-verify", is_flag=True,
              help="Accept webhook calls without Twilio's signature. Testing only.")
@click.option("--open", "open_browser", is_flag=True,
              help="Open the simulator (or the front page) in the browser once listening.")
@click.pass_obj
def serve_cmd(ctx: Context, port: int, host: str, sim: bool, no_verify: bool,
              open_browser: bool) -> None:
    """Run the web side: Twilio's webhook, the map and upload pages, the simulator."""
    if ctx.settings.public_url == f"http://localhost:{DEFAULT_PORT}" and port != DEFAULT_PORT:
        # Links in texts point here until a public address is set.
        ctx.settings = replace(ctx.settings, public_url=f"http://localhost:{port}")
    settings = ctx.settings
    app = _app(ctx, sim=sim, verify=not no_verify)
    try:
        server = make_server(app, host, port)
    except OSError as exc:
        raise click.ClickException(f"cannot listen on {host}:{port} ({exc}); is another "
                                   "server already running? Try --port 8081.") from exc
    start_flusher(app)
    local = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    click.echo(f"Dos Ojos SMS on {local}   (Ctrl+C to stop)")
    click.echo(f"  data       {settings.data_dir}")
    if sim:
        click.echo(f"  simulator  {local}/sim")
    if settings.twilio_ready:
        click.echo(f"  Twilio     on: texts go to real phones from {settings.twilio_from or settings.twilio_service}")
        click.echo(f"  webhook    {settings.link('sms/twilio')}  (set this in the Twilio console)")
        if not settings.public_url.startswith("https://"):
            click.secho("  WARNING: DOSOJOS_PUBLIC_URL is not an https address, so Twilio "
                        "cannot reach this server and links in texts will not open on "
                        "phones.", fg="yellow")
    else:
        click.echo("  Twilio     not set up: texts stay on this computer (simulator, 'chat')")
    if ctx.as_of:
        click.secho(f"  pinned to {ctx.as_of} (--as-of or DOSOJOS_AS_OF)", fg="yellow")
    if open_browser:
        # The socket is already listening, so the page's first request waits for us
        # instead of failing as it would if the browser raced the start-up.
        webbrowser.open(f"{local}/sim" if sim else local)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nstopped")
    finally:
        server.server_close()


@cli.command("chat")
@click.option("--phone", default=SIM_PHONE, show_default=True,
              help="The pretend farmer's number.")
@click.pass_obj
def chat_cmd(ctx: Context, phone: str) -> None:
    """Text the bot from this terminal, as a farmer would. Blank line or Ctrl+C quits.

    Start a line with 'photo:' and a file path to send a picture.
    """
    app = _app(ctx)
    phone = store.normalize_phone(phone)
    with app.db() as conn:
        seen = max((r["id"] for r in store.messages(conn, phone)), default=0)
    click.echo(f"Texting as {phone}. Try: hola")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        seen = _chat_send(app, phone, line, seen)


def _chat_send(app: App, phone: str, line: str, seen: int) -> int:
    media = []
    if line.lower().startswith("photo:"):
        path = Path(line.split(":", 1)[1].strip().strip('"'))
        if not path.exists():
            click.secho(f"no such file: {path}", fg="red")
            return seen
        media, line = [Media(str(path.resolve()))], ""
    app.receive(Inbound(phone, line, media, channel="console"), reply_status="kept")
    with app.db() as conn:
        rows = [r for r in store.messages(conn, phone, after=seen) if r["direction"] == "out"]
        last = max((r["id"] for r in store.messages(conn, phone, after=seen)), default=seen)
    for row in rows:
        click.secho(f"  {row['body']}", fg="green")
        click.secho(f"  ({text.segments(row['body'])} SMS)", fg="bright_black")
    return last


@cli.command("replay")
@click.argument("script", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--phone", default=SIM_PHONE, show_default=True)
@click.option("--channel", type=click.Choice(["sim", "console"]), default="sim", show_default=True)
@click.pass_obj
def replay_cmd(ctx: Context, script: Path, phone: str, channel: str) -> None:
    """Send a file of texts, one per line, as a farmer; '#' lines are comments.

    '@photo <path>' sends a picture, '@pin <lat> <lon>' a shared location.
    """
    app = _app(ctx)
    phone = store.normalize_phone(phone)
    for raw in script.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        media, lat, lon, body = [], None, None, line
        if line.startswith("@photo "):
            path = (script.parent / line.split(" ", 1)[1].strip()).resolve()
            media, body = [Media(str(path))], ""
        elif line.startswith("@pin "):
            lat, lon = (float(v) for v in line.split()[1:3])
            body = ""
        replies = app.receive(Inbound(phone, body, media, lat, lon, channel), reply_status="kept")
        click.echo(f"> {line}")
        for reply in replies:
            click.secho(f"  {reply}", fg="green")


@cli.command("say")
@click.argument("phone")
@click.argument("message")
@click.pass_obj
def say_cmd(ctx: Context, phone: str, message: str) -> None:
    """Text a farmer from the team (after 8 pm it waits for the morning)."""
    with store.session(ctx.settings.db_path) as conn:
        farmer = store.get_farmer(conn, store.normalize_phone(phone))
        if farmer is None:
            raise click.ClickException(f"no farmer with number {phone}")
        result = outbox.deliver(conn, ctx.settings, farmer, message, now=ctx.now())
    click.echo(f"{farmer.phone}: {result}")


# --------------------------------------------------------------------------- #
# Looking
# --------------------------------------------------------------------------- #


@cli.command("farmers")
@click.pass_obj
def farmers_cmd(ctx: Context) -> None:
    """Everyone who has texted in."""
    with store.session(ctx.settings.db_path) as conn:
        rows = []
        for farmer in store.farmers(conn):
            count = len(store.fields_of(conn, farmer.phone))
            alerts = {True: "yes", False: "no", None: "-"}[farmer.alerts]
            rows.append((farmer.phone, farmer.name or "-", farmer.language, farmer.channel,
                         str(count), alerts, "STOPPED" if farmer.opted_out_at else farmer.state))
    click.echo(_table(("PHONE", "NAME", "LANG", "CHANNEL", "FIELDS", "ALERTS", "STATE"), rows))


@cli.command("fields")
@click.pass_obj
def fields_cmd(ctx: Context) -> None:
    """Every field, what we know about it, and what is still missing."""
    settings = ctx.settings
    with store.session(settings.db_path) as conn:
        bot = Bot(conn, settings, now=ctx.now())
        rows = []
        for record in store.all_fields(conn):
            farmer = store.get_farmer(conn, record.phone)
            turn = Turn(bot, farmer, Inbound(record.phone))
            events = store.events_for(conn, record.id)
            planted = max((e.day for e in events if e.kind == "planted"), default=None)
            watered = max((e.day for e in events if e.kind == "irrigated"), default=None)
            size = (f"{record.acres:.1f}" if record.acres else "-") + (
                f" ({record.acres_said:g})" if record.acres_said else "")
            ground = "yes" if latest_terrain(settings, record.id) else "-"
            rows.append((record.id, (farmer.name or record.phone)[:16], record.name[:18],
                         text.crop_name(record.crop, "en", record.crop_name) if record.crop else "-",
                         size, str(planted or "-"), record.irrigation or "-",
                         record.water_enters or "-", str(watered or "-"), ground,
                         ", ".join(turn.missing_all(record)) or "complete"))
    click.echo(_table(("FIELD", "FARMER", "NAME", "CROP", "ACRES (SAID)", "PLANTED", "METHOD",
                       "WATER IN", "LAST WATERED", "GROUND", "MISSING"), rows))


@cli.command("messages")
@click.option("--phone", default=None, help="Only this farmer.")
@click.option("--unread", is_flag=True, help="Only texts the bot did not understand.")
@click.option("--mark-read", is_flag=True, help="Mark the unread ones shown as read.")
@click.option("--last", type=int, default=40, show_default=True)
@click.pass_obj
def messages_cmd(ctx: Context, phone: str | None, unread: bool, mark_read: bool, last: int) -> None:
    """The conversation log."""
    with store.session(ctx.settings.db_path) as conn:
        rows = store.messages(conn, store.normalize_phone(phone) if phone else None,
                              unread_only=unread, limit=100_000)[-last:]
        for row in rows:
            arrow = "<-" if row["direction"] == "in" else "->"
            flag = " [UNREAD]" if row["unread"] else ""
            media = json.loads(row["media"] or "[]")
            extra = f" [{len(media)} photo(s)]" if media else ""
            click.echo(f"{row['created_at'][:16]} {row['phone']} {arrow} {row['body']}{extra}"
                       f"{flag}" + (f"  ({row['status']})" if row["direction"] == "out" and
                                    row["status"] not in ("sent", "kept") else ""))
        if mark_read:
            store.mark_read(conn, [r["id"] for r in rows if r["unread"]])


@cli.command("todo")
@click.pass_obj
def todo_cmd(ctx: Context) -> None:
    """What the team has to do: texts to answer, fields to draw, flights to process."""
    settings = ctx.settings
    items: list[str] = []
    with store.session(settings.db_path) as conn:
        for row in store.messages(conn, unread_only=True, limit=100_000):
            farmer = store.get_farmer(conn, row["phone"])
            who = f"{farmer.name} " if farmer and farmer.name else ""
            items.append(f"ANSWER  {who}{row['phone']}: {row['body'] or '[photo]'!r}  "
                         f"(sms say {row['phone']} \"...\"; then sms messages --unread --mark-read)")
        for record in store.all_fields(conn):
            if record.outline is None and record.lat is None and record.place:
                items.append(f"DRAW    {record.id} {record.name}: described as {record.place!r}. "
                             f"Find it, then: sms link {record.id}")
            elif record.outline is None and record.lat is not None:
                items.append(f"MAP     {record.id} {record.name}: pin sent, corners not tapped yet "
                             f"(or draw it: sms link {record.id})")
        for upload in store.uploads(conn):
            if not upload["done_at"]:
                continue
            odm = settings.drone_workspace / "data" / "odm" / upload["flight_id"]
            if not odm.exists():
                items.append(f"FLIGHT  {upload['flight_id']}: {upload['files']} files in "
                             f"{upload['folder']}. Run the drone half on it (survey, odm, ...) with "
                             f"--workspace {settings.drone_workspace}")
        bot = Bot(conn, settings, now=ctx.now())
        for record in store.all_fields(conn):
            events = store.events_for(conn, record.id)
            harvested = [e.day for e in events if e.kind == "harvested"]
            planted = [e.day for e in events if e.kind == "planted"]
            bare = record.crop == "none" or (harvested and (not planted or max(harvested) > max(planted)))
            if bare and record.outline is not None and not latest_terrain(settings, record.id):
                items.append(f"FLY     {record.id} {record.name}: bare now, and no ground map yet; "
                             "the best time for a flight over bare soil")
    click.echo("\n".join(items) if items else "Nothing to do.")


# --------------------------------------------------------------------------- #
# Maps drawn by the team
# --------------------------------------------------------------------------- #


@cli.command("link")
@click.argument("field_id")
@click.pass_obj
def link_cmd(ctx: Context, field_id: str) -> None:
    """A map link for the team to draw a field; saving it texts the farmer."""
    with store.session(ctx.settings.db_path) as conn:
        record = store.get_field(conn, field_id)
        if record is None:
            raise click.ClickException(f"no field {field_id}")
        token = store.new_link(conn, "map", field_id, days=LINK_DAYS, meta={"by": "team"},
                               now=ctx.now())
    click.echo(ctx.settings.link(f"f/{token}"))
    click.echo("Open it while 'serve' runs; the farmer gets a text with the acres once it is saved.")


def read_outline(path: Path, feature_id: str | None = None) -> dict:
    """A polygon from a GeoJSON file or a Google Earth KML file.

    ``feature_id`` picks one feature out of a collection by its ``id`` property.
    """
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".kml":
        tree = ElementTree.fromstring(raw)
        found = [n.text for n in tree.iter() if n.tag.split("}")[-1] == "coordinates" and n.text]
        if not found:
            raise click.ClickException(f"{path}: no <coordinates> in the KML")
        ring = [[float(v) for v in point.split(",")[:2]] for point in found[0].split()]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return {"type": "Polygon", "coordinates": [ring]}
    payload = json.loads(raw)
    if payload.get("type") == "FeatureCollection":
        features = payload.get("features") or [{}]
        if feature_id is not None:
            features = [f for f in features if str((f.get("properties") or {}).get("id")) == feature_id]
            if not features:
                raise click.ClickException(f"{path}: no feature with id {feature_id!r}")
        payload = features[0]
    if payload.get("type") == "Feature":
        payload = payload.get("geometry") or {}
    if payload.get("type") not in ("Polygon", "MultiPolygon"):
        raise click.ClickException(f"{path}: expected a Polygon, got {payload.get('type')!r}")
    return payload


@cli.command("outline")
@click.argument("field_id")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--id", "feature_id", default=None,
              help="Which feature of a collection, by its id property  [default: the first]")
@click.option("--quiet", is_flag=True, help="Do not text the farmer.")
@click.pass_obj
def outline_cmd(ctx: Context, field_id: str, path: Path, feature_id: str | None,
                quiet: bool) -> None:
    """Set a field's outline from a GeoJSON or KML file (e.g. drawn in Google Earth)."""
    geometry = shape(read_outline(path, feature_id))
    if not geometry.is_valid:
        raise click.ClickException(f"{path}: the outline crosses itself; redraw it")
    acres = compute_acres(geometry, utm_epsg_for(geometry))
    with store.session(ctx.settings.db_path) as conn:
        record = store.get_field(conn, field_id)
        if record is None:
            raise click.ClickException(f"no field {field_id}")
        record.outline, record.acres = mapping(geometry), round(acres, 2)
        record.outline_by, record.outline_at = "team", store.now_iso()
        store.save_field(conn, record)
        farmer = store.get_farmer(conn, record.phone)
        click.echo(f"{field_id} {record.name}: {acres:.1f} acres"
                   + (f" (the farmer said {record.acres_said:g})" if record.acres_said else ""))
        if not quiet:
            body = text.say("map_by_team", farmer.language, field=record.name,
                            acres=f"{acres:.1f}",
                            link=ctx.settings.link(f"f/{map_token(conn, record.id, ctx.now())}"))
            click.echo(f"text to {farmer.phone}: "
                       f"{outbox.deliver(conn, ctx.settings, farmer, body, now=ctx.now())}")


# --------------------------------------------------------------------------- #
# Into the two halves, and back out as alerts
# --------------------------------------------------------------------------- #


@cli.command("export")
@click.pass_obj
def export_cmd(ctx: Context) -> None:
    """Write fields.geojson and field_log.csv into the farm's satellite workspace."""
    _export(ctx)


def _export(ctx: Context) -> export_mod.Exported:
    with store.session(ctx.settings.db_path) as conn:
        try:
            result = export_mod.export(conn, ctx.settings)
        except export_mod.FieldValidationError as exc:
            raise click.ClickException(f"the satellite half refused an outline: {exc}") from exc
    click.echo(f"{len(result.field_ids)} field(s) and {result.events} event(s) written to "
               f"{result.fields_path.parent}")
    for field_id, reason in result.skipped:
        click.echo(f"  not yet: {field_id} ({reason})")
    return result


def _sat(ctx: Context, *args: str) -> None:
    """Run one satellite command on the farm workspace, in this same Python."""
    command = [sys.executable, "-m", "dosojos_sat", "--workspace",
               str(ctx.settings.sat_workspace), *args]
    click.secho(f"\n$ dosojos-sat {' '.join(args)}", fg="cyan")
    if subprocess.run(command, check=False).returncode != 0:
        raise click.ClickException(f"'dosojos-sat {' '.join(args)}' failed; see its message above")


#: Years of history behind "what this field usually does on this date". Four is
#: what the satellite half's baseline defaults to, and the least that makes a
#: percentile mean anything.
BASELINE_YEARS = 4


@cli.command("daily")
@click.option("--send", is_flag=True, help="Send the alerts and reminders (else list them).")
@click.option("--skip-fetch", is_flag=True, help="Use the imagery already cached.")
@click.option("--years", type=int, default=BASELINE_YEARS + 1, show_default=True,
              help="Years of imagery to keep: this season, plus the years the field's "
                   "own normal is built from.")
@click.option("--skip-baseline", is_flag=True,
              help="Don't rebuild each field's own normal (it changes slowly).")
@click.pass_obj
def daily_cmd(ctx: Context, send: bool, skip_fetch: bool, years: int,
              skip_baseline: bool) -> None:
    """Once a day: export, fetch imagery, weather and soil, then alerts and reminders."""
    result = _export(ctx)
    if not result.field_ids:
        click.echo("No field has a map and a crop yet; nothing for the satellite to look at.")
        return
    today = ctx.as_of or ctx.settings.now().date()
    _sat(ctx, "init-fields", str(result.fields_path))
    if not skip_fetch and ctx.as_of:
        # A pinned demo needs its own years, not every image up to the real today.
        _sat(ctx, "fetch", "--start", date(today.year - years + 1, 1, 1).isoformat(),
             "--end", today.isoformat())
    elif not skip_fetch:
        _sat(ctx, "fetch", "--years", str(years))
    _sat(ctx, "weather", "--start", (today - timedelta(days=400)).isoformat(),
         "--end", today.isoformat())
    _sat(ctx, "soil")
    if not skip_baseline:
        # What each field usually does on this date, from its own earlier years.
        # Only the "why" page draws it, so a field with too little history behind
        # it costs a chart and never a failed run.
        try:
            _sat(ctx, "baseline", "--season", str(today.year),
                 "--history-years", str(BASELINE_YEARS))
        except click.ClickException as exc:
            click.secho(f"NOTE: no field normal yet ({exc.message}); the water advice "
                        "does not need one.", fg="yellow")
    growing = [i for i in result.field_ids if _crop_of(ctx, i) not in (None, "none")]
    if growing:
        _sat(ctx, "water", "--as-of", today.isoformat(), "--fields", ",".join(growing))
    click.secho("\n$ dosojos-sms remind" + (" --send" if send else ""), fg="cyan")
    _remind(ctx, send)


def _crop_of(ctx: Context, field_id: str) -> str | None:
    with store.session(ctx.settings.db_path) as conn:
        record = store.get_field(conn, field_id)
    return record.crop if record else None


@dataclass
class Planned:
    farmer: store.Farmer
    field_id: str
    kind: str
    key: str
    body: str
    ask: str | None = None       # a field question the farmer is put on


#: Missing facts worth a reminder, most important first.
REMIND_ORDER = ("map", "crop", "planted", "method", "last_irrigation", "side")
REMIND_EVERY = timedelta(days=7)
CHECKIN_AFTER = timedelta(days=3)


def plan_reminders(conn, settings: Settings, bot: Bot, today: date) -> list[Planned]:
    """At most one text per farmer: water first, then a check-in, then a missing fact."""
    from dosojos_sat.water import STATUS_HARVESTED

    planned: list[Planned] = []
    for farmer in store.farmers(conn):
        if farmer.opted_out_at or not farmer.lang or farmer.alerts is not True:
            continue
        options: list[Planned] = []
        lang = farmer.language
        for record in store.fields_of(conn, farmer.phone):
            item = bot.water.field(record, store.events_for(conn, record.id), today)
            s = item.status
            if s is not None and s.status != STATUS_HARVESTED and s.method != "none":
                cycle = s.last_irrigation or s.start
                gross = text.inches(round(s.refill_gross_in or s.refill_net_in or 0, 1))
                sent_now = store.last_alert(conn, record.id, "water_now")
                if s.days_left == 0 and not store.alert_sent(conn, record.id, "water_now", cycle):
                    options.append(Planned(farmer, record.id, "water_now", cycle, text.say(
                        "alert_now", lang, field=record.name, gross=gross,
                        method=text.method_name(s.method, lang))))
                elif (s.days_left is not None and 0 < s.days_left <= 3
                      and not store.alert_sent(conn, record.id, "water_soon", cycle)):
                    options.append(Planned(farmer, record.id, "water_soon", cycle, text.say(
                        "alert_soon", lang, field=record.name,
                        about=text.about_days(s.days_left, lang),
                        date=text.day(date.fromisoformat(s.water_by), lang, today))))
                elif (s.days_left == 0 and sent_now and farmer.state == "idle"
                      and today - sent_now.date() >= CHECKIN_AFTER
                      and not store.alert_sent(conn, record.id, "checkin", cycle)):
                    options.append(Planned(farmer, record.id, "checkin", cycle,
                                           text.say("checkin", lang, field=record.name)))
            # Sorghum, watered or not: once a week while sugarcane aphid is worth scouting.
            stage = s.stage if (s is not None and s.status != STATUS_HARVESTED
                                and record.crop == "sorghum") else None
            if stage and "sugarcane_aphid" in (stage.get("watch") or []):
                year, week, _ = today.isocalendar()
                key = f"{year}-W{week:02d}"
                if not store.alert_sent(conn, record.id, "scout", key):
                    midge = (text.say("alert_scout_midge", lang)
                             if "midge" in stage["watch"] else "")
                    options.append(Planned(farmer, record.id, "scout", key, text.say(
                        "alert_scout", lang, field=record.name, midge=midge,
                        stage=text.pick(text.SORGHUM_STAGES[stage["stage"]], lang))))
            if farmer.state != "idle":
                continue
            last = store.last_alert(conn, record.id, "missing")
            if last and datetime.now(last.tzinfo) - last < REMIND_EVERY:
                continue
            missing = Turn(bot, farmer, Inbound(farmer.phone)).missing_all(record)
            question = next((q for q in REMIND_ORDER if q in missing), None)
            if question == "map" and record.lat is not None:
                options.append(Planned(farmer, record.id, "missing", f"map:{today}", text.say(
                    "remind_map", lang, field=record.name,
                    link=settings.link(f"f/{map_token(conn, record.id, bot.now)}"))))
            elif question and question != "map":
                options.append(Planned(farmer, record.id, "missing", f"{question}:{today}",
                                       text.say("remind_missing", lang, field=record.name),
                                       ask=question))
        order = ("water_now", "water_soon", "checkin", "scout", "missing")
        if options:
            planned.append(min(options, key=lambda p: order.index(p.kind)))
    return planned


def _remind(ctx: Context, send: bool) -> None:
    settings = ctx.settings
    today = ctx.as_of or settings.now().date()
    with store.session(settings.db_path) as conn:
        bot = Bot(conn, settings, now=ctx.now())
        planned = plan_reminders(conn, settings, bot, today)
        if not planned:
            click.echo("No alerts or reminders due.")
        for item in planned:
            body = item.body
            if send:
                if item.ask:
                    body += bot.ask(item.farmer, item.ask, item.field_id) or ""
                elif item.kind == "checkin":
                    bot.checkin(item.farmer, item.field_id)
                result = outbox.deliver(conn, settings, item.farmer, body, now=ctx.now())
                store.record_alert(conn, item.field_id, item.kind, item.key)
            else:
                if item.ask:
                    body += bot.ask(item.farmer, item.ask, item.field_id, save=False) or ""
                result = "not sent (add --send)"
            click.echo(f"{item.farmer.phone} {item.field_id} {item.kind:<10} {result}: {body}")


@cli.command("remind")
@click.option("--send", is_flag=True, help="Send them (else only list what would go).")
@click.pass_obj
def remind_cmd(ctx: Context, send: bool) -> None:
    """Water alerts, "have you watered?" check-ins and missing-fact reminders."""
    _remind(ctx, send)


if __name__ == "__main__":  # pragma: no cover  (last, once every command is registered)
    cli()
