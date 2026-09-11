"""Tests for chart rendering: files land, and the legibility rules hold."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from dosojos_sat import charts
from dosojos_sat.baseline import BaselineParams, build_baseline


@pytest.fixture()
def baseline() -> pd.DataFrame:
    """A well-supported climatology built from four synthetic years."""
    rows = [
        (2022 + year, doy, 0.5)
        for year in range(4)
        for doy in range(1, 366, 5)
    ]
    history = pd.DataFrame(rows, columns=["year", "doy", "median"])
    return build_baseline(history, BaselineParams(season=2026))


@pytest.fixture()
def season() -> pd.DataFrame:
    """A short current season sitting below the baseline."""
    return pd.DataFrame(
        {
            "date": [date(2026, 7, 1), date(2026, 7, 11), date(2026, 7, 21)],
            "median": [0.40, 0.38, 0.36],
        }
    )


def _plot(tmp_path: Path, baseline: pd.DataFrame, season: pd.DataFrame, **kwargs) -> Path:
    """Render a chart with sensible defaults for the fixtures."""
    return charts.plot_field(
        field_id="f1", name="Test Field", crop="citrus", index_name="NDVI",
        baseline=baseline, season=season, year=2026, out_dir=tmp_path,
        history_years=(2022, 2025), **kwargs,
    )


def test_chart_is_written(tmp_path: Path, baseline, season) -> None:
    """A PNG lands at the expected path with real content in it."""
    path = _plot(tmp_path, baseline, season)
    assert path == charts.chart_path(tmp_path, "f1", "NDVI")
    assert path.exists()
    assert path.stat().st_size > 10_000          # a real plot, not a blank canvas
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_output_directory_is_created(tmp_path: Path, baseline, season) -> None:
    """Charting into a fresh directory must not require it to exist first."""
    target = tmp_path / "nested" / "out"
    assert _plot(target, baseline, season).exists()


def test_chart_renders_with_no_season_data(tmp_path: Path, baseline) -> None:
    """A field with nothing this season still gets its baseline drawn."""
    empty = pd.DataFrame({"date": [], "median": []})
    assert _plot(tmp_path, baseline, empty).exists()


def test_flagged_and_run_annotations_render(tmp_path: Path, baseline, season) -> None:
    """The verdict caption and run shading must not break rendering."""
    path = _plot(
        tmp_path, baseline, season,
        flagged_dates={date(2026, 7, 21)},
        verdict="FLAGGED  -  score 74/100  -  water stress  (sustained shortfall)",
        run_start=date(2026, 7, 1),
    )
    assert path.stat().st_size > 10_000


def test_fonts_stay_large_enough_to_read_from_a_slide() -> None:
    """These go in a pitch deck, so nothing may quietly shrink below 12pt."""
    assert min(charts.FONT_SIZES.values()) >= 12
    assert charts.FONT_SIZES["title"] >= 18


def test_leap_year_baseline_is_trimmed_for_a_common_year(baseline) -> None:
    """Day 366 must not be plotted into a 365-day year."""
    dates_2026, *_ = charts._baseline_series(baseline, 2026)
    dates_2028, *_ = charts._baseline_series(baseline, 2028)
    assert len(dates_2026) == 365
    assert len(dates_2028) == 366
    assert max(dates_2026) == date(2026, 12, 31)


def test_a_past_date_is_marked_and_later_points_faded(tmp_path: Path, baseline, season) -> None:
    """Judging a season as it stood on a drone flight's date must not break rendering."""
    path = _plot(tmp_path, baseline, season, cutoff=date(2026, 7, 11))
    assert path.exists() and path.stat().st_size > 10_000


def test_public_data_charts_carry_a_banner(tmp_path: Path, baseline, season) -> None:
    path = _plot(tmp_path, baseline, season, banner="FREE PUBLIC DATA, NOT OUR FIELD")
    assert path.exists() and path.stat().st_size > 10_000
