"""Getting texts out to farmers.

Replies go back inside Twilio's webhook answer. Everything else (the map saved,
the photos received, an alert, a team message) goes through :func:`deliver`:

- never to a farmer who replied STOP;
- nothing unasked between 8 pm and 8 am farm time: it waits for the morning;
- through Twilio only when it is set up and the farmer came in by real SMS.
  Farmers made in the simulator or the terminal keep their texts in the
  database, where those screens show them. Nothing reaches a phone by accident.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone

from . import store, text, twilio
from .config import QUIET_END_HOUR, QUIET_START_HOUR, Settings
from .store import Farmer

log = logging.getLogger(__name__)


def quiet(now_local: datetime) -> bool:
    return not (QUIET_START_HOUR <= now_local.hour < QUIET_END_HOUR)


def next_morning(now_local: datetime) -> datetime:
    """8 am farm time: today if it is still early, else tomorrow."""
    start = datetime.combine(now_local.date(), time(QUIET_START_HOUR), now_local.tzinfo)
    return start if now_local < start else start + timedelta(days=1)


def deliver(conn: sqlite3.Connection, settings: Settings, farmer: Farmer, body: str, *,
            now: datetime | None = None, urgent: bool = False) -> str:
    """Send one text the farmer did not just ask for; returns what became of it.

    ``urgent`` is for answers to something the farmer did a moment ago, such as
    saving a map, which go out at any hour.
    """
    body = text.gsm_safe(body)
    now = now or settings.now()
    if farmer.opted_out_at:
        log.info("not texting %s: they opted out", farmer.phone)
        return "opted out"
    if farmer.channel != "sms" or not settings.twilio_ready:
        store.log_out(conn, farmer.phone, body, status="kept")
        return "kept"
    if not urgent and quiet(now):
        morning = next_morning(now).astimezone(timezone.utc).isoformat(timespec="seconds")
        store.log_out(conn, farmer.phone, body, status="queued", send_after=morning)
        return "queued"
    message_id = store.log_out(conn, farmer.phone, body, status="sending")
    return _send(conn, settings, farmer.phone, body, message_id)


def _send(conn: sqlite3.Connection, settings: Settings, phone: str, body: str,
          message_id: int) -> str:
    try:
        sid = twilio.send(settings, phone, body)
    except twilio.TwilioError as exc:
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
        if not settings.twilio_ready:
            store.mark_message(conn, row["id"], status="kept")
            continue
        sent += _send(conn, settings, row["phone"], row["body"], row["id"]) == "sent"
    return sent
