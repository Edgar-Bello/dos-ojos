"""The record of every conversation: farmers, fields, events, messages and links.

Nothing a farmer says is thrown away. Each event keeps the message it came from
and any photo behind it (a district water ticket), so every line of
field_log.csv can be traced to the text that said it. A correction voids an
event instead of deleting it.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS farmers (
    phone        TEXT PRIMARY KEY,             -- E.164, e.g. +19565550123
    name         TEXT,
    lang         TEXT,                         -- es | en
    channel      TEXT NOT NULL DEFAULT 'sms',  -- sms | sim | console
    state        TEXT NOT NULL DEFAULT 'new',  -- the question waiting for an answer
    context      TEXT NOT NULL DEFAULT '{}',
    alerts       INTEGER,                      -- 1 wants alerts, 0 does not, NULL not asked
    plan         TEXT NOT NULL DEFAULT 'satellite',  -- satellite | drone | thermal
    consent_at   TEXT,
    opted_out_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fields (
    id           TEXT PRIMARY KEY,             -- F001, F002, ...
    phone        TEXT NOT NULL REFERENCES farmers(phone),
    name         TEXT NOT NULL,
    acres_said   REAL,                         -- what the farmer says it is
    lat          REAL,                         -- the pin they sent
    lon          REAL,
    place        TEXT,                         -- or the place in words, for the team to find
    outline      TEXT,                         -- GeoJSON geometry, WGS84
    acres        REAL,                         -- measured from the outline
    outline_by   TEXT,                         -- farmer | team
    outline_at   TEXT,
    crop         TEXT,                         -- sorghum | cotton | ... | other | none
    crop_name    TEXT,                         -- the farmer's word for another crop
    irrigation   TEXT,
    water_enters TEXT,                         -- N | S | E | W
    answers      TEXT NOT NULL DEFAULT '{}',   -- questions answered "don't know" or "none"
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL,
    direction   TEXT NOT NULL,                 -- in | out
    body        TEXT NOT NULL,
    media       TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL,                 -- received | sent | queued | failed | kept
    provider_id TEXT,
    error       TEXT,
    unread      INTEGER NOT NULL DEFAULT 0,    -- 1: the bot did not understand; the team reads it
    send_after  TEXT,
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_provider
    ON messages(provider_id) WHERE provider_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_phone ON messages(phone, id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id   TEXT NOT NULL REFERENCES fields(id),
    day        TEXT NOT NULL,
    kind       TEXT NOT NULL,                  -- planted | irrigated | rain | harvested | photo | scouting
    inches     REAL,
    note       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL,                  -- sms | ticket | team
    message_id INTEGER REFERENCES messages(id),
    evidence   TEXT,                           -- a photo behind it, e.g. the district ticket
    created_at TEXT NOT NULL,
    voided_at  TEXT
);
CREATE INDEX IF NOT EXISTS events_field ON events(field_id, day);

CREATE TABLE IF NOT EXISTS links (
    token      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,                  -- map | upload | explain
    field_id   TEXT NOT NULL REFERENCES fields(id),
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS uploads (
    token     TEXT PRIMARY KEY REFERENCES links(token),
    flight_id TEXT NOT NULL,
    folder    TEXT NOT NULL,
    flown_on  TEXT,
    bare_soil INTEGER,
    files     INTEGER NOT NULL DEFAULT 0,
    bytes     INTEGER NOT NULL DEFAULT 0,
    done_at   TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    field_id TEXT NOT NULL,
    kind     TEXT NOT NULL,                    -- water | checkin | missing
    key      TEXT NOT NULL,                    -- what it was about, so it is said once
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (field_id, kind, key)
);
"""

#: ``scouting`` is a pest count; its ``note`` holds the count as JSON.
EVENT_KINDS = ("planted", "irrigated", "rain", "harvested", "photo", "scouting")

#: Columns added after the first databases were made. SQLite cannot bring an
#: existing table forward through CREATE TABLE IF NOT EXISTS, so each one is
#: added on open if it is not there yet. A farm's database is the record of
#: everything the farmers said; it is migrated, never rebuilt.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "farmers": {"plan": "TEXT NOT NULL DEFAULT 'satellite'"},
}


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the database, creating its folder and schema if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(_SCHEMA)
    migrate(conn)
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any column a newer version needs; returns the ones added."""
    added = []
    for table, columns in ADDED_COLUMNS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                added.append(f"{table}.{name}")
    return added


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A connection that commits on success and always closes."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso() -> str:
    return utc_iso()


def utc_iso(now: datetime | None = None) -> str:
    """``now`` in UTC as stored, or the real time. Links pass the app's clock, so a
    demo pinned to a past day keeps the links in its texts working."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="seconds")


