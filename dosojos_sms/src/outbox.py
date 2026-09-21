"""Getting texts out to farmers.

Replies go back inside Twilio's webhook answer. Everything else (the map saved,
the photos received, an alert, a team message) goes through :func:`deliver`:

- never to a farmer who replied STOP;
- nothing unasked between 8 pm and 8 am farm time: it waits for the morning;
- through Twilio only when it is set up and the farmer came in by real SMS, and
  through Telegram only when its bot is set up and the farmer came in there.
  Farmers made in the simulator or the terminal keep their texts in the
  database, where those screens show them. Nothing reaches a phone by accident.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone

from . import store, telegram, text, twilio
from .config import QUIET_END_HOUR, QUIET_START_HOUR, Settings
from .store import Farmer

log = logging.getLogger(__name__)


def can_send(settings: Settings, channel: str) -> bool:
    """True when texts on this channel really leave the computer."""
    if channel == "sms":
        return settings.twilio_ready
    if channel == "telegram":
        return bool(settings.telegram_token)
    return False


def quiet(now_local: datetime) -> bool:
    return not (QUIET_START_HOUR <= now_local.hour < QUIET_END_HOUR)


def next_morning(now_local: datetime) -> datetime:
    """8 am farm time: today if it is still early, else tomorrow."""
    start = datetime.combine(now_local.date(), time(QUIET_START_HOUR), now_local.tzinfo)
    return start if now_local < start else start + timedelta(days=1)


def deliver(conn: sqlite3.Connection, settings: Settings, farmer: Farmer, body: str, *,
            now: datetime | None = None, urgent: bool = False,
            media: list[str] | None = None) -> str:
    """Send one text the farmer did not just ask for; returns what became of it.

    ``urgent`` is for answers to something the farmer did a moment ago, such as
    saving a map, which go out at any hour. ``media`` are links to pictures the
    text carries; a phone only gets them when the links are public (https).
    """
    body = text.gsm_safe(body)
    now = now or settings.now()
    if farmer.opted_out_at:
        log.info("not texting %s: they opted out", farmer.phone)
        return "opted out"
    if not can_send(settings, farmer.channel):
        store.log_out(conn, farmer.phone, body, status="kept", media=media)
        return "kept"
    if not urgent and quiet(now):
        morning = next_morning(now).astimezone(timezone.utc).isoformat(timespec="seconds")
        store.log_out(conn, farmer.phone, body, status="queued", send_after=morning,
                      media=media)
        return "queued"
    message_id = store.log_out(conn, farmer.phone, body, status="sending", media=media)
    return _send(conn, settings, farmer.phone, body, message_id, media)


def _send(conn: sqlite3.Connection, settings: Settings, phone: str, body: str,
          message_id: int, media: list[str] | None = None) -> str:
    try:
        if telegram.is_telegram(phone):
            local = [_our_picture(conn, settings, url) or url for url in media or ()]
            sid = telegram.send(settings, phone, body, media=local)
        else:
            sid = (twilio.send(settings, phone, body, media=media) if media
                   else twilio.send(settings, phone, body))
    except (twilio.TwilioError, telegram.TelegramError) as exc:
        store.mark_message(conn, message_id, status="failed", error=str(exc))
        log.warning("text to %s failed: %s", phone, exc)
        if exc.code == 21610:
            record = store.get_farmer(conn, phone)
            if record and not record.opted_out_at:
                record.opted_out_at = store.now_iso()
                store.save_farmer(conn, record)
        return "failed"
    store.mark_message(conn, message_id, status="sent", provider_id=sid)
    return "sent"


def _our_picture(conn: sqlite3.Connection, settings: Settings, url: str) -> str | None:
    """The file behind one of this server's own picture links (/p/<token>), if any.

    Read on the connection already open: a second one would wait on this one's
    unfinished write and fail with "database is locked".
    """
    prefix = settings.link("p/")
    if not url.startswith(prefix):
        return None
    link = store.get_link(conn, url[len(prefix):], "picture")
    if link is None:
        return None
    path = json.loads(link["meta"] or "{}").get("path")
    return path or None


def flush(conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None) -> int:
    """Send the texts held overnight that are now due; returns how many went out."""
    now = now or settings.now()
    if quiet(now):
        return 0
    sent = 0
    for row in store.queued(conn, now.astimezone(timezone.utc).isoformat(timespec="seconds")):
        farmer = store.get_farmer(conn, row["phone"])
        if farmer is None or farmer.opted_out_at:
            store.mark_message(conn, row["id"], status="failed", error="opted out before sending")
            continue
        if not can_send(settings, farmer.channel):
            store.mark_message(conn, row["id"], status="kept")
            continue
        sent += _send(conn, settings, row["phone"], row["body"], row["id"],
                      json.loads(row["media"] or "[]")) == "sent"
    return sent
