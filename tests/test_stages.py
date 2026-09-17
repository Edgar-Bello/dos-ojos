"""Tests for sorghum growth stages from heat units (Texas A&M AgriLife B-6137)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from dosojos_sat import stages


def f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32.0) * 5.0 / 9.0


def season(planted: date, days: int, high_f: float = 95.0, low_f: float = 65.0,
           skip: set[int] = frozenset()) -> pd.DataFrame:
    """A run of identical days: 95/65 F is 30 heat units a day."""
    rows = [{"date": planted + timedelta(days=i), "tmax_c": f_to_c(high_f),
             "tmin_c": f_to_c(low_f)}
            for i in range(0, days + 1) if i not in skip]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Heat units
# --------------------------------------------------------------------------- #


def test_the_bulletin_s_own_worked_example() -> None:
    """B-6137: a 94 F high and a 65 F low is 29.5 growing degree units."""
    assert stages.daily_gdu(f_to_c(94), f_to_c(65)) == pytest.approx(29.5)


def test_a_cold_day_adds_nothing_rather_than_taking_away() -> None:
    """Temperatures under 50 F count as 50, so development stalls; it does not run backwards."""
    assert stages.daily_gdu(f_to_c(45), f_to_c(30)) == 0


def test_heat_past_100_f_does_not_speed_the_crop_up() -> None:
    """A 108 F Valley afternoon counts as 100 F."""
    assert stages.daily_gdu(f_to_c(108), f_to_c(70)) == stages.daily_gdu(f_to_c(100), f_to_c(70))


def test_a_medium_hybrid_sits_between_short_and_long() -> None:
    short = dict(stages.thresholds("short"))
    long_ = dict(stages.thresholds("long"))
    medium = dict(stages.thresholds("medium"))
    assert short["boot"] == 1683 and long_["boot"] == 1750
    assert medium["boot"] == round((1683 + 1750) / 2)
    assert medium["black_layer"] == round((2673 + 3360) / 2)


def test_an_unknown_maturity_is_taken_as_medium_and_says_so() -> None:
    planted = date(2025, 3, 1)
    estimate = stages.estimate(season(planted, 40), planted, planted + timedelta(days=40), None)
    assert estimate.maturity == "medium" and estimate.maturity_assumed


# --------------------------------------------------------------------------- #
# Where the crop is
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("days, expected", [
    (5, "planted"),            # 150 units, not up yet
    (7, "emergence"),          # 210
    (17, "three_leaf"),        # 510
    (31, "panicle_initiation"),   # 930, short season
    (57, "boot"),              # 1710
    (62, "flowering"),         # 1860
    (90, "black_layer"),       # 2700
])
def test_stage_follows_the_short_season_table(days: int, expected: str) -> None:
    planted = date(2025, 3, 1)
    estimate = stages.estimate(season(planted, days), planted, planted + timedelta(days=days),
                               "short")
    assert estimate.stage == expected


def test_a_cool_spring_is_behind_a_warm_one_on_the_same_day() -> None:
    """The whole point: day 50 after a February planting is not day 50 after an April one."""
    planted = date(2025, 2, 15)
    as_of = planted + timedelta(days=50)
    cool = stages.estimate(season(planted, 50, 75, 50), planted, as_of, "medium")
    warm = stages.estimate(season(planted, 50, 95, 70), planted, as_of, "medium")
    assert stages.STAGE_KEYS.index(cool.stage) < stages.STAGE_KEYS.index(warm.stage)


def test_the_next_stage_is_projected_from_recent_heat() -> None:
    planted = date(2025, 3, 1)
    as_of = planted + timedelta(days=40)                 # 1200 units, short season
    estimate = stages.estimate(season(planted, 40), planted, as_of, "short")
    assert estimate.stage == "panicle_initiation"
    assert estimate.next_stage == "flag_leaf" and estimate.next_gdu == 1287
    assert estimate.gdu_per_day == pytest.approx(30.0)
    assert estimate.next_date == as_of + timedelta(days=3)   # 87 more at 30 a day


def test_every_milestone_gets_a_date_past_or_projected() -> None:
    planted = date(2025, 3, 1)
    estimate = stages.estimate(season(planted, 60), planted, planted + timedelta(days=60), "short")
    reached = [m for m in estimate.milestones if m[1] <= estimate.gdu]
    ahead = [m for m in estimate.milestones if m[1] > estimate.gdu]
    assert all(d is not None and d <= estimate.as_of for _, _, d in reached)
    assert all(d is not None and d > estimate.as_of for _, _, d in ahead)


def test_a_season_with_missing_temperatures_claims_nothing() -> None:
    """Gaps would put the crop earlier than it really is; better to say nothing."""
    planted = date(2025, 3, 1)
    gappy = season(planted, 40, skip=set(range(10, 25)))
    assert stages.estimate(gappy, planted, planted + timedelta(days=40), "short") is None


def test_no_temperatures_at_all_is_not_an_error() -> None:
    planted = date(2025, 3, 1)
    weather = pd.DataFrame({"date": [planted + timedelta(days=i) for i in range(30)],
                            "eto_mm": [6.0] * 30, "rain_mm": [0.0] * 30})
    assert stages.estimate(weather, planted, planted + timedelta(days=29), "short") is None


# --------------------------------------------------------------------------- #
# Water and pests
# --------------------------------------------------------------------------- #


def test_panicle_initiation_to_flowering_is_the_critical_stretch() -> None:
    planted = date(2025, 3, 1)
    before = stages.estimate(season(planted, 25), planted, planted + timedelta(days=25), "short")
    during = stages.estimate(season(planted, 45), planted, planted + timedelta(days=45), "short")
    after = stages.estimate(season(planted, 80), planted, planted + timedelta(days=80), "short")
    assert not before.critical and before.critical_in_days == 6     # 750 -> 924 at 30/day
    assert during.critical
    assert not after.critical and after.critical_in_days is None


@pytest.mark.parametrize("stage, threshold", [
    ("five_leaf", 20), ("boot", 20), ("heading", 30), ("flowering", 30),
    ("hard_dough", 30), ("black_layer", None),
])
def test_sugarcane_aphid_threshold_by_stage(stage: str, threshold: int | None) -> None:
    """Sorghum Checkoff guide: 20% up to boot, 30% heading through dough, harvest-only after."""
    assert stages.aphid_threshold(stage) == threshold


@pytest.mark.parametrize("percent, stage, verdict", [
    (35, "flowering", "above"), (30, "flowering", "above"), (27, "flowering", "near"),
    (10, "flowering", "below"), (22, "boot", "above"), (50, "black_layer", "harvest"),
])
def test_a_scouting_count_against_the_threshold(percent, stage, verdict) -> None:
    assert stages.aphid_verdict(percent, stage) == verdict


def test_midge_is_watched_first_while_the_crop_flowers() -> None:
    planted = date(2025, 3, 1)
    estimate = stages.estimate(season(planted, 62), planted, planted + timedelta(days=62), "short")
    assert estimate.stage == "flowering"
    assert estimate.watch[0] == "midge" and "sugarcane_aphid" in estimate.watch


def test_a_mature_crop_is_told_about_harvest_not_aphids() -> None:
    planted = date(2025, 3, 1)
    estimate = stages.estimate(season(planted, 95), planted, planted + timedelta(days=95), "short")
    assert estimate.stage == "black_layer"
    assert "harvest" in estimate.watch and "sugarcane_aphid" not in estimate.watch


def test_the_estimate_serialises_for_the_status_file() -> None:
    planted = date(2025, 3, 1)
    estimate = stages.estimate(season(planted, 45), planted, planted + timedelta(days=45), "short")
    data = estimate.to_dict()
    assert data["stage"] == estimate.stage and data["stage_label"] == estimate.label
    assert data["milestones"][0]["stage"] == "emergence"
