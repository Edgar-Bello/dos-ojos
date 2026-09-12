"""The water checkbook: how much water is left in each field, and how long it lasts.

A field's root zone is treated like a bank account (FAO Irrigation and Drainage
Paper 56, chapter 8). The balance is the soil's available water, from the soil
survey. Every day the crop withdraws what the weather demands (reference ET)
times a crop coefficient, and rain and irrigation make deposits. Once the
account runs down past a crop-specific share, the plant starts closing its pores
to save water and growth suffers: that point is when to irrigate.

Two things come from our own eyes rather than from tables. The crop coefficient
follows the satellite's NDVI, so a thin stand or a late crop draws less water
than a textbook calendar would assume, and the root zone deepens as the canopy
fills in. The farm supplies the rest in a small log: planting date, and the
date (and, if known, inches) of each irrigation.

Everything here is a daily loop over plain arrays. The projection holds the last
week's weather and the current crop coefficient, and assumes no rain, which is
the honest question a grower asks: if it stays like this, when do I water?
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .soils import SoilProfile

log = logging.getLogger(__name__)

MM_PER_INCH = 25.4

#: NDVI of bare soil and of a full canopy, between which the crop coefficient
#: and root depth are scaled. Sentinel-2 L2A over RGV soils and full crops.
NDVI_BARE = 0.15
NDVI_FULL = 0.85

#: Rain under this share of the day's ETo wets the leaves and evaporates.
LIGHT_RAIN_SHARE = 0.2

#: Days of weather averaged for the projection.
PROJECTION_WEATHER_DAYS = 7
#: Spread of the projection: a hotter and a milder week than the last one.
PROJECTION_SPREAD = (1.2, 0.8)
MAX_PROJECTION_DAYS = 45

#: Past this many days without a satellite image, the crop coefficient is stale.
STALE_IMAGE_DAYS = 20
#: With no planting or irrigation on record, the balance starts this long ago
#: from an assumed full profile.
ASSUMED_START_DAYS = 60

#: Share of delivered water that ends up in the root zone, by method. Typical
#: application efficiencies; a furrow run loses water to deep percolation at the
#: head and runoff at the tail.
EFFICIENCY: dict[str, float] = {
    "furrow": 0.65, "flood": 0.70, "border": 0.70, "basin": 0.80,
    "sprinkler": 0.75, "pivot": 0.85, "drip": 0.90,
}
DEFAULT_METHOD = "furrow"

STATUS_NOW = "water now"
STATUS_SOON = "water within 3 days"
STATUS_WEEK = "water this week"
STATUS_OK = "ok for now"
STATUS_HARVESTED = "harvested"


class WaterError(RuntimeError):
    """Raised when a field's balance cannot be run from what is cached."""


# --------------------------------------------------------------------------- #
# Crops
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Crop:
    """FAO-56 coefficients for one crop (Tables 12 and 22).

    ``p`` is the share of available water that can go before the crop feels it.
    ``sensitive_dap`` is the window, in days after planting, when running short
    costs the most yield; perennials give ``sensitive_months`` instead.
    """

    key: str
    label: str
    kc_ini: float
    kc_mid: float
    kc_end: float
    p: float
    root_min_m: float
    root_max_m: float
    perennial: bool = False
    sensitive_dap: tuple[int, int] | None = None
    sensitive_months: tuple[int, ...] = ()
    sensitive_stage: str = ""


CROPS: dict[str, Crop] = {
    "sorghum": Crop("sorghum", "grain sorghum", 0.30, 1.05, 0.55, 0.55, 0.3, 1.2,
                    sensitive_dap=(50, 80), sensitive_stage="boot to flowering"),
    # Ratoon cane keeps its roots between cuts, so the root zone starts deep.
    "sugarcane": Crop("sugarcane", "sugarcane", 0.40, 1.25, 0.75, 0.65, 0.8, 1.5,
                      sensitive_months=(5, 6, 7, 8), sensitive_stage="grand growth"),
    "cotton": Crop("cotton", "cotton", 0.35, 1.18, 0.60, 0.65, 0.3, 1.2,
                   sensitive_dap=(60, 95), sensitive_stage="first bloom to peak bloom"),
    "corn": Crop("corn", "corn", 0.30, 1.20, 0.60, 0.55, 0.3, 1.2,
                 sensitive_dap=(55, 80), sensitive_stage="tasseling and silking"),
    "soybean": Crop("soybean", "soybean", 0.40, 1.15, 0.50, 0.50, 0.3, 1.0,
                    sensitive_dap=(60, 100), sensitive_stage="pod set and fill"),
    # Mature citrus, 70% canopy, bare middles: a near-constant coefficient.
    "citrus": Crop("citrus", "citrus", 0.65, 0.65, 0.65, 0.50, 1.0, 1.0, perennial=True,
                   sensitive_months=(2, 3, 4, 5), sensitive_stage="bloom and fruit set"),
    "generic": Crop("generic", "unrecognised crop", 0.35, 1.10, 0.60, 0.50, 0.3, 1.0),
}

