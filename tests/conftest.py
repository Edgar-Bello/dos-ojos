"""Shared fixtures: a throwaway data folder, a pinned clock and a pretend checkbook."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dosojos_sms import store
from dosojos_sms.bot import Bot, Inbound, Media
from dosojos_sms.config import Settings
from dosojos_sms.status import FieldWater

#: Saturday 12 September 2026, noon on the farm: the day the water example is pinned to.
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
TODAY = NOW.date()
PHONE = "+19565550123"

#: A 40-acre square near Weslaco, as the map page would send it (lon, lat).
SQUARE = [[-97.9990, 26.1470], [-97.9990, 26.1507], [-97.9949, 26.1507], [-97.9949, 26.1470]]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    loaded = Settings.load(tmp_path / "farm_data", env={})
    loaded.ensure_dirs()
    return loaded


@pytest.fixture
def conn(settings: Settings) -> sqlite3.Connection:
    connection = store.connect(settings.db_path)
    yield connection
    connection.close()


@dataclass
class FakeStatus:
    """The parts of a WaterStatus the SMS side reads."""

    days_left: int | None = 5
    days_range: list[int] | None = dc_field(default_factory=lambda: [4, 7])
    water_by: str | None = "2026-09-17"
    status: str = "water this week"
    method: str = "furrow"
    refill_net_in: float | None = 3.4
    refill_gross_in: float | None = 5.26
    crop_model: str = "sorghum"
    sensitive: str | None = None
    confidence: str = "high"
    last_irrigation: str | None = "2026-08-18"
    start: str = "2026-07-20"


class FakeWater:
    """Answers every field with the same checkbook, or a reason there is none."""

    def __init__(self, status: FakeStatus | None = None, reason: str | None = None):
        self.status = status if status is not None else FakeStatus()
        self.reason = reason
        self.calls: list[str] = []

    def field(self, record, events, as_of: date) -> FieldWater:
        self.calls.append(record.id)
        if record.outline is None:
            return FieldWater(record, reason="no_map")
        if self.reason:
            return FieldWater(record, reason=self.reason)
        return FieldWater(record, status=self.status, intake="slow")


def resolver_to(url: str):
    """A stand-in for following a short map link."""
    return lambda _short: [url]


@pytest.fixture
def water() -> FakeWater:
    return FakeWater()


@pytest.fixture
def bot(conn: sqlite3.Connection, settings: Settings, water: FakeWater) -> Bot:
    return Bot(conn, settings, now=NOW, resolve=resolver_to("https://example.invalid/none"),
               water=water)


class Phone:
    """A farmer's phone: send a text, get the replies."""

    def __init__(self, bot: Bot, number: str = PHONE, channel: str = "sim"):
        self.bot, self.number, self.channel = bot, number, channel

    def __call__(self, body: str = "", *, photo: str | None = None,
                 lat: float | None = None, lon: float | None = None) -> list[str]:
        media = [Media(photo)] if photo else []
        message_id = store.log_in(self.bot.conn, self.number, body, media=[photo] if photo else [])
        return self.bot.handle(Inbound(self.number, body, media, lat, lon, self.channel,
                                       message_id))

    def all(self, *bodies: str) -> list[str]:
        """Several texts in a row; the replies to the last one."""
        replies: list[str] = []
        for body in bodies:
            replies = self(body)
        return replies

    @property
    def farmer(self) -> store.Farmer:
        return store.get_farmer(self.bot.conn, self.number)

    @property
    def state(self) -> str:
        return self.farmer.state


@pytest.fixture
def phone(bot: Bot) -> Phone:
    return Phone(bot)


def onboard(phone: Phone, *, lang: str = "1") -> None:
    """Up to the first field's name question."""
    phone.all("hola", lang, "Juan Ejemplo", "si")


def register_field(phone: Phone, name: str = "Campo Norte", *, pin: str = "26.1484, -97.9940",
                   crop: str = "1", planted: str = "7/20", method: str = "1", side: str = "N",
                   last: str = "8/18 4") -> None:
    """A whole field, answered and confirmed, from its name on."""
    phone(name)
    phone("40")
    phone(pin)
    phone(crop)
    if planted:
        phone(planted)
        phone("si")
    if method:
        phone(method)
    if side:
        phone(side)
    if last:
        phone(last)
        phone("si")
        phone("no")


def draw(conn: sqlite3.Connection, field_id: str, ring=SQUARE) -> None:
    """Give a field the outline the map page would save."""
    record = store.get_field(conn, field_id)
    record.outline = {"type": "Polygon", "coordinates": [ring + [ring[0]]]}
    record.acres = 40.0
    store.save_field(conn, record)