def normalize_phone(raw: str) -> str:
    """E.164: '(956) 555-0123' and 'whatsapp:+19565550123' both become '+19565550123'."""
    text = raw.strip()
    if text.lower().startswith("whatsapp:"):
        text = text[len("whatsapp:"):]
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise ValueError(f"not a phone number: {raw!r}")
    if text.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


# --------------------------------------------------------------------------- #
# Farmers
# --------------------------------------------------------------------------- #


@dataclass
class Farmer:
    phone: str
    name: str | None = None
    lang: str | None = None
    channel: str = "sms"
    state: str = "new"
    context: dict = dc_field(default_factory=dict)
    alerts: bool | None = None
    #: What this farmer signed up for: satellite only, plus their own drone
    #: photos, or plus a thermal camera. See text.PLANS.
    plan: str = "satellite"
    consent_at: str | None = None
    opted_out_at: str | None = None
    created_at: str = ""

    @property
    def language(self) -> str:
        return self.lang or "es"


def _farmer(row: sqlite3.Row) -> Farmer:
    return Farmer(
        phone=row["phone"], name=row["name"], lang=row["lang"], channel=row["channel"],
        state=row["state"], context=json.loads(row["context"] or "{}"),
        alerts=None if row["alerts"] is None else bool(row["alerts"]),
        plan=row["plan"] or "satellite",
        consent_at=row["consent_at"], opted_out_at=row["opted_out_at"],
        created_at=row["created_at"],
    )


def get_farmer(conn: sqlite3.Connection, phone: str) -> Farmer | None:
    row = conn.execute("SELECT * FROM farmers WHERE phone = ?", (phone,)).fetchone()
    return _farmer(row) if row else None


def add_farmer(conn: sqlite3.Connection, phone: str, *, channel: str = "sms") -> Farmer:
    stamp = now_iso()
    conn.execute("INSERT INTO farmers(phone, channel, created_at, updated_at) VALUES (?,?,?,?)",
                 (phone, channel, stamp, stamp))
    return Farmer(phone=phone, channel=channel, created_at=stamp)


def save_farmer(conn: sqlite3.Connection, farmer: Farmer) -> None:
    conn.execute(
        """UPDATE farmers SET name = ?, lang = ?, channel = ?, state = ?, context = ?,
               alerts = ?, plan = ?, consent_at = ?, opted_out_at = ?, updated_at = ?
           WHERE phone = ?""",
        (farmer.name, farmer.lang, farmer.channel, farmer.state, json.dumps(farmer.context),
         None if farmer.alerts is None else int(farmer.alerts), farmer.plan, farmer.consent_at,
         farmer.opted_out_at, now_iso(), farmer.phone),
    )


def farmers(conn: sqlite3.Connection) -> list[Farmer]:
    return [_farmer(r) for r in conn.execute("SELECT * FROM farmers ORDER BY created_at")]


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #


@dataclass
class FieldRow:
    id: str
    phone: str
    name: str
    acres_said: float | None = None
    lat: float | None = None
    lon: float | None = None
    place: str | None = None
    outline: dict | None = None
    acres: float | None = None
    outline_by: str | None = None
    outline_at: str | None = None
    crop: str | None = None
    crop_name: str | None = None
    irrigation: str | None = None
    water_enters: str | None = None
    answers: dict = dc_field(default_factory=dict)
    created_at: str = ""


_FIELD_COLUMNS = ("phone", "name", "acres_said", "lat", "lon", "place", "outline", "acres",
                  "outline_by", "outline_at", "crop", "crop_name", "irrigation",
                  "water_enters", "answers")


def _field(row: sqlite3.Row) -> FieldRow:
    values = {k: row[k] for k in _FIELD_COLUMNS}
    values["outline"] = json.loads(row["outline"]) if row["outline"] else None
    values["answers"] = json.loads(row["answers"] or "{}")
    return FieldRow(id=row["id"], created_at=row["created_at"], **values)


def add_field(conn: sqlite3.Connection, phone: str, name: str) -> FieldRow:
    """A new field with the next free id, F001 onwards."""
    numbers = [int(r["id"][1:]) for r in conn.execute("SELECT id FROM fields")
               if re.fullmatch(r"F\d+", r["id"])]
    field_id = f"F{(max(numbers) + 1 if numbers else 1):03d}"
    stamp = now_iso()
    conn.execute("INSERT INTO fields(id, phone, name, created_at, updated_at) VALUES (?,?,?,?,?)",
                 (field_id, phone, name, stamp, stamp))
    return FieldRow(id=field_id, phone=phone, name=name, created_at=stamp)