_CROP_WORDS: tuple[tuple[str, str], ...] = (
    ("sorghum", "sorghum"), ("milo", "sorghum"), ("cane", "sugarcane"),
    ("cotton", "cotton"), ("corn", "corn"), ("maize", "corn"), ("soy", "soybean"),
    ("citrus", "citrus"), ("orange", "citrus"), ("grapefruit", "citrus"),
    ("lemon", "citrus"), ("lime", "citrus"),
)


def crop_for(text: str | None) -> Crop:
    """The crop a field's free-text ``crop`` names, or the generic one."""
    lowered = (text or "").lower()
    for word, key in _CROP_WORDS:
        if word in lowered:
            return CROPS[key]
    return CROPS["generic"]


def canopy_fraction(ndvi: np.ndarray) -> np.ndarray:
    """Share of the ground covered, scaled linearly between bare soil and full canopy."""
    return np.clip((np.asarray(ndvi, dtype=float) - NDVI_BARE) / (NDVI_FULL - NDVI_BARE), 0, 1)


def crop_coefficient(fc: np.ndarray, crop: Crop) -> np.ndarray:
    """FAO single crop coefficient from canopy cover.

    Anchored on the crop's table values: bare ground uses ``kc_ini``, a full
    canopy ``kc_mid``, and a senescing canopy falls back between them as its
    NDVI drops. Perennials hold ``kc_mid``.
    """
    fc = np.asarray(fc, dtype=float)
    if crop.perennial:
        return np.full_like(fc, crop.kc_mid)
    return crop.kc_ini + (crop.kc_mid - crop.kc_ini) * fc


def root_depth(fc: np.ndarray, crop: Crop) -> np.ndarray:
    """Effective root depth in metres, deepening as the canopy fills and never shrinking."""
    fc = np.asarray(fc, dtype=float)
    if crop.perennial:
        return np.full_like(fc, crop.root_max_m)
    grown = np.maximum.accumulate(np.nan_to_num(fc, nan=0.0))
    return crop.root_min_m + (crop.root_max_m - crop.root_min_m) * grown


def depletion_fraction(crop: Crop, etc_mm: float) -> float:
    """FAO-56 ``p`` adjusted for the day's demand: less slack on a hot day."""
    return float(np.clip(crop.p + 0.04 * (5.0 - etc_mm), 0.1, 0.8))


# --------------------------------------------------------------------------- #
# Field log
# --------------------------------------------------------------------------- #

EVENTS = ("planted", "irrigated", "rain", "harvested")
_EVENT_ALIASES = {
    "plant": "planted", "planting": "planted", "sown": "planted", "seeded": "planted",
    "irrigation": "irrigated", "irrigate": "irrigated", "watered": "irrigated",
    "water": "irrigated", "riego": "irrigated",
    "harvest": "harvested", "cut": "harvested",
    "rainfall": "rain", "lluvia": "rain",
}


class FieldLogError(ValueError):
    """Raised when the field log cannot be read, naming the offending line."""


@dataclass(frozen=True)
class LogEvent:
    """One line of the field log."""

    field_id: str
    day: date
    event: str
    inches: float | None = None
    notes: str = ""


