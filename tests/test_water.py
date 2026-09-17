"""Tests for the water checkbook: the field log, the daily balance and the verdict."""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from dosojos_sat import water
from dosojos_sat.soils import manual_profile

DAY0 = date(2026, 5, 1)
#: 2 in/ft: 166.7 mm of available water in the top metre.
SOIL = manual_profile(2.0)
SOIL_NOTE = {"name": "test loam", "awc_in_per_ft": 2.0}


def _days(n: int, start: date = DAY0) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _weather(n: int, *, eto: float = 6.0, rain: float = 0.0, start: date = DAY0) -> pd.DataFrame:
    return pd.DataFrame({"date": _days(n, start), "eto_mm": eto, "rain_mm": rain})


def _ndvi(values: dict[int, float], start: date = DAY0) -> pd.DataFrame:
    return pd.DataFrame({"date": [start + timedelta(days=k) for k in values],
                         "median": list(values.values())})


def _event(day: date, event: str, inches: float | None = None, field_id: str = "f1"):
    return water.LogEvent(field_id, day, event, inches)


def _checkbook(**overrides):
    args = dict(
        field_id="f1", name="Test", crop_text="grain sorghum", soil=SOIL, soil_note=SOIL_NOTE,
        weather=_weather(90), ndvi=_ndvi({5: 0.2, 25: 0.5, 45: 0.8, 70: 0.85}),
        events=[_event(DAY0, "planted")], as_of=DAY0 + timedelta(days=60), method="furrow",
    )
    args.update(overrides)
    return water.checkbook(**args)


# --------------------------------------------------------------------------- #
# Crops and coefficients
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text, key", [
    ("grain sorghum", "sorghum"), ("sorghum trials", "sorghum"), ("Sugarcane", "sugarcane"),
    ("energy cane", "sugarcane"), ("citrus (orange)", "citrus"), ("cotton", "cotton"),
    ("onions", "generic"), (None, "generic"),
])
def test_crop_names_are_matched_by_keyword(text, key) -> None:
    assert water.crop_for(text).key == key


def test_crop_coefficient_follows_the_canopy_between_table_values() -> None:
    """Bare ground uses the initial coefficient, full canopy the mid-season one."""
    sorghum = water.CROPS["sorghum"]
    fc = water.canopy_fraction(np.array([0.10, 0.50, 0.95]))
    kc = water.crop_coefficient(fc, sorghum)
    assert kc[0] == pytest.approx(sorghum.kc_ini)
    assert sorghum.kc_ini < kc[1] < sorghum.kc_mid
    assert kc[2] == pytest.approx(sorghum.kc_mid)


def test_roots_deepen_with_the_canopy_and_never_shrink() -> None:
    sorghum = water.CROPS["sorghum"]
    roots = water.root_depth(np.array([0.0, 0.5, 1.0, 0.3]), sorghum)
    assert roots[0] == pytest.approx(sorghum.root_min_m)
    assert roots[2] == pytest.approx(sorghum.root_max_m)
    assert roots[3] == pytest.approx(sorghum.root_max_m)      # senescence keeps the roots


def test_a_hot_day_leaves_less_slack() -> None:
    """FAO-56: the depletion allowed before stress shrinks as demand rises."""
    crop = water.CROPS["sorghum"]
    assert water.depletion_fraction(crop, 8.0) < crop.p < water.depletion_fraction(crop, 2.0)


# --------------------------------------------------------------------------- #
# Field log
# --------------------------------------------------------------------------- #


