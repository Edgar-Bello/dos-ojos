"""What the checkbook and the drone say about a farmer's fields, as text messages.

``dosojos-sms daily`` fills the farm's satellite workspace with imagery, weather
and soil. Answering AGUA then runs the same water checkbook as
``dosojos-sat water``, in-process and for this farmer's fields only, with the
events straight from the SMS database: an irrigation texted a minute ago counts
at once, without waiting for the next daily run.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Sequence

from dosojos_sat import cache as sat_cache
from dosojos_sat import soils as sat_soils
from dosojos_sat import water as sat_water

from . import text
from .config import Settings
from .store import Event, FieldRow
from .text import say

log = logging.getLogger(__name__)

#: How the satellite side's crop models are named in fields.geojson.
CROP_TEXT = {
    "sorghum": "grain sorghum", "cotton": "cotton", "corn": "corn",
    "sugarcane": "sugarcane", "citrus": "citrus", "soybean": "soybean",
}
LOG_KINDS = ("planted", "irrigated", "rain", "harvested")


def crop_text(record: FieldRow) -> str:
    """The crop as the satellite half reads it: its own words for another crop."""
    if record.crop == "other":
        return record.crop_name or "other crop"
    if record.crop == "none":
        return "fallow"
    return CROP_TEXT.get(record.crop or "", record.crop or "unknown")


def log_events(events: Sequence[Event]) -> list[sat_water.LogEvent]:
    """The events the checkbook reads, as field log lines."""
    return [sat_water.LogEvent(e.field_id, e.day, e.kind, e.inches, e.note)
            for e in events if e.kind in LOG_KINDS and e.voided_at is None]


@dataclass
class FieldWater:
    """One field's checkbook, or why there is none yet.

    ``reason`` is ``no_map`` (no outline to look at), ``no_crop`` or
    ``no_data`` (the daily run has not fetched this field yet).
    """

    field: FieldRow
    status: sat_water.WaterStatus | None = None
    reason: str | None = None
    intake: str | None = None


class Water:
    """Runs the checkbook against the farm's satellite workspace."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def db_path(self):
        return self.settings.sat_workspace / "cache" / "dosojos.sqlite"

    def field(self, record: FieldRow, events: Sequence[Event], as_of: date) -> FieldWater:
        if record.outline is None:
            return FieldWater(record, reason="no_map")
        if record.crop in (None, "none"):
            return FieldWater(record, reason="no_crop")
        if not self.db_path.exists():
            return FieldWater(record, reason="no_data")
        with sat_cache.session(self.db_path) as conn:
            cached = sat_cache.get_soil(conn, record.id)
            if cached is None:
                return FieldWater(record, reason="no_data")
            profile = sat_soils.SoilProfile.from_dict(cached["profile"])
            weather = sat_cache.get_weather(conn, record.id, as_of - timedelta(days=400), as_of)
            ndvi = sat_cache.get_observations(conn, record.id, "NDVI",
                                              year_range=(as_of.year - 1, as_of.year))
        try:
            status, _, _ = sat_water.checkbook(
                field_id=record.id, name=record.name, crop_text=crop_text(record),
                soil=profile, soil_note={"name": profile.name, "intake": profile.intake},
                weather=weather, ndvi=ndvi, events=log_events(events), as_of=as_of,
                method=record.irrigation,
            )
        except sat_water.WaterError as exc:
            log.info("%s: no checkbook yet: %s", record.id, exc)
            return FieldWater(record, reason="no_data")
        return FieldWater(record, status=status, intake=profile.intake)


def urgency(item: FieldWater) -> tuple:
    """Fields that need water first; then the ones we know least about."""
    s = item.status
    if s is None:
        return (3, 0, item.field.id)
    if s.status == sat_water.STATUS_HARVESTED:
        return (2, 0, item.field.id)
    days = s.days_left if s.days_left is not None else sat_water.MAX_PROJECTION_DAYS + 1
    return (0 if s.days_left is not None else 1, days, item.field.id)