def _parse_day(text: str, where: str) -> date:
    """ISO dates, or the month/day/year a US spreadsheet writes."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise FieldLogError(f"{where}: date {text!r} is not YYYY-MM-DD or M/D/YYYY")


def read_field_log(path: Path) -> list[LogEvent]:
    """Read ``field_id,date,event,inches,notes`` rows; missing file means no events.

    ``inches`` is what was delivered for an irrigation, or a rain gauge reading.
    An irrigation with no inches is taken to have filled the root zone.

    Raises:
        FieldLogError: on an unknown event, a bad date or a negative amount.
    """
    path = Path(path)
    if not path.exists():
        return []
    events: list[LogEvent] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"field_id", "date", "event"}
        headers = {h.strip().lower() for h in (reader.fieldnames or [])}
        if not required <= headers:
            raise FieldLogError(
                f"{path.name} needs columns field_id, date, event (and optionally "
                f"inches, notes); found {sorted(headers)}"
            )
        for line, raw in enumerate(reader, start=2):
            row = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            if not any(row.values()):
                continue
            where = f"{path.name} line {line}"
            event = row["event"].lower()
            event = _EVENT_ALIASES.get(event, event)
            if event not in EVENTS:
                raise FieldLogError(f"{where}: event {row['event']!r} is not one of "
                                    f"{', '.join(EVENTS)}")
            inches = None
            if row.get("inches"):
                try:
                    inches = float(row["inches"])
                except ValueError as exc:
                    raise FieldLogError(f"{where}: inches {row['inches']!r} is not a number") from exc
                if inches < 0:
                    raise FieldLogError(f"{where}: inches cannot be negative")
            if event == "rain" and inches is None:
                raise FieldLogError(f"{where}: a rain entry needs the gauge reading in inches")
            if not row["field_id"]:
                raise FieldLogError(f"{where}: field_id is empty")
            events.append(LogEvent(row["field_id"], _parse_day(row["date"], where), event,
                                   inches, row.get("notes", "")))
    return sorted(events, key=lambda e: (e.field_id, e.day))


# --------------------------------------------------------------------------- #
# The balance
# --------------------------------------------------------------------------- #


@dataclass
class WaterStatus:
    """One field's checkbook as of a date. Depths are in inches, rates per day."""

    field_id: str
    name: str
    crop: str
    crop_model: str
    as_of: str
    status: str
    days_left: int | None
    days_range: list[int] | None
    water_by: str | None
    water_left_in: float | None
    until_stress_in: float | None
    capacity_in: float | None
    stress_point_in: float | None
    pct_left: float | None
    root_depth_in: float | None
    kc: float | None
    eto_in_day: float | None
    use_in_day: float | None
    refill_net_in: float | None
    refill_gross_in: float | None
    method: str
    efficiency: float | None
    start: str
    start_reason: str
    last_irrigation: str | None
    last_rain: str | None
    stressed_days_30: int
    weather_through: str | None
    last_image: str | None
    soil: dict
    sensitive: str | None
    confidence: str
    notes: list[str] = field(default_factory=list)
    rank: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _inches(mm: float | None) -> float | None:
    return None if mm is None or not math.isfinite(mm) else round(mm / MM_PER_INCH, 2)


#: Log entries older than this say nothing about this season's root zone.
LOG_LOOKBACK_DAYS = 365


def season_start(events: Sequence[LogEvent], as_of: date) -> tuple[date, str, str]:
    """Where the balance starts, why, and how that start is known.

    A planting in the past year (the profile is taken as full, from
    pre-irrigation or winter rain) beats the first irrigation of the past year,
    which beats an assumption. The last value is ``planted``, ``irrigated`` or
    ``assumed``.
    """
    past = [e for e in events if 0 <= (as_of - e.day).days <= LOG_LOOKBACK_DAYS]
    planted = [e for e in past if e.event == "planted"]
    if planted:
        return planted[-1].day, f"planted {planted[-1].day.isoformat()}", "planted"
    irrigated = [e for e in past if e.event == "irrigated"]
    if irrigated:
        first = irrigated[0].day
        return first, f"first irrigation on record, {first.isoformat()}", "irrigated"
    start = as_of - timedelta(days=ASSUMED_START_DAYS)
    return start, (f"no planting or irrigation on record: assumed the soil was full on "
                   f"{start.isoformat()}"), "assumed"


def daily_ndvi(observations: pd.DataFrame, days: Sequence[date]) -> np.ndarray:
    """NDVI for every day, interpolated between images and held beyond the last one."""
    obs = observations.dropna(subset=["median"]).sort_values("date")
    ordinal = np.array([d.toordinal() for d in days], dtype=float)
    return np.interp(ordinal, [d.toordinal() for d in obs["date"]],
                     obs["median"].to_numpy(dtype=float))


