"""Tests for the day-of-year climatology against synthetic data with known answers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dosojos_sat.baseline import (
    DAYS_IN_YEAR,
    BaselineParams,
    build_baseline,
    build_climatology,
    classify_confidence,
    confidence_summary,
    doy_distance,
    smooth_circular,
    thin_stretches,
)

PARAMS = BaselineParams(season=2026, history_years=4, doy_window=12, smooth_window=15)


def _history(rows: list[tuple[int, int, float]]) -> pd.DataFrame:
    """Build a history frame from ``(year, doy, median)`` triples."""
    return pd.DataFrame(rows, columns=["year", "doy", "median"])


# --------------------------------------------------------------------------- #
# Day-of-year arithmetic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (100, 112, 12),      # inside a 12-day window
        (100, 113, 13),      # just outside
        (1, 366, 1),         # across the new year
        (360, 5, 11),        # late December to early January
        (200, 200, 0),       # same day
        (1, 184, 183),       # maximum possible separation
    ],
)
def test_doy_distance_wraps_the_year(a: int, b: int, expected: int) -> None:
    """Distance is circular, so December and January are neighbours."""
    assert float(doy_distance(a, b)) == pytest.approx(expected)


def test_doy_distance_is_symmetric() -> None:
    """Direction around the circle must not change the distance."""
    days = np.arange(1, DAYS_IN_YEAR + 1)
    assert np.array_equal(doy_distance(days, 200), doy_distance(200, days))


def test_history_window_excludes_the_current_season() -> None:
    """Season 2026 with four history years reads 2022 to 2025."""
    assert PARAMS.year_range == (2022, 2025)
    assert BaselineParams(season=2026, history_years=2).year_range == (2024, 2025)


# --------------------------------------------------------------------------- #
# Pooling and percentiles
# --------------------------------------------------------------------------- #


def test_bin_pools_only_observations_inside_the_window() -> None:
    """A day at distance 13 must not reach a bin with a 12-day window."""
    history = _history([(2022, 100, 1.0), (2023, 112, 2.0), (2024, 113, 99.0)])
    table = build_climatology(history, PARAMS)
    row = table[table.doy == 100].iloc[0]
    assert row["n_obs"] == 2                       # 99.0 at distance 13 is excluded
    assert row["median"] == pytest.approx(1.5)


def test_percentiles_match_a_known_distribution() -> None:
    """Values 0.00..1.00 in even steps give exactly the round percentiles."""
    history = _history([(2022 + i % 4, 200, v) for i, v in enumerate(np.arange(101) / 100)])
    row = build_climatology(history, PARAMS).query("doy == 200").iloc[0]
    assert row["median"] == pytest.approx(0.50)
    assert row["p10"] == pytest.approx(0.10)
    assert row["p25"] == pytest.approx(0.25)
    assert row["p75"] == pytest.approx(0.75)
    assert row["p90"] == pytest.approx(0.90)
    assert row["n_obs"] == 101
    assert row["n_years"] == 4


def test_bin_counts_distinct_years_not_observations() -> None:
    """Ten looks in one year is not four years of evidence."""
    history = _history([(2022, 200 + i, 0.5) for i in range(10)])
    row = build_climatology(history, PARAMS).query("doy == 200").iloc[0]
    assert row["n_obs"] == 10
    assert row["n_years"] == 1


def test_bins_wrap_across_the_new_year() -> None:
    """A 1 January bin must draw on late-December history."""
    history = _history([(2022, 360, 0.4), (2023, 361, 0.6)])
    row = build_climatology(history, PARAMS).query("doy == 1").iloc[0]
    assert row["n_obs"] == 2                       # 360 and 361 are 7 and 6 days away
    assert row["median"] == pytest.approx(0.5)


def test_empty_bins_are_kept_with_zero_counts() -> None:
    """Sparse coverage stays visible instead of vanishing from the table."""
    history = _history([(2022, 200, 0.5)])
    table = build_climatology(history, PARAMS)
    assert len(table) == DAYS_IN_YEAR
    far = table.query("doy == 20").iloc[0]
    assert far["n_obs"] == 0
    assert np.isnan(far["median"])


def test_no_history_gives_a_full_empty_table() -> None:
    """A field with no history yields 366 empty bins rather than an error."""
    table = build_climatology(_history([]), PARAMS)
    assert len(table) == DAYS_IN_YEAR
    assert table["n_obs"].sum() == 0


def test_nan_observations_are_ignored() -> None:
    """A NaN median must not poison the bin it falls in."""
    history = _history([(2022, 200, 0.4), (2023, 200, float("nan")), (2024, 200, 0.6)])
    row = build_climatology(history, PARAMS).query("doy == 200").iloc[0]
    assert row["n_obs"] == 2
    assert row["median"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Smoothing
# --------------------------------------------------------------------------- #


def test_smoothing_preserves_a_constant() -> None:
    """A flat series must survive the smoother unchanged, including at the wrap."""
    flat = pd.Series([0.5] * DAYS_IN_YEAR)
    smoothed = smooth_circular(flat, 15)
    assert np.allclose(smoothed.to_numpy(), 0.5)


def test_smoothing_wraps_rather_than_tapering_at_the_edges() -> None:
    """1 January is smoothed against late December, not against nothing.

    A non-wrapping smoother would pull the year's ends toward whatever lies
    inside it, putting a false step right where winter crops sit.
    """
    series = pd.Series([0.0] * (DAYS_IN_YEAR - 1) + [10.0])   # spike on the last day
    smoothed = smooth_circular(series, 5)
    assert smoothed.iloc[0] > 0                                # spike reached 1 January
    assert smoothed.iloc[-1] > 0
    assert smoothed.iloc[180] == pytest.approx(0.0)            # but not the far side


def test_smoothing_fills_a_short_gap_from_its_neighbours() -> None:
    """NaN bins are skipped by the mean, which is what closes small holes."""
    series = pd.Series([1.0, 1.0, np.nan, 1.0, 1.0])
    assert smooth_circular(series, 3).iloc[2] == pytest.approx(1.0)


def test_window_of_one_is_a_passthrough() -> None:
    """Turning smoothing off must not alter the values."""
    series = pd.Series([0.1, 0.9, 0.3])
    assert smooth_circular(series, 1).tolist() == [0.1, 0.9, 0.3]


def test_interpolated_bins_are_marked() -> None:
    """Bins that borrowed their value from neighbours are flagged as such."""
    history = _history([(2022 + y, doy, 0.5) for y in range(4) for doy in range(1, 130, 6)])
    table = build_baseline(history, PARAMS)
    borrowed = table[table.interpolated == 1]
    assert not borrowed.empty
    assert (borrowed["n_obs"] == 0).all()          # no data of its own
    assert borrowed["median"].notna().all()        # but a value was reached


def test_unreachable_bins_stay_empty_and_unmarked() -> None:
    """A hole too wide for the smoother stays NaN rather than being invented."""
    history = _history([(2022 + y, 200, 0.5) for y in range(4)])
    table = build_baseline(history, PARAMS)
    far = table.query("doy == 20").iloc[0]
    assert np.isnan(far["median"])
    assert far["interpolated"] == 0
    assert far["confidence"] == "none"


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("n_obs", "n_years", "expected"),
    [
        (12, 4, "high"),
        (6, 3, "high"),
        (6, 2, "medium"),      # enough looks, too few years
        (20, 1, "low"),
        (5, 3, "medium"),
        (3, 2, "medium"),
        (2, 2, "low"),
        (1, 1, "low"),
        (0, 0, "none"),
    ],
)
def test_confidence_grades(n_obs: int, n_years: int, expected: str) -> None:
    """Grades depend on both how many looks and how many distinct years."""
    assert classify_confidence(n_obs, n_years) == expected


def test_many_observations_from_one_year_is_not_high_confidence() -> None:
    """Twenty looks in a single season describe that season, not a normal."""
    assert classify_confidence(20, 1) == "low"


def test_dense_history_grades_high_almost_everywhere() -> None:
    """Four years of five-day revisit should give a well-supported baseline."""
    history = _history(
        [(2022 + y, doy, 0.5) for y in range(4) for doy in range(1, DAYS_IN_YEAR, 5)]
    )
    grades = confidence_summary(build_baseline(history, PARAMS))
    assert grades["high"] == DAYS_IN_YEAR
    assert grades["none"] == 0


def test_thin_stretches_report_where_the_baseline_is_weak() -> None:
    """Long weak runs are reported; isolated weak days are not."""
    history = _history(
        [(2022 + y, doy, 0.5) for y in range(4) for doy in range(1, 200, 5)]
    )
    table = build_baseline(history, PARAMS)
    stretches = thin_stretches(table, min_length=5)
    assert stretches, "expected the unobserved half of the year to be reported"
    start, end, grade = stretches[0]
    assert grade in {"low", "none"}
    assert 200 <= start <= DAYS_IN_YEAR


def test_doy_label_is_platform_independent() -> None:
    """strftime's no-padding flag differs by platform, so the day is built by hand."""
    from dosojos_sat.cli import _doy_label

    assert _doy_label(1) == "1 Jan"
    assert _doy_label(60) == "29 Feb"      # a leap year is used for labelling
    assert _doy_label(366) == "31 Dec"