def message(item: FieldWater, lang: str, today: date, *,
            map_link: Callable[[FieldRow], str] | None = None) -> str:
    """One field's answer to AGUA, in a text or two."""
    record, s = item.field, item.status
    name = record.name
    if item.reason == "no_map":
        if record.lat is None or map_link is None:
            return say("status_no_data", lang, field=name)
        return say("status_no_map", lang, field=name, link=map_link(record))
    if item.reason == "no_crop":
        return say("status_no_crop", lang, field=name)
    if s is None:
        return say("status_no_data", lang, field=name)
    if s.status == sat_water.STATUS_HARVESTED:
        return say("status_harvested", lang, field=name)

    crop = text.crop_name(record.crop, lang, record.crop_name)
    rainfed = s.method == "none"
    gross = s.refill_gross_in if s.refill_gross_in is not None else s.refill_net_in
    values = {"field": name, "crop": crop, "method": text.method_name(s.method, lang),
              "gross": text.inches(round(gross or 0, 1))}
    if s.days_left == 0:
        body = say("status_now_rainfed" if rainfed else "status_now", lang, **values)
    elif s.days_left is None:
        body = say("status_plenty", lang, days=sat_water.MAX_PROJECTION_DAYS, **values)
    else:
        low, high = s.days_range or (s.days_left, s.days_left)
        body = say("status_days_rainfed" if rainfed else "status_days", lang,
                   about=text.about_days(s.days_left, lang), low=low, high=high,
                   date=text.day(date.fromisoformat(s.water_by), lang, today), **values)
    if s.sensitive and s.crop_model in text.STAGES:
        stage = text.pick(text.STAGES[s.crop_model], lang)
        key = "status_stage_soon" if "in about" in s.sensitive else "status_stage"
        body += say(key, lang, stage=stage)
    if s.confidence == "low":
        body += say("status_rough", lang)
    return body


# --------------------------------------------------------------------------- #
# The drone's ground report
# --------------------------------------------------------------------------- #


def latest_terrain(settings: Settings, field_id: str) -> dict | None:
    """The most recent flight's terrain.json for a field, if the team has run one."""
    manifest = settings.drone_workspace / "flights.json"
    if not manifest.exists():
        return None
    try:
        flights = json.loads(manifest.read_text(encoding="utf-8")).get("flights") or {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("cannot read %s: %s", manifest, exc)
        return None
    found = []
    for flight_id, entry in flights.items():
        report = settings.drone_workspace / "out" / flight_id / "terrain.json"
        if entry.get("field_id") == field_id and report.exists():
            found.append((entry.get("flown_on") or "", report))
    if not found:
        return None
    return json.loads(max(found)[1].read_text(encoding="utf-8"))


def ground_lines(report: dict | None, lang: str, *, intake: str | None = None,
                 limit: int = 2) -> list[str]:
    """The ground report's must-do items, in a line each."""
    if not report:
        return []
    lines: list[str] = []
    for item in report.get("advice") or []:
        line = _ground_line(item, report, lang, intake)
        if line and line not in lines:
            lines.append(line)
    return lines[:limit]


def _ground_line(item: dict, report: dict, lang: str, intake: str | None) -> str | None:
    topic, finding, priority = item.get("topic"), item.get("finding", ""), item.get("priority")
    where_match = re.search(r"in the (.+?):", finding)
    where = text.pick(text.PLACES.get(where_match.group(1), (where_match.group(1),) * 2), lang) \
        if where_match else text.pick(text.PLACES["middle"], lang)
    if topic == "high spot" and priority == 1:
        return say("ground_high_spot", lang, where=where)
    if topic == "low spot" and priority == 1:
        return say("ground_low_spot", lang, where=where)
    if topic in ("row ends", "far end", "near end") and priority == 1:
        if "first third" in finding:
            return say("ground_head", lang)
        soak = {"slow": "ground_soak_slow", "fast": "ground_soak_fast"}.get(intake or "")
        body = say("ground_tail" if topic == "row ends" else "ground_far", lang)
        return body + (say(soak, lang) if soak else "")
    if topic == "leveling" and priority == 1:
        return say("ground_uneven", lang, yd=f"{report.get('cut_yd3_per_acre', 0):.0f}")
    if topic == "grade" and priority == 1:
        return say("ground_basin" if "basin" in item.get("advice", "") else "ground_steep", lang)
    if topic == "high ground" and priority == 1:
        return say("ground_high", lang)
    if topic == "low ground" and priority == 1:
        return say("ground_low", lang)
    if topic == "cause":
        return say("ground_not_it", lang)
    return None