def run_balance(
    *,
    days: Sequence[date],
    eto_mm: np.ndarray,
    rain_mm: np.ndarray,
    ndvi: np.ndarray,
    crop: Crop,
    soil: SoilProfile,
    irrigations: dict[date, float | None],
    efficiency: float,
    start_depletion_mm: float = 0.0,
) -> pd.DataFrame:
    """Run the daily root-zone balance; one row per day, depths in millimetres.

    ``irrigations`` maps a day to the inches delivered, or None for a full
    irrigation that refills the root zone. ``dr`` is the depletion at the end of
    the day: how far below full the root zone is.
    """
    fc = canopy_fraction(ndvi)
    kc = crop_coefficient(fc, crop)
    roots = root_depth(fc, crop)
    dr = float(start_depletion_mm)
    rows = []
    demand: list[float] = []
    for i, day in enumerate(days):
        eto = float(eto_mm[i])
        taw = soil.taw_mm(float(roots[i]))
        dr = min(dr, taw)
        etc_potential = kc[i] * eto
        # The stress point moves with demand, but with the week's demand rather
        # than one day's: a single cool day does not give a crop more slack.
        demand = (demand + [etc_potential])[-PROJECTION_WEATHER_DAYS:]
        p = depletion_fraction(crop, float(np.mean(demand)))
        raw = p * taw
        ks = 1.0 if dr <= raw else max(0.0, (taw - dr) / ((1.0 - p) * taw))
        etc = ks * etc_potential

        rain = float(rain_mm[i]) if math.isfinite(rain_mm[i]) else 0.0
        effective = rain if rain >= LIGHT_RAIN_SHARE * eto else 0.0
        irrigation = 0.0
        if day in irrigations:
            delivered = irrigations[day]
            irrigation = dr if delivered is None else delivered * MM_PER_INCH * efficiency

        dr = dr - effective - irrigation + etc
        drained = max(0.0, -dr)          # beyond full: runoff or deep percolation
        dr = min(max(dr, 0.0), taw)
        rows.append({
            "date": day, "eto_mm": eto, "rain_mm": rain, "rain_effective_mm": effective,
            "irrigation_mm": irrigation, "ndvi": float(ndvi[i]), "fc": float(fc[i]),
            "kc": float(kc[i]), "root_m": float(roots[i]), "taw_mm": taw, "raw_mm": raw,
            "ks": ks, "etc_mm": etc, "dr_mm": dr, "drained_mm": drained,
        })
    return pd.DataFrame(rows)


def project(
    *, depletion_mm: float, eto_mm: float, kc: float, root_m: float, crop: Crop,
    soil: SoilProfile, max_days: int = MAX_PROJECTION_DAYS,
) -> tuple[int | None, list[float]]:
    """Days until the root zone reaches the stress point, with no rain.

    Returns ``(days, depletion path)``; days is 0 when it is already there and
    None when it would not get there within ``max_days``.
    """
    taw = soil.taw_mm(root_m)
    etc = kc * eto_mm
    raw = depletion_fraction(crop, etc) * taw
    dr = depletion_mm
    path = [dr]
    if dr >= raw:
        return 0, path
    for day in range(1, max_days + 1):
        dr = min(taw, dr + etc)
        path.append(dr)
        if dr >= raw:
            return day, path
    return None, path


def _status_for(days_left: int | None) -> str:
    if days_left is None:
        return STATUS_OK
    if days_left <= 0:
        return STATUS_NOW
    if days_left <= 3:
        return STATUS_SOON
    if days_left <= 7:
        return STATUS_WEEK
    return STATUS_OK


def _sensitive_note(crop: Crop, planted: date | None, as_of: date,
                    days_left: int | None) -> str | None:
    """Say so when the crop is at, or about to reach, the stage it can least afford."""
    horizon = (days_left or 0) + 7
    if crop.sensitive_dap and planted:
        dap = (as_of - planted).days
        low, high = crop.sensitive_dap
        if low - horizon <= dap <= high:
            when = "now" if dap >= low else f"in about {low - dap} days"
            return (f"{crop.label} reaches {crop.sensitive_stage} {when} (day {dap} after "
                    "planting): the stage that can least afford to run dry")
    if crop.sensitive_months and as_of.month in crop.sensitive_months:
        return f"{crop.label} is in {crop.sensitive_stage}: keep it from running dry"
    return None