def save_field(conn: sqlite3.Connection, record: FieldRow) -> None:
    values = [getattr(record, k) for k in _FIELD_COLUMNS]
    values[_FIELD_COLUMNS.index("outline")] = (json.dumps(record.outline)
                                               if record.outline else None)
    values[_FIELD_COLUMNS.index("answers")] = json.dumps(record.answers)
    assignments = ", ".join(f"{k} = ?" for k in _FIELD_COLUMNS)
    conn.execute(f"UPDATE fields SET {assignments}, updated_at = ? WHERE id = ?",
                 (*values, now_iso(), record.id))


def get_field(conn: sqlite3.Connection, field_id: str) -> FieldRow | None:
    row = conn.execute("SELECT * FROM fields WHERE id = ?", (field_id,)).fetchone()
    return _field(row) if row else None


def fields_of(conn: sqlite3.Connection, phone: str) -> list[FieldRow]:
    return [_field(r) for r in
            conn.execute("SELECT * FROM fields WHERE phone = ? ORDER BY id", (phone,))]


def all_fields(conn: sqlite3.Connection) -> list[FieldRow]:
    return [_field(r) for r in conn.execute("SELECT * FROM fields ORDER BY id")]


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Event:
    id: int
    field_id: str
    day: date
    kind: str
    inches: float | None
    note: str
    source: str
    message_id: int | None
    evidence: str | None
    created_at: str
    voided_at: str | None


def _event(row: sqlite3.Row) -> Event:
    return Event(row["id"], row["field_id"], date.fromisoformat(row["day"]), row["kind"],
                 row["inches"], row["note"], row["source"], row["message_id"],
                 row["evidence"], row["created_at"], row["voided_at"])


def add_event(conn: sqlite3.Connection, field_id: str, day: date, kind: str, *,
              inches: float | None = None, note: str = "", source: str = "sms",
              message_id: int | None = None, evidence: str | None = None) -> int:
    """Record one planting, irrigation, rain reading, harvest or photo.

    A second report of the same kind on the same day replaces the first: a
    farmer who says "4 inches" and later "no, 3" means 3.
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    if kind != "photo":
        conn.execute("UPDATE events SET voided_at = ? WHERE field_id = ? AND day = ? "
                     "AND kind = ? AND voided_at IS NULL",
                     (now_iso(), field_id, day.isoformat(), kind))
    cursor = conn.execute(
        """INSERT INTO events(field_id, day, kind, inches, note, source, message_id, evidence,
                              created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (field_id, day.isoformat(), kind, inches, note, source, message_id, evidence, now_iso()),
    )
    return int(cursor.lastrowid)


