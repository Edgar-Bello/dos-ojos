"""Grain sorghum growth stages from heat units, and what to watch for at each.

Sorghum hybrids grown today ignore day length: how far along a crop is depends
on the heat it has had. A Valley field planted in a cool February and one planted
in a warm April are not at the same stage on the same day after planting, which
is why a fixed "days 50 to 80" window was the wrong tool.

Everything here follows published Texas guidance, cited where it is used:

- **Stages.** Gerik, Bean and Vanderlip, *Sorghum Growth and Development*, Texas
  A&M AgriLife Extension B-6137 (2003). Growing degree units are counted from
  planting as ``(high + low) / 2 - 50`` in Fahrenheit, with any temperature above
  100 F counted as 100 and any below 50 F as 50. Table 1 gives the cumulative
  units to each stage for short- and long-season hybrids; a medium hybrid is
  taken halfway between them.
- **Sugarcane aphid.** The United Sorghum Checkoff Program's *Sugarcane Aphid*
  pocket guide (2021), from the regional team's work: 20% of plants infested before
  and at boot, 30% from heading through dough, and at black layer only to prevent
  harvest trouble. The Lower Rio Grande Valley's own Texas A&M AgriLife IPM
  newsletter (PestCast, Weslaco, 20 May 2023) applies the same 30% in grain fill.
- **Sorghum midge.** One midge per head at flowering (the same PestCast).

None of this replaces walking the field. It says when to walk it, and what to count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

#: Heat-unit limits, Fahrenheit (B-6137).
BASE_F = 50.0
CAP_F = 100.0

#: Recent days whose average heat projects the next stage.
PROJECTION_DAYS = 14
#: Below this share of days with temperatures, the season total is not trusted.
MIN_COVERAGE = 0.9

MATURITIES = ("short", "medium", "long")
DEFAULT_MATURITY = "medium"

#: B-6137 Table 1: cumulative GDU from planting, (short season, long season).
_TABLE = (
    ("emergence", 200, 200),
    ("three_leaf", 500, 500),
    ("four_leaf", 575, 575),
    ("five_leaf", 660, 660),
    ("panicle_initiation", 924, 1365),
    ("flag_leaf", 1287, 1470),
    ("boot", 1683, 1750),
    ("heading", 1749, 1890),
    ("flowering", 1848, 1995),
    ("soft_dough", 2211, 2310),
    ("hard_dough", 2508, 2765),
    ("black_layer", 2673, 3360),
)

#: The stage keys in order, with "planted" before emergence.
STAGE_KEYS = ("planted",) + tuple(row[0] for row in _TABLE)

STAGE_LABELS = {
    "planted": "planted, not up yet",
    "emergence": "emergence",
    "three_leaf": "three-leaf",
    "four_leaf": "four-leaf",
    "five_leaf": "five-leaf",
    "panicle_initiation": "panicle initiation",
    "flag_leaf": "flag leaf visible",
    "boot": "boot",
    "heading": "heading",
    "flowering": "flowering",
    "soft_dough": "soft dough",
    "hard_dough": "hard dough",
    "black_layer": "black layer (mature)",
}

#: B-6137: panicle initiation to flowering sets seed number, 70% of final yield,
#: and the crop's water need peaks at boot. The stretch that can least afford to
#: run dry.
CRITICAL_FROM, CRITICAL_TO = "panicle_initiation", "flowering"

#: Sugarcane aphid: share of infested plants that warrants treatment, by stage.
#: None at black layer, where it is treated only if honeydew would stop the combine.
_APHID_BEFORE_HEADING = 20
_APHID_HEADING_ON = 30

#: Sorghum midge: one per head, checked while the crop is flowering.
MIDGE_PER_HEAD = 1


def thresholds(maturity: str | None) -> list[tuple[str, float]]:
    """Cumulative GDU to each stage for a hybrid of this maturity."""
    maturity = maturity if maturity in MATURITIES else DEFAULT_MATURITY
    out = []
    for key, short, long_ in _TABLE:
        if maturity == "short":
            value = short
        elif maturity == "long":
            value = long_
        else:
            value = round((short + long_) / 2)
        out.append((key, float(value)))
    return out


def c_to_f(celsius):
    return celsius * 9.0 / 5.0 + 32.0


def daily_gdu(high_c: float, low_c: float) -> float:
    """One day's growing degree units, B-6137's way: both ends clamped to 50-100 F."""
    high = min(max(c_to_f(high_c), BASE_F), CAP_F)
    low = min(max(c_to_f(low_c), BASE_F), CAP_F)
    return (high + low) / 2.0 - BASE_F


def gdu_series(weather: pd.DataFrame, planted: date, as_of: date) -> pd.DataFrame:
    """Daily and cumulative GDU from the day after planting to ``as_of``.

    Days without temperatures are left out; the caller checks how many there were.
    """
    if weather is None or weather.empty or "tmax_c" not in weather or "tmin_c" not in weather:
        return pd.DataFrame(columns=["date", "gdu", "cumulative"])
    frame = weather[["date", "tmax_c", "tmin_c"]].copy()
    frame = frame[(frame["date"] > planted) & (frame["date"] <= as_of)]
    frame = frame.dropna(subset=["tmax_c", "tmin_c"]).sort_values("date")
    frame["gdu"] = [daily_gdu(h, l) for h, l in zip(frame["tmax_c"], frame["tmin_c"])]
    frame["cumulative"] = frame["gdu"].cumsum()
    return frame[["date", "gdu", "cumulative"]].reset_index(drop=True)


@dataclass(frozen=True)
class StageEstimate:
    """Where a sorghum field is, where it goes next, and what to watch for now."""

    planted: date
    as_of: date
    maturity: str
    maturity_assumed: bool
    gdu: float
    days_after_planting: int
    stage: str
    next_stage: str | None
    next_gdu: float | None
    next_date: date | None           # projected from the last two weeks' heat
    gdu_per_day: float | None
    critical: bool                   # panicle initiation to flowering
    critical_in_days: int | None     # days until that stretch starts, if still ahead
    aphid_threshold_pct: int | None
    watch: tuple[str, ...]           # sugarcane_aphid, midge, headworm, harvest
    coverage: float                  # share of days since planting with temperatures
    milestones: list[tuple[str, float, date | None]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return STAGE_LABELS[self.stage]

    def to_dict(self) -> dict:
        return {
            "planted": self.planted.isoformat(), "as_of": self.as_of.isoformat(),
            "maturity": self.maturity, "maturity_assumed": self.maturity_assumed,
            "gdu": round(self.gdu), "days_after_planting": self.days_after_planting,
            "stage": self.stage, "stage_label": self.label,
            "next_stage": self.next_stage,
            "next_gdu": None if self.next_gdu is None else round(self.next_gdu),
            "next_date": self.next_date.isoformat() if self.next_date else None,
            "gdu_per_day": None if self.gdu_per_day is None else round(self.gdu_per_day, 1),
            "critical": self.critical, "critical_in_days": self.critical_in_days,
            "aphid_threshold_pct": self.aphid_threshold_pct, "watch": list(self.watch),
            "coverage": round(self.coverage, 3),
            "milestones": [{"stage": k, "gdu": round(g),
                            "date": d.isoformat() if d else None}
                           for k, g, d in self.milestones],
        }


def aphid_threshold(stage: str) -> int | None:
    """Percent of plants infested that warrants treating sugarcane aphid at this stage."""
    order = STAGE_KEYS.index(stage)
    if stage == "black_layer":
        return None
    if order < STAGE_KEYS.index("heading"):
        return _APHID_BEFORE_HEADING
    return _APHID_HEADING_ON


def watch_for(stage: str) -> tuple[str, ...]:
    """What is worth scouting for at this stage, most pressing first."""
    order = STAGE_KEYS.index(stage)
    items = []
    if STAGE_KEYS.index("emergence") <= order < STAGE_KEYS.index("black_layer"):
        items.append("sugarcane_aphid")
    if stage in ("heading", "flowering"):
        items.insert(0, "midge")
    if stage in ("flowering", "soft_dough", "hard_dough"):
        items.append("headworm")
    if stage in ("hard_dough", "black_layer"):
        items.append("harvest")
    return tuple(items)


def estimate(
    weather: pd.DataFrame,
    planted: date,
    as_of: date,
    maturity: str | None = None,
) -> StageEstimate | None:
    """The field's growth stage on ``as_of``, or None without enough temperatures.

    A season whose temperature record has gaps would put the crop earlier than it
    is, so below ``MIN_COVERAGE`` of the days since planting nothing is claimed.
    """
    if as_of < planted:
        return None
    days = (as_of - planted).days
    series = gdu_series(weather, planted, as_of)
    coverage = (len(series) / days) if days else 1.0
    if days and coverage < MIN_COVERAGE:
        return None

    assumed = maturity not in MATURITIES
    maturity = DEFAULT_MATURITY if assumed else maturity
    table = thresholds(maturity)
    total = float(series["cumulative"].iloc[-1]) if len(series) else 0.0

    stage, next_stage, next_gdu = "planted", table[0][0], table[0][1]
    for key, need in table:
        if total >= need:
            stage = key
        else:
            next_stage, next_gdu = key, need
            break
    else:
        next_stage = next_gdu = None

    recent = series.tail(PROJECTION_DAYS)
    rate = float(recent["gdu"].mean()) if len(recent) else None
    last_day = series["date"].iloc[-1] if len(series) else planted

    def projected(need: float) -> date | None:
        if need <= total:
            hit = series[series["cumulative"] >= need]
            return hit["date"].iloc[0] if len(hit) else None
        if not rate or rate <= 0:
            return None
        return last_day + timedelta(days=math.ceil((need - total) / rate))

    milestones = [(key, need, projected(need)) for key, need in table]
    order = STAGE_KEYS.index(stage)
    start = STAGE_KEYS.index(CRITICAL_FROM)
    end = STAGE_KEYS.index(CRITICAL_TO)
    critical = start <= order <= end
    critical_in_days = None
    if order < start:
        when = projected(dict(table)[CRITICAL_FROM])
        critical_in_days = (when - as_of).days if when else None

    return StageEstimate(
        planted=planted, as_of=as_of, maturity=maturity, maturity_assumed=assumed,
        gdu=total, days_after_planting=days, stage=stage, next_stage=next_stage,
        next_gdu=next_gdu, next_date=projected(next_gdu) if next_gdu else None,
        gdu_per_day=rate, critical=critical, critical_in_days=critical_in_days,
        aphid_threshold_pct=aphid_threshold(stage), watch=watch_for(stage),
        coverage=coverage, milestones=milestones,
    )


def aphid_verdict(percent_infested: float, stage: str) -> str:
    """'above', 'near' (within 5 points) or 'below' the threshold; 'harvest' at black layer."""
    threshold = aphid_threshold(stage)
    if threshold is None:
        return "harvest"
    if percent_infested >= threshold:
        return "above"
    if percent_infested >= threshold - 5:
        return "near"
    return "below"