def checkbook(
    *,
    field_id: str,
    name: str,
    crop_text: str,
    soil: SoilProfile,
    soil_note: dict,
    weather: pd.DataFrame,
    ndvi: pd.DataFrame,
    events: Sequence[LogEvent],
    as_of: date,
    method: str | None,
) -> tuple[WaterStatus, pd.DataFrame, pd.DataFrame]:
    """Run one field's checkbook up to ``as_of`` and project it forward.

    Args:
        weather: daily ``date``, ``eto_mm``, ``rain_mm`` (and optionally ``source``).
        ndvi: this season's satellite observations with ``date`` and ``median``.
        events: the field log for this field.
        method: irrigation method from the field settings; ``none`` for rainfed.

    Returns:
        ``(status, daily, projection)``: the verdict, one row per day of the
        balance, and the projected path from ``as_of``.

    Raises:
        WaterError: when the weather or the satellite does not cover the season.
    """
    crop = crop_for(crop_text)
    events = [e for e in events if e.day <= as_of]
    notes: list[str] = []
    low_confidence: list[str] = []
    medium_confidence: list[str] = []

    start, start_reason, start_kind = season_start(events, as_of)
    assumed = start_kind == "assumed"
    if assumed:
        low_confidence.append("start assumed")
        notes.append(start_reason + "; add the planting or last irrigation date to "
                                    "field_log.csv for a real answer")
    harvested = [e for e in events if e.event == "harvested" and e.day >= start]
    end = min(as_of, harvested[-1].day) if harvested else as_of

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    daily_weather, filled, through = _weather_for(weather, days, field_id, start)
    if filled:
        medium_confidence.append("weather filled")
        notes.append(f"weather published through {through.isoformat()}; the {filled} "
                     "day(s) after it are taken to be like the week before")

    season_obs = ndvi[(ndvi["date"] >= start - timedelta(days=45)) & (ndvi["date"] <= as_of)]
    season_obs = season_obs.dropna(subset=["median"])[["date", "median"]]
    sown = start_kind == "planted" and not crop.perennial
    if sown:
        # A crop just planted covers nothing, whatever grew there before: anchor
        # bare soil on the planting date so weeds or the last crop cannot inflate
        # its early water use.
        season_obs = pd.concat([
            pd.DataFrame({"date": [start], "median": [NDVI_BARE]}),
            season_obs[season_obs["date"] > start],
        ], ignore_index=True)
    if season_obs.empty:
        raise WaterError(
            f"no satellite images of {field_id} between {start - timedelta(days=45)} and "
            f"{as_of}, so the crop's water use is unknown. Run 'dosojos-sat fetch' first."
        )
    images = season_obs.iloc[1:] if sown else season_obs
    if images.empty:
        notes.append("no clear satellite image since planting yet; the crop is taken "
                     "to be still small")
    last_image = max(images["date"]) if not images.empty else start
    # Judged up to a harvest, if there was one: a cut field is bare for a reason.
    bare = _bare_ground_note(images[images["date"] <= end], crop, start, end, sown)
    if bare:
        low_confidence.append("bare ground")
        notes.append(bare)
    image_age = (as_of - last_image).days
    if image_age > STALE_IMAGE_DAYS:
        (low_confidence if image_age > 2 * STALE_IMAGE_DAYS else medium_confidence).append(
            "stale image")
        notes.append(f"last clear satellite image {image_age} days before {as_of}; the "
                     "crop's size since then is assumed unchanged")

    method = method or DEFAULT_METHOD
    efficiency = EFFICIENCY.get(method)
    if method == "none":
        notes.append("rainfed: no irrigation expected, so days left are days until rain is needed")
    elif method not in EFFICIENCY:
        efficiency = EFFICIENCY[DEFAULT_METHOD]
    if efficiency is None:
        efficiency = 1.0
    if crop.key == "generic":
        low_confidence.append("generic crop")
        notes.append(f"crop {crop_text!r} not recognised; generic coefficients used")

    rain_gauge = {e.day: e.inches * MM_PER_INCH for e in events
                  if e.event == "rain" and e.inches is not None}
    rain = np.array([rain_gauge.get(d, r) for d, r in
                     zip(days, daily_weather["rain_mm"].to_numpy(dtype=float))])
    irrigations = {e.day: e.inches for e in events if e.event == "irrigated" and e.day >= start}
    if irrigations and method == "none":
        notes.append("irrigations logged for a field marked rainfed; they are counted")
    if method not in ("none",) and not irrigations and not assumed:
        medium_confidence.append("no irrigations")
        notes.append("no irrigation logged since the start; if the field was watered, "
                     "add the dates to field_log.csv")

    ndvi_daily = daily_ndvi(season_obs, days)
    daily = run_balance(
        days=days, eto_mm=daily_weather["eto_mm"].to_numpy(dtype=float), rain_mm=rain,
        ndvi=ndvi_daily, crop=crop, soil=soil, irrigations=irrigations,
        efficiency=efficiency,
    )
    daily["filled_weather"] = daily_weather["filled"].to_numpy()
    last = daily.iloc[-1]
    recent = daily.tail(30)

    projection = pd.DataFrame(columns=["date", "dr_mm"])
    planted = start if start_kind == "planted" else None
    last_irr = max(irrigations) if irrigations else None
    rains = daily[daily["rain_effective_mm"] > 0]
    last_rain = (f"{rains.iloc[-1]['date'].isoformat()} "
                 f"({rains.iloc[-1]['rain_mm'] / MM_PER_INCH:.2f} in)") if not rains.empty else None

    if harvested:
        status_word, days_left, days_range, water_by = STATUS_HARVESTED, None, None, None
        until, refill = None, None
        notes.insert(0, f"harvested {harvested[-1].day.isoformat()}: no water needed until "
                        "the next crop")
        sensitive = None
    else:
        actual = daily[~daily["filled_weather"]].tail(PROJECTION_WEATHER_DAYS)
        eto_next = float((actual if not actual.empty else daily.tail(7))["eto_mm"].mean())
        args = {"depletion_mm": float(last["dr_mm"]), "kc": float(last["kc"]),
                "root_m": float(last["root_m"]), "crop": crop, "soil": soil}
        days_left, path = project(eto_mm=eto_next, **args)
        spread = [project(eto_mm=eto_next * f, **args)[0] for f in PROJECTION_SPREAD]
        days_range = None
        if days_left is not None and days_left > 0:
            days_range = [d if d is not None else MAX_PROJECTION_DAYS for d in spread]
        status_word = _status_for(days_left)
        water_by = (as_of + timedelta(days=days_left)).isoformat() if days_left is not None else None
        projection = pd.DataFrame({
            "date": [as_of + timedelta(days=i) for i in range(len(path))], "dr_mm": path,
        })
        until = max(0.0, float(last["raw_mm"] - last["dr_mm"]))
        # Water now refills today's deficit; water later refills what will be
        # gone by then, which is the stress point.
        refill = float(last["dr_mm"]) if days_left == 0 else float(last["raw_mm"])
        sensitive = _sensitive_note(crop, planted, as_of, days_left)
        if days_left is None:
            notes.append(f"more than {MAX_PROJECTION_DAYS} days of water at the current rate")

    confidence = "low" if low_confidence else ("medium" if medium_confidence else "high")
    eto_week = float(daily.tail(PROJECTION_WEATHER_DAYS)["eto_mm"].mean())
    status = WaterStatus(
        field_id=field_id, name=name, crop=crop_text, crop_model=crop.key,
        as_of=as_of.isoformat(), status=status_word, days_left=days_left,
        days_range=days_range, water_by=water_by,
        water_left_in=_inches(float(last["taw_mm"] - last["dr_mm"])),
        until_stress_in=_inches(until) if until is not None else None,
        capacity_in=_inches(float(last["taw_mm"])),
        stress_point_in=_inches(float(last["taw_mm"] - last["raw_mm"])),
        pct_left=round(1.0 - float(last["dr_mm"] / last["taw_mm"]), 3) if last["taw_mm"] else None,
        root_depth_in=round(float(last["root_m"]) * 39.37, 1),
        kc=round(float(last["kc"]), 2),
        eto_in_day=_inches(eto_week),
        use_in_day=_inches(float(last["kc"]) * eto_week),
        refill_net_in=_inches(refill) if refill is not None else None,
        refill_gross_in=(_inches(refill / efficiency)
                         if refill is not None and method != "none" else None),
        method=method, efficiency=efficiency if method != "none" else None,
        start=start.isoformat(), start_reason=start_reason,
        last_irrigation=last_irr.isoformat() if last_irr else None,
        last_rain=last_rain,
        stressed_days_30=int((recent["ks"] < 1.0).sum()),
        weather_through=through.isoformat() if through else None,
        last_image=last_image.isoformat(),
        soil=soil_note, sensitive=sensitive, confidence=confidence, notes=notes,
    )
    return status, daily, projection


