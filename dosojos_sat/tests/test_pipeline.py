"""Tests for fetch window arithmetic and day persistence, without any network."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from dosojos_sat import cache, pipeline
from dosojos_sat.config import Settings
from dosojos_sat.fields import Field
from dosojos_sat.indices import IndexStats
from dosojos_sat.stac import ClipOutcome
from shapely.geometry import box


# --------------------------------------------------------------------------- #
# Fetch window
# --------------------------------------------------------------------------- #


def test_years_covers_whole_calendar_years_up_to_today() -> None:
    """--years 5 in 2026 starts at 2022-01-01: four history years plus this season."""
    start, end = pipeline.season_range(5, today=date(2026, 9, 9))
    assert start == date(2022, 1, 1)
    assert end == date(2026, 9, 9)


def test_one_year_is_the_current_season_only() -> None:
    """--years 1 fetches this calendar year and nothing earlier."""
    start, _ = pipeline.season_range(1, today=date(2026, 9, 9))
    assert start == date(2026, 1, 1)


def test_window_start_is_january_first_not_a_rolling_date() -> None:
    """A rolling five-year window would pull in a partial year the baseline drops."""
    start, _ = pipeline.season_range(5, today=date(2026, 3, 2))
    assert start == date(2022, 1, 1)


@pytest.mark.parametrize("years", [0, -1])
def test_non_positive_years_rejected(years: int) -> None:
    """Asking for no years is a mistake worth naming."""
    with pytest.raises(ValueError, match="at least 1"):
        pipeline.season_range(years)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


@pytest.fixture()
def env(tmp_path: Path) -> tuple[sqlite3.Connection, Field, Settings]:
    """A cache holding one field, plus matching settings."""
    settings = Settings.from_root(tmp_path)
    settings.ensure_dirs()
    conn = cache.connect(settings.db_path)
    cache.init_schema(conn)
    field = Field(
        field_id="f1", name="Test", crop="citrus",
        geometry=box(-97.98, 26.20, -97.97, 26.21),
        utm_epsg=32614, acres_computed=10.0,
    )
    cache.upsert_fields(conn, [field])
    return conn, field, settings


def _outcome(status: str) -> ClipOutcome:
    """A ClipOutcome with no attached raster."""
    return ClipOutcome(
        field_id="f1", solar_date=date(2026, 6, 1), scene_ids=("S2A_A", "S2A_B"),
        status=status, reason="" if status == "kept" else "too cloudy",
        valid_fraction=0.95 if status == "kept" else 0.2,
        n_valid_px=95, n_total_px=100, duration_ms=1234,
    )


def _stats() -> IndexStats:
    """Filled statistics for a synthetic observation."""
    return IndexStats(
        mean=0.5, median=0.5, p10=0.1, p25=0.2, p75=0.8, p90=0.9,
        std=0.05, valid_fraction=0.95, n_valid_px=95, n_total_px=100,
    )


def test_kept_day_writes_one_row_per_index(
    env: tuple[sqlite3.Connection, Field, Settings]
) -> None:
    """Three indices from one day become three observations and one log entry."""
    conn, field, settings = env
    result = pipeline.DayResult(
        _outcome("kept"), {name: _stats() for name in ("NDVI", "NDMI", "NDWI")}
    )
    assert pipeline.persist_day(conn, field, result, settings) == 3
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM fetch_log").fetchone()["c"] == 1


def test_dropped_day_is_logged_but_stores_no_observation(
    env: tuple[sqlite3.Connection, Field, Settings]
) -> None:
    """A cloudy day leaves a trace so it is never re-read, but no measurement."""
    conn, field, settings = env
    assert pipeline.persist_day(conn, field, pipeline.DayResult(_outcome("dropped")), settings) == 0
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 0

    row = conn.execute("SELECT status, reason, scene_id FROM fetch_log").fetchone()
    assert row["status"] == "dropped"
    assert row["reason"] == "too cloudy"
    assert row["scene_id"] == "S2A_A+S2A_B"      # merged solar day keeps both ids


def test_merged_scene_ids_are_stored_together(
    env: tuple[sqlite3.Connection, Field, Settings]
) -> None:
    """An observation fused from two tiles records both source scenes."""
    conn, field, settings = env
    pipeline.persist_day(conn, field, pipeline.DayResult(_outcome("kept"), {"NDVI": _stats()}), settings)
    assert conn.execute("SELECT scene_id FROM observations").fetchone()["scene_id"] == "S2A_A+S2A_B"


def test_clip_path_is_stored_relative_to_the_project(
    env: tuple[sqlite3.Connection, Field, Settings]
) -> None:
    """Relative paths keep the cache portable between machines."""
    conn, field, settings = env
    clip = settings.clip_path("f1", "2026-06-01")
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"")
    result = pipeline.DayResult(_outcome("kept"), {"NDVI": _stats()}, clip)

    pipeline.persist_day(conn, field, result, settings)
    stored = conn.execute("SELECT clip_path FROM observations").fetchone()["clip_path"]
    assert not Path(stored).is_absolute()
    assert stored.endswith("2026-06-01.tif")