def _log(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "field_log.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_field_log_accepts_spreadsheet_habits(tmp_path: Path) -> None:
    """Aliases, US dates and blank inches are what a farm spreadsheet will hold."""
    path = _log(tmp_path, "field_id,date,event,inches,notes\n"
                          "f1,3/15/2026,Planting,,\n"
                          "f1,2026-04-20,watered,4.5,district ticket\n"
                          "f1,2026-05-01,irrigation,,\n"
                          "f1,2026-05-03,rain,0.8,gauge\n")
    events = water.read_field_log(path)
    assert [e.event for e in events] == ["planted", "irrigated", "irrigated", "rain"]
    assert events[0].day == date(2026, 3, 15)
    assert events[1].inches == 4.5 and events[2].inches is None


@pytest.mark.parametrize("row, message", [
    ("f1,2026-04-20,fertilised,,", "line 2"),
    ("f1,2026-04-20,rain,,", "gauge reading"),
    ("f1,20-04-2026,irrigated,,", "YYYY-MM-DD"),
    ("f1,2026-04-20,irrigated,-2,", "negative"),
])
def test_a_bad_log_line_is_named(tmp_path: Path, row: str, message: str) -> None:
    path = _log(tmp_path, "field_id,date,event,inches,notes\n" + row + "\n")
    with pytest.raises(water.FieldLogError, match=message):
        water.read_field_log(path)


def test_no_log_file_means_no_events(tmp_path: Path) -> None:
    assert water.read_field_log(tmp_path / "absent.csv") == []


# --------------------------------------------------------------------------- #
# The daily balance
# --------------------------------------------------------------------------- #


def _balance(n: int = 40, *, rain=None, irrigations=None, efficiency: float = 1.0):
    citrus = water.CROPS["citrus"]         # constant coefficient and roots: easy arithmetic
    return water.run_balance(
        days=_days(n), eto_mm=np.full(n, 6.0),
        rain_mm=np.zeros(n) if rain is None else np.asarray(rain, dtype=float),
        ndvi=np.full(n, 0.8), crop=citrus, soil=SOIL, irrigations=irrigations or {},
        efficiency=efficiency,
    )


def test_depletion_grows_by_the_crop_s_use_until_stress_slows_it() -> None:
    daily = _balance()
    use = 0.65 * 6.0
    assert daily["dr_mm"].iloc[9] == pytest.approx(10 * use)
    stressed = daily[daily["ks"] < 1.0]
    assert not stressed.empty
    assert (stressed["etc_mm"] < use).all()          # a stressed crop uses less
    assert (daily["dr_mm"] <= daily["taw_mm"]).all()


def test_a_full_irrigation_refills_the_root_zone() -> None:
    daily = _balance(irrigations={DAY0 + timedelta(days=20): None})
    assert daily["dr_mm"].iloc[20] == pytest.approx(0.65 * 6.0)   # only that day's use left


def test_delivered_inches_count_after_losses() -> None:
    dry = _balance()
    wet = _balance(irrigations={DAY0 + timedelta(days=5): 1.0}, efficiency=0.5)
    assert dry["dr_mm"].iloc[5] - wet["dr_mm"].iloc[5] == pytest.approx(0.5 * 25.4)


def test_a_light_shower_evaporates_and_a_storm_drains() -> None:
    rain = np.zeros(40)
    rain[10], rain[20] = 1.0, 500.0            # under 20% of ETo, and far past full
    daily = _balance(rain=rain)
    assert daily["rain_effective_mm"].iloc[10] == 0.0
    assert daily["dr_mm"].iloc[20] == 0.0
    assert daily["drained_mm"].iloc[20] > 0.0


def test_projection_counts_days_to_the_stress_point() -> None:
    citrus = water.CROPS["citrus"]
    days, path = water.project(depletion_mm=0.0, eto_mm=6.0, kc=0.65, root_m=1.0,
                               crop=citrus, soil=SOIL)
    raw = water.depletion_fraction(citrus, 0.65 * 6.0) * SOIL.taw_mm(1.0)
    assert days == math.ceil(raw / (0.65 * 6.0))
    assert path[-1] >= raw > path[-2]


def test_projection_is_zero_when_already_short() -> None:
    citrus = water.CROPS["citrus"]
    days, _ = water.project(depletion_mm=150.0, eto_mm=6.0, kc=0.65, root_m=1.0,
                            crop=citrus, soil=SOIL)
    assert days == 0


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


def test_a_watered_field_gets_a_date_and_an_amount() -> None:
    status, daily, projection = _checkbook(
        events=[_event(DAY0, "planted"), _event(DAY0 + timedelta(days=55), "irrigated")]
    )
    assert status.days_left and status.days_left > 0
    assert status.water_by == (date.fromisoformat(status.as_of)
                               + timedelta(days=status.days_left)).isoformat()
    low, high = status.days_range
    assert low <= status.days_left <= high
    assert status.refill_gross_in == pytest.approx(status.refill_net_in / 0.65, abs=0.02)
    assert status.confidence == "high"
    assert len(daily) == 61 and projection["dr_mm"].iloc[0] == pytest.approx(daily["dr_mm"].iloc[-1])


def test_a_crop_just_planted_starts_from_bare_soil_whatever_grew_before() -> None:
    """Weeds or the last crop before planting must not inflate early water use."""
    status, daily, _ = _checkbook(ndvi=_ndvi({-10: 0.8, 30: 0.6, 55: 0.8}),
                                  as_of=DAY0 + timedelta(days=40))
    assert daily["kc"].iloc[0] == pytest.approx(water.CROPS["sorghum"].kc_ini)


def test_a_harvested_field_needs_no_water() -> None:
    status, daily, projection = _checkbook(
        events=[_event(DAY0, "planted"), _event(DAY0 + timedelta(days=50), "harvested")]
    )
    assert status.status == water.STATUS_HARVESTED
    assert status.days_left is None and projection.empty
    assert daily["date"].iloc[-1] == DAY0 + timedelta(days=50)


def test_without_records_the_start_is_assumed_and_said_so() -> None:
    status, _, _ = _checkbook(events=[])
    assert status.confidence == "low"
    assert any("field_log.csv" in note for note in status.notes)


def test_unpublished_weather_days_are_filled_and_flagged() -> None:
    status, daily, _ = _checkbook(weather=_weather(59))       # stops two days short
    assert daily["filled_weather"].sum() == 2
    assert status.confidence == "medium"
    assert status.weather_through == (DAY0 + timedelta(days=58)).isoformat()


def test_missing_weather_names_the_command_to_run() -> None:
    with pytest.raises(water.WaterError, match="dosojos-sat weather"):
        _checkbook(weather=_weather(30, start=DAY0 + timedelta(days=10)))


def test_a_grove_that_reads_as_bare_ground_is_doubted() -> None:
    status, _, _ = _checkbook(crop_text="citrus", method="flood",
                              ndvi=_ndvi({k: 0.15 for k in range(0, 61, 5)}),
                              events=[_event(DAY0 + timedelta(days=30), "irrigated")])
    assert status.confidence == "low"
    assert any("bare ground" in note for note in status.notes)


def test_the_stage_that_can_least_afford_stress_is_named() -> None:
    status, _, _ = _checkbook(as_of=DAY0 + timedelta(days=63),
                              events=[_event(DAY0, "planted"),
                                      _event(DAY0 + timedelta(days=60), "irrigated")])
    assert status.sensitive and "boot to flowering now" in status.sensitive


def _hot_weather(n: int, high_c: float, low_c: float) -> pd.DataFrame:
    frame = _weather(n)
    frame["tmax_c"], frame["tmin_c"] = high_c, low_c
    return frame


def test_sorghum_with_temperatures_is_staged_by_heat_not_the_calendar() -> None:
    """Day 40 after a hot planting is already in the critical stretch; the calendar says not yet."""
    events = [_event(DAY0, "planted"), _event(DAY0 + timedelta(days=38), "irrigated")]
    # 35/18.3 C is 95/65 F, 30 heat units a day: 1200 by day 40, past panicle initiation.
    status, _, _ = _checkbook(weather=_hot_weather(90, 35.0, 18.3333), events=events,
                              as_of=DAY0 + timedelta(days=40), maturity="short")
    assert status.stage["stage"] == "panicle_initiation"
    assert status.sensitive and "heat units" in status.sensitive
    assert status.stage["aphid_threshold_pct"] == 20


def test_a_cool_spring_holds_the_warning_back() -> None:
    """Day 63 would have warned by the calendar; at 10 heat units a day it is far too early."""
    events = [_event(DAY0, "planted"), _event(DAY0 + timedelta(days=60), "irrigated")]
    # 70/50 F: 10 heat units a day, 630 by day 63, still five-leaf-ish.
    status, _, _ = _checkbook(weather=_hot_weather(90, 21.111, 10.0), events=events,
                              as_of=DAY0 + timedelta(days=63), maturity="medium")
    assert status.stage["stage"] in ("four_leaf", "five_leaf")
    assert status.sensitive is None


def test_other_crops_carry_no_sorghum_stage() -> None:
    status, _, _ = _checkbook(crop_text="corn", weather=_hot_weather(90, 35.0, 18.3333))
    assert status.stage is None


def test_a_rainfed_field_has_no_delivered_amount() -> None:
    status, _, _ = _checkbook(method="none")
    assert status.refill_gross_in is None and status.efficiency is None


def _status(field_id: str, status: str, days, sensitive=None) -> water.WaterStatus:
    blank = {name: None for name in water.WaterStatus.__dataclass_fields__}
    blank.update(field_id=field_id, name=field_id, crop="c", crop_model="c", as_of="2026-01-01",
                 status=status, days_left=days, method="furrow", start="2026-01-01",
                 start_reason="", stressed_days_30=0, soil={}, sensitive=sensitive,
                 confidence="high", notes=[])
    return water.WaterStatus(**blank)


def test_fields_are_ranked_by_who_needs_water_first() -> None:
    ranked = water.rank([
        _status("plenty", water.STATUS_OK, None),
        _status("cut", water.STATUS_HARVESTED, None),
        _status("week", water.STATUS_WEEK, 6),
        _status("dry", water.STATUS_NOW, 0),
        _status("week-flowering", water.STATUS_WEEK, 6, sensitive="flowering"),
    ])
    assert [s.field_id for s in ranked] == ["dry", "week-flowering", "week", "plenty", "cut"]
    assert [s.rank for s in ranked] == [1, 2, 3, 4, 5]
