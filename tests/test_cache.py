"""Tests for cache schema behaviour, idempotency and the resume logic."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from dosojos_sat import cache
from dosojos_sat.indices import IndexStats


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """An initialised cache holding one field."""
    connection = cache.connect(tmp_path / "test.sqlite")
    cache.init_schema(connection)
    connection.execute(
        """
        INSERT INTO fields (field_id, name, crop, acres_computed, utm_epsg,
                            centroid_lon, centroid_lat, geometry_wkt, geom_hash, updated_at)
        VALUES ('f1', 'Test', 'citrus', 10.0, 32614, -97.9, 26.2, 'POLYGON EMPTY', 'h', 'now')
        """
    )
    yield connection
    connection.close()


def _stats(median: float = 0.5) -> IndexStats:
    """A filled IndexStats with a chosen median."""
    return IndexStats(
        mean=median, median=median, p10=0.1, p25=0.2, p75=0.8, p90=0.9,
        std=0.05, valid_fraction=0.95, n_valid_px=95, n_total_px=100,
    )


def _record(median: float = 0.5, index_name: str = "NDVI") -> cache.ObservationRecord:
    """An observation for field f1 on a fixed date."""
    return cache.ObservationRecord(
        field_id="f1",
        obs_date=date(2026, 6, 1),
        index_name=index_name,
        stats=_stats(median),
        scene_id="S2A_TEST",
    )


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_second_write_is_ignored_without_force(conn: sqlite3.Connection) -> None:
    """Re-fetching the same day must not change a stored observation."""
    assert cache.upsert_observation(conn, _record(0.5)) is True
    assert cache.upsert_observation(conn, _record(0.9)) is False

    stored = conn.execute("SELECT median FROM observations").fetchone()["median"]
    assert stored == pytest.approx(0.5)


def test_force_overwrites_the_existing_row(conn: sqlite3.Connection) -> None:
    """--force replaces the summary rather than inserting a duplicate."""
    cache.upsert_observation(conn, _record(0.5))
    assert cache.upsert_observation(conn, _record(0.9), force=True) is True

    rows = conn.execute("SELECT median FROM observations").fetchall()
    assert len(rows) == 1
    assert rows[0]["median"] == pytest.approx(0.9)


def test_indices_of_one_day_coexist(conn: sqlite3.Connection) -> None:
    """The unique key is per index, so all three fit on the same date."""
    for name in ("NDVI", "NDMI", "NDWI"):
        cache.upsert_observation(conn, _record(index_name=name))
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 3


def test_doy_and_year_are_derived_from_the_date(conn: sqlite3.Connection) -> None:
    """The climatology groups on doy, so it must be stored correctly."""
    cache.upsert_observation(conn, _record())
    row = conn.execute("SELECT doy, year FROM observations").fetchone()
    assert (row["doy"], row["year"]) == (152, 2026)   # 1 June 2026


# --------------------------------------------------------------------------- #
# Resume logic
# --------------------------------------------------------------------------- #


def _log(conn: sqlite3.Connection, day: date, status: str) -> None:
    """Append a fetch_log row for a day."""
    cache.log_fetch(
        conn, field_id="f1", obs_date=day, scene_id="S2A_TEST",
        valid_fraction=0.9, status=status, reason="", duration_ms=10,
    )


def test_dropped_days_are_remembered_so_reruns_are_cheap(conn: sqlite3.Connection) -> None:
    """A cloudy day rejected once must not be re-read on the next run."""
    _log(conn, date(2026, 6, 1), "kept")
    _log(conn, date(2026, 6, 4), "dropped")

    attempted = cache.attempted_dates(conn, "f1")
    assert attempted == {date(2026, 6, 1): "kept", date(2026, 6, 4): "dropped"}


def test_errors_are_not_treated_as_resolved(conn: sqlite3.Connection) -> None:
    """A day that failed to read deserves another attempt, unlike a clean drop."""
    _log(conn, date(2026, 6, 9), "error")
    assert date(2026, 6, 9) not in cache.attempted_dates(conn, "f1")


def test_latest_attempt_wins(conn: sqlite3.Connection) -> None:
    """A day retried after an error reports its final state, not its first."""
    _log(conn, date(2026, 6, 9), "dropped")
    _log(conn, date(2026, 6, 9), "kept")
    assert cache.attempted_dates(conn, "f1")[date(2026, 6, 9)] == "kept"

    summary = cache.fetch_log_summary(conn)
    assert summary["n"].sum() == 1                     # one day, not two attempts
    assert summary.iloc[0]["status"] == "kept"


def test_attempted_dates_are_scoped_to_one_field(conn: sqlite3.Connection) -> None:
    """One field's history must never suppress another field's fetch."""
    _log(conn, date(2026, 6, 1), "kept")
    assert cache.attempted_dates(conn, "other-field") == {}


# --------------------------------------------------------------------------- #
# Reading back
# --------------------------------------------------------------------------- #


def test_observations_come_back_ordered_with_doy(conn: sqlite3.Connection) -> None:
    """The baseline needs dates in order with their day-of-year attached."""
    for day, median in [(date(2026, 7, 1), 0.7), (date(2026, 6, 1), 0.5)]:
        cache.upsert_observation(
            conn,
            cache.ObservationRecord(
                field_id="f1", obs_date=day, index_name="NDVI",
                stats=_stats(median), scene_id="S2A_TEST",
            ),
        )
    frame = cache.get_observations(conn, "f1", "NDVI")
    assert list(frame["date"]) == [date(2026, 6, 1), date(2026, 7, 1)]
    assert list(frame["doy"]) == [152, 182]
    assert list(frame["median"]) == pytest.approx([0.5, 0.7])


def test_year_range_filters_history(conn: sqlite3.Connection) -> None:
    """Baseline history is selected by year, excluding the current season."""
    for year in (2023, 2024, 2026):
        cache.upsert_observation(
            conn,
            cache.ObservationRecord(
                field_id="f1", obs_date=date(year, 6, 1), index_name="NDVI",
                stats=_stats(), scene_id="S2A_TEST",
            ),
        )
    history = cache.get_observations(conn, "f1", "NDVI", year_range=(2023, 2025))
    assert list(history["year"]) == [2023, 2024]


def test_empty_result_is_an_empty_frame(conn: sqlite3.Connection) -> None:
    """A field with no observations reads back cleanly rather than raising."""
    assert cache.get_observations(conn, "f1", "NDVI").empty


# --------------------------------------------------------------------------- #
# Gaps
# --------------------------------------------------------------------------- #


def test_gaps_report_long_stretches_only(conn: sqlite3.Connection) -> None:
    """Normal 5-day revisit spacing is not a gap; a cloudy season is."""
    for day in [date(2026, 1, 1), date(2026, 1, 6), date(2026, 3, 1), date(2026, 3, 6)]:
        cache.upsert_observation(
            conn,
            cache.ObservationRecord(
                field_id="f1", obs_date=day, index_name="NDVI",
                stats=_stats(), scene_id="S2A_TEST",
            ),
        )
    gaps = cache.observation_gaps(conn, min_days=30)
    assert len(gaps) == 1
    assert gaps.iloc[0]["gap_start"] == date(2026, 1, 6)
    assert gaps.iloc[0]["gap_end"] == date(2026, 3, 1)
    assert gaps.iloc[0]["days"] == 54


def _observation(conn: sqlite3.Connection, day: date, median: float) -> None:
    """Store one NDVI observation with a chosen median."""
    cache.upsert_observation(
        conn,
        cache.ObservationRecord(
            field_id="f1", obs_date=day, index_name="NDVI",
            stats=_stats(median), scene_id="S2A_TEST",
        ),
    )


def test_pinned_indices_are_flagged(conn: sqlite3.Connection) -> None:
    """Mostly-saturated medians mean mis-scaled reflectance, not a lush field.

    Regression for the run where the BOA offset was applied twice: every stored
    value still looked like a valid index, and only the pile-up at 1.0 gave it away.
    """
    for day in range(1, 11):
        _observation(conn, date(2026, 6, day), 1.0 if day <= 6 else 0.5)
    report = cache.saturation_report(conn)
    assert len(report) == 1
    assert report.iloc[0]["n_pinned"] == 6
    assert report.iloc[0]["pinned_fraction"] == pytest.approx(0.6)


def test_healthy_indices_are_not_flagged(conn: sqlite3.Connection) -> None:
    """One saturated observation in twenty is ordinary and stays quiet."""
    for day in range(1, 21):
        _observation(conn, date(2026, 6, day), 1.0 if day == 1 else 0.5)
    assert cache.saturation_report(conn).empty


def test_no_observations_gives_an_empty_gap_frame(conn: sqlite3.Connection) -> None:
    """An unfetched cache reports no gaps rather than raising on an empty frame."""
    result = cache.observation_gaps(conn)
    assert result.empty
    assert list(result.columns) == ["field_id", "gap_start", "gap_end", "days"]


# --------------------------------------------------------------------------- #
# Version 2: water settings, weather and soils
# --------------------------------------------------------------------------- #


def test_a_version_1_cache_upgrades_in_place(tmp_path: Path) -> None:
    """Existing caches gain the water columns and tables without losing a row."""
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE fields (
            field_id TEXT PRIMARY KEY, name TEXT NOT NULL, crop TEXT NOT NULL,
            acres_declared REAL, acres_computed REAL NOT NULL, utm_epsg INTEGER NOT NULL,
            centroid_lon REAL NOT NULL, centroid_lat REAL NOT NULL,
            geometry_wkt TEXT NOT NULL, geom_hash TEXT NOT NULL, source_file TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT INTO fields VALUES ('f1', 'Old', 'cane', NULL, 5, 32614, -97.9, 26.2,
                                   'POINT (0 0)', 'h', NULL, 'then');
        """
    )
    old.close()

    with cache.session(path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(fields)")}
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        field = cache.get_fields(conn)[0]
    assert {"irrigation", "water_enters", "soil_awc_in_ft"} <= columns
    assert version["value"] == str(cache.SCHEMA_VERSION)
    assert field.name == "Old" and field.irrigation is None


def test_a_station_reading_beats_gridmet_on_the_same_day(conn: sqlite3.Connection) -> None:
    import pandas as pd

    days = [date(2026, 7, 1), date(2026, 7, 2)]
    cache.upsert_weather(conn, "f1", pd.DataFrame({"date": days, "eto_mm": [7.0, 7.5],
                                                   "rain_mm": [0.0, 0.0],
                                                   "tmax_c": [36.0, 37.0],
                                                   "tmin_c": [24.0, 25.0]}), "gridmet")
    cache.upsert_weather(conn, "f1", pd.DataFrame({"date": days[:1], "eto_mm": [6.2],
                                                   "rain_mm": [3.0]}), "station:mcallen.csv")
    frame = cache.get_weather(conn, "f1", days[0], days[1])
    assert frame["eto_mm"].tolist() == [6.2, 7.5]
    assert frame["source"].tolist() == ["station:mcallen.csv", "gridmet"]
    assert cache.weather_dates(conn, "f1", "gridmet") == set(days)
    # The station file has no temperatures, so its day borrows gridMET's.
    assert frame["tmax_c"].tolist() == [36.0, 37.0]


def test_gridmet_days_without_temperatures_are_fetched_again(conn: sqlite3.Connection) -> None:
    """A cache filled before highs and lows were kept must fill them in, not skip the days."""
    import pandas as pd

    days = [date(2026, 7, 1), date(2026, 7, 2)]
    cache.upsert_weather(conn, "f1", pd.DataFrame({"date": days, "eto_mm": [7.0, 7.5],
                                                   "rain_mm": [0.0, 0.0]}), "gridmet")
    assert cache.weather_dates(conn, "f1", "gridmet") == set()
    cache.upsert_weather(conn, "f1", pd.DataFrame({"date": days, "eto_mm": [7.0, 7.5],
                                                   "rain_mm": [0.0, 0.0],
                                                   "tmax_c": [36.0, 37.0],
                                                   "tmin_c": [24.0, 25.0]}), "gridmet")
    assert cache.weather_dates(conn, "f1", "gridmet") == set(days)


def test_a_refetch_without_temperatures_keeps_the_ones_stored(conn: sqlite3.Connection) -> None:
    """If gridMET's temperature service is down one day, yesterday's highs survive."""
    import pandas as pd

    day = [date(2026, 7, 1)]
    cache.upsert_weather(conn, "f1", pd.DataFrame({"date": day, "eto_mm": [7.0], "rain_mm": [0.0],
                                                   "tmax_c": [36.0], "tmin_c": [24.0]}),
                         "gridmet")
    cache.upsert_weather(conn, "f1", pd.DataFrame({"date": day, "eto_mm": [7.1], "rain_mm": [0.0],
                                                   "tmax_c": [float("nan")],
                                                   "tmin_c": [float("nan")]}), "gridmet")
    frame = cache.get_weather(conn, "f1", day[0], day[0])
    assert frame["eto_mm"].tolist() == [7.1] and frame["tmax_c"].tolist() == [36.0]


def test_a_soil_profile_is_stored_with_its_outline(conn: sqlite3.Connection) -> None:
    cache.upsert_soil(conn, "f1", "hash-1", "ssurgo", {"name": "Mercedes clay"})
    stored = cache.get_soil(conn, "f1")
    assert stored["geom_hash"] == "hash-1" and stored["profile"]["name"] == "Mercedes clay"
    assert cache.get_soil(conn, "nobody") is None