def events_for(conn: sqlite3.Connection, field_id: str | None = None, *,
               include_void: bool = False) -> list[Event]:
    sql, params = "SELECT * FROM events", []
    clauses = []
    if field_id:
        clauses.append("field_id = ?")
        params.append(field_id)
    if not include_void:
        clauses.append("voided_at IS NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return [_event(r) for r in conn.execute(sql + " ORDER BY field_id, day, id", params)]


def last_event_of(conn: sqlite3.Connection, phone: str) -> Event | None:
    """The farmer's most recently recorded event still standing."""
    row = conn.execute(
        """SELECT e.* FROM events e JOIN fields f ON f.id = e.field_id
           WHERE f.phone = ? AND e.voided_at IS NULL ORDER BY e.id DESC LIMIT 1""",
        (phone,),
    ).fetchone()
    return _event(row) if row else None


def void_event(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute("UPDATE events SET voided_at = ? WHERE id = ?", (now_iso(), event_id))


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


def log_in(conn: sqlite3.Connection, phone: str, body: str, *, media: list | None = None,
           provider_id: str | None = None) -> int | None:
    """Store an incoming text; None when the provider already delivered this one."""
    try:
        cursor = conn.execute(
            "INSERT INTO messages(phone, direction, body, media, status, provider_id, created_at) "
            "VALUES (?, 'in', ?, ?, 'received', ?, ?)",
            (phone, body, json.dumps(media or []), provider_id, now_iso()),
        )
    except sqlite3.IntegrityError:
        return None
    return int(cursor.lastrowid)


def log_out(conn: sqlite3.Connection, phone: str, body: str, *, status: str,
            provider_id: str | None = None, error: str | None = None,
            send_after: str | None = None, media: list[str] | None = None) -> int:
    """An outgoing text; ``media`` are links to the pictures it carries."""
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO messages(phone, direction, body, media, status, provider_id, error, "
        "send_after, created_at, sent_at) VALUES (?, 'out', ?, ?, ?, ?, ?, ?, ?, ?)",
        (phone, body, json.dumps(media or []), status, provider_id, error, send_after, stamp,
         stamp if status in ("sent", "kept") else None),
    )
    return int(cursor.lastrowid)


def mark_message(conn: sqlite3.Connection, message_id: int, *, status: str,
                 provider_id: str | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE messages SET status = ?, provider_id = COALESCE(?, provider_id), error = ?, "
        "sent_at = CASE WHEN ? IN ('sent', 'kept') THEN ? ELSE sent_at END WHERE id = ?",
        (status, provider_id, error, status, now_iso(), message_id),
    )


def mark_unread(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute("UPDATE messages SET unread = 1 WHERE id = ?", (message_id,))


def mark_read(conn: sqlite3.Connection, message_ids: list[int]) -> None:
    conn.executemany("UPDATE messages SET unread = 0 WHERE id = ?", [(i,) for i in message_ids])


def messages(conn: sqlite3.Connection, phone: str | None = None, *, after: int = 0,
             unread_only: bool = False, limit: int = 500) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM messages WHERE id > ?", [after]
    if phone:
        sql += " AND phone = ?"
        params.append(phone)
    if unread_only:
        sql += " AND unread = 1"
    return conn.execute(sql + " ORDER BY id LIMIT ?", (*params, limit)).fetchall()


def queued(conn: sqlite3.Connection, now_utc: str) -> list[sqlite3.Row]:
    """Outgoing texts held for the morning, now due."""
    return conn.execute(
        "SELECT * FROM messages WHERE direction = 'out' AND status = 'queued' "
        "AND (send_after IS NULL OR send_after <= ?) ORDER BY id", (now_utc,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Links: map drawing and photo upload pages
# --------------------------------------------------------------------------- #


def new_link(conn: sqlite3.Connection, kind: str, field_id: str, *, days: int,
             meta: dict | None = None, now: datetime | None = None) -> str:
    """An unguessable token for one field's map or upload page."""
    token = secrets.token_urlsafe(12)
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conn.execute(
        "INSERT INTO links(token, kind, field_id, meta, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)",
        (token, kind, field_id, json.dumps(meta or {}),
         created.isoformat(timespec="seconds"),
         (created + timedelta(days=days)).isoformat(timespec="seconds")),
    )
    return token


def get_link(conn: sqlite3.Connection, token: str, kind: str, *,
             now: datetime | None = None) -> sqlite3.Row | None:
    """The link if it exists, is of this kind and has not expired by ``now``."""
    row = conn.execute("SELECT * FROM links WHERE token = ? AND kind = ?",
                       (token, kind)).fetchone()
    if row is None or row["expires_at"] < utc_iso(now):
        return None
    return row


def use_link(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("UPDATE links SET used_at = ? WHERE token = ?", (now_iso(), token))


def new_upload(conn: sqlite3.Connection, token: str, *, flight_id: str, folder: str,
               flown_on: date | None, bare_soil: bool | None) -> None:
    conn.execute(
        "INSERT INTO uploads(token, flight_id, folder, flown_on, bare_soil) VALUES (?,?,?,?,?)",
        (token, flight_id, folder, flown_on.isoformat() if flown_on else None,
         None if bare_soil is None else int(bare_soil)),
    )


def get_upload(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM uploads WHERE token = ?", (token,)).fetchone()


def count_upload(conn: sqlite3.Connection, token: str, n_bytes: int) -> None:
    conn.execute("UPDATE uploads SET files = files + 1, bytes = bytes + ? WHERE token = ?",
                 (n_bytes, token))


def finish_upload(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("UPDATE uploads SET done_at = ? WHERE token = ?", (now_iso(), token))


def uploads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT u.*, l.field_id FROM uploads u JOIN links l ON l.token = u.token "
        "ORDER BY l.created_at").fetchall()


# --------------------------------------------------------------------------- #
# Alerts already sent
# --------------------------------------------------------------------------- #


def alert_sent(conn: sqlite3.Connection, field_id: str, kind: str, key: str) -> bool:
    return conn.execute("SELECT 1 FROM alerts WHERE field_id = ? AND kind = ? AND key = ?",
                        (field_id, kind, key)).fetchone() is not None


def last_alert(conn: sqlite3.Connection, field_id: str, kind: str) -> datetime | None:
    row = conn.execute("SELECT MAX(sent_at) AS last FROM alerts WHERE field_id = ? AND kind = ?",
                       (field_id, kind)).fetchone()
    return datetime.fromisoformat(row["last"]) if row and row["last"] else None


def record_alert(conn: sqlite3.Connection, field_id: str, kind: str, key: str) -> None:
    conn.execute("INSERT OR REPLACE INTO alerts(field_id, kind, key, sent_at) VALUES (?,?,?,?)",
                 (field_id, kind, key, now_iso()))