#: Below this NDVI the satellite is looking at soil, not a crop.
BARE_NDVI = 0.28
#: Days after planting by which an annual crop should show from orbit.
EMERGENCE_DAYS = 45


def _bare_ground_note(images: pd.DataFrame, crop: Crop, start: date, as_of: date,
                      sown: bool) -> str | None:
    """Say so when the satellite sees bare soil where the log says a crop stands.

    A grove does not read as bare ground, and neither does a crop six weeks
    after planting; either the outline, the crop or the log is wrong, and the
    checkbook would otherwise carry on as if all three were right.
    """
    recent = images[images["date"] >= as_of - timedelta(days=45)]
    if recent.empty:
        return None
    level = float(recent["median"].median())
    if level >= BARE_NDVI:
        return None
    if crop.perennial:
        return (f"the satellite sees bare ground (NDVI {level:.2f}) where {crop.label} "
                "should stand; check the field outline or the crop")
    if sown and (as_of - start).days >= EMERGENCE_DAYS:
        return (f"the satellite still sees mostly bare ground (NDVI {level:.2f}) "
                f"{(as_of - start).days} days after planting; check the stand, the "
                "outline or the planting date")
    return None


def _weather_for(weather: pd.DataFrame, days: Sequence[date], field_id: str, start: date
                 ) -> tuple[pd.DataFrame, int, date | None]:
    """Weather for every day of the balance, filling only the unpublished tail.

    gridMET runs a day or two behind, so the last few days before ``as_of`` are
    filled with the week before them. A gap anywhere else is an error: the
    balance would silently skip a week of water use.
    """
    if weather.empty:
        raise WaterError(
            f"no weather cached for {field_id}. Run 'dosojos-sat weather "
            f"--start {start.isoformat()}' first."
        )
    table = weather.set_index("date")[["eto_mm", "rain_mm"]]
    frame = pd.DataFrame(index=pd.Index(list(days), name="date")).join(table)
    have = frame["eto_mm"].notna().to_numpy()
    if not have.any():
        raise WaterError(
            f"no weather cached for {field_id} from {start.isoformat()}. Run "
            f"'dosojos-sat weather --start {start.isoformat()}' first."
        )
    if not have[0]:
        raise WaterError(
            f"weather for {field_id} starts after {start.isoformat()}. Run "
            f"'dosojos-sat weather --start {start.isoformat()}' first."
        )
    last_have = int(np.flatnonzero(have)[-1])
    holes = ~have[: last_have + 1]
    if holes.any():
        gap = [days[i].isoformat() for i in np.flatnonzero(holes)[:3]]
        raise WaterError(
            f"weather for {field_id} has missing days ({', '.join(gap)}...). Re-run "
            "'dosojos-sat weather' to fill them."
        )
    tail = len(days) - 1 - last_have
    if tail > 10:
        raise WaterError(
            f"weather for {field_id} ends {tail} days before {days[-1].isoformat()}. Run "
            "'dosojos-sat weather' to bring it up to date."
        )
    frame["filled"] = False
    if tail:
        week = frame.iloc[max(0, last_have - PROJECTION_WEATHER_DAYS + 1): last_have + 1]
        frame.iloc[last_have + 1:, frame.columns.get_loc("eto_mm")] = float(week["eto_mm"].mean())
        frame.iloc[last_have + 1:, frame.columns.get_loc("rain_mm")] = 0.0
        frame.iloc[last_have + 1:, frame.columns.get_loc("filled")] = True
    frame["rain_mm"] = frame["rain_mm"].fillna(0.0)
    return frame.reset_index(), tail, days[last_have]


def rank(statuses: Iterable[WaterStatus]) -> list[WaterStatus]:
    """Order fields by who needs water first, and number them.

    Fields that need it now come first, then by days left; a crop at its most
    sensitive stage breaks ties; harvested fields and those with water to spare
    go last.
    """
    def key(s: WaterStatus):
        if s.status == STATUS_HARVESTED:
            return (2, 0, 0, s.field_id)
        days = s.days_left if s.days_left is not None else MAX_PROJECTION_DAYS + 1
        return (0 if s.days_left is not None else 1, days, 0 if s.sensitive else 1, s.field_id)

    ordered = sorted(statuses, key=key)
    for number, status in enumerate(ordered, start=1):
        status.rank = number
    return ordered
