"""SQLite cache: schema definition and read/write helpers.

Everything the tool needs for an offline run lives here. Per-pixel arrays never
enter the database; clipped rasters go to disk and are referenced by path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Literal, Sequence

import numpy as np
import pandas as pd
import rasterio
from shapely import wkt

from .config import BAND_ASSETS
from .fields import Field
from .indices import IndexStats

if TYPE_CHECKING:  # type-only, so the offline path never imports odc-stac
    from .baseline import BaselineRun
    from .stac import Clip, SceneRef

log = logging.getLogger(__name__)

#: 2 added the field water settings and the weather and soils tables.
SCHEMA_VERSION = 3

#: Columns version 2 added to ``fields``; older caches gain them in place.
_FIELD_COLUMNS_V2 = (
    ("irrigation", "TEXT"),
    ("water_enters", "TEXT"),
    ("soil_awc_in_ft", "REAL"),
)
#: Daily highs and lows, added in version 3 for heat units and growth stages.
_WEATHER_COLUMNS_V3 = (
    ("tmax_c", "REAL"),
    ("tmin_c", "REAL"),
)

FieldSyncStatus = Literal["inserted", "unchanged", "metadata-updated", "geometry-changed"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS fields (
    field_id       TEXT PRIMARY KEY,
    name           TEXT    NOT NULL,
    crop           TEXT    NOT NULL,
    acres_declared REAL,
    acres_computed REAL    NOT NULL,
    utm_epsg       INTEGER NOT NULL,
    centroid_lon   REAL    NOT NULL,
    centroid_lat   REAL    NOT NULL,
    geometry_wkt   TEXT    NOT NULL,
    geom_hash      TEXT    NOT NULL,
    source_file    TEXT,
    updated_at     TEXT    NOT NULL,
    irrigation     TEXT,                      -- furrow | flood | ... | none
    water_enters   TEXT,                      -- N | S | E | W
    soil_awc_in_ft REAL                       -- overrides the soil survey
);

CREATE TABLE IF NOT EXISTS scenes (
    scene_id            TEXT PRIMARY KEY,
    solar_date          TEXT NOT NULL,
    datetime_utc        TEXT NOT NULL,
    platform            TEXT,
    mgrs_tile           TEXT,
    epsg                INTEGER,
    cloud_cover         REAL,
    processing_baseline TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    field_id       TEXT    NOT NULL REFERENCES fields(field_id) ON DELETE CASCADE,
    date           TEXT    NOT NULL,          -- solar day, YYYY-MM-DD
    index_name     TEXT    NOT NULL,          -- NDVI | NDMI | NDWI
    mean           REAL,
    median         REAL,
    p10            REAL,
    p90            REAL,
    std            REAL,
    valid_fraction REAL    NOT NULL,
    scene_id       TEXT    NOT NULL,          -- '+'-joined when a day merges >1 item
    p25            REAL,
    p75            REAL,
    n_valid_px     INTEGER NOT NULL,
    n_total_px     INTEGER NOT NULL,
    doy            INTEGER NOT NULL,
    year           INTEGER NOT NULL,
    clip_path      TEXT,
    fetched_at     TEXT    NOT NULL,
    PRIMARY KEY (field_id, date, index_name)
);

CREATE INDEX IF NOT EXISTS ix_obs_baseline ON observations(field_id, index_name, doy);
CREATE INDEX IF NOT EXISTS ix_obs_season   ON observations(field_id, index_name, year);

CREATE TABLE IF NOT EXISTS fetch_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    field_id       TEXT NOT NULL,
    date           TEXT NOT NULL,
    scene_id       TEXT,
    valid_fraction REAL,
    status         TEXT NOT NULL,             -- kept | dropped | cached | error
    reason         TEXT,
    duration_ms    INTEGER
);

CREATE INDEX IF NOT EXISTS ix_log_field ON fetch_log(field_id, date);

CREATE TABLE IF NOT EXISTS baseline_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    index_name    TEXT    NOT NULL,
    season        INTEGER NOT NULL,
    history_years INTEGER NOT NULL,
    year_min      INTEGER,
    year_max      INTEGER,
    doy_window    INTEGER NOT NULL,
    smooth_window INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS baseline_doy (
    field_id     TEXT    NOT NULL REFERENCES fields(field_id) ON DELETE CASCADE,
    index_name   TEXT    NOT NULL,
    doy          INTEGER NOT NULL,
    median       REAL,
    p10          REAL,
    p25          REAL,
    p75          REAL,
    p90          REAL,
    n_obs        INTEGER NOT NULL,
    n_years      INTEGER NOT NULL,
    interpolated INTEGER NOT NULL DEFAULT 0,
    confidence   TEXT    NOT NULL,            -- high | medium | low | none
    run_id       INTEGER NOT NULL REFERENCES baseline_runs(run_id),
    PRIMARY KEY (field_id, index_name, doy)
);

CREATE TABLE IF NOT EXISTS weather (
    field_id   TEXT NOT NULL REFERENCES fields(field_id) ON DELETE CASCADE,
    date       TEXT NOT NULL,
    source     TEXT NOT NULL,                 -- gridmet | station:<file name>
    eto_mm     REAL,                          -- short-grass reference evapotranspiration
    rain_mm    REAL,
    tmax_c     REAL,                          -- daily high, for heat units
    tmin_c     REAL,                          -- daily low
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (field_id, date, source)
);

CREATE TABLE IF NOT EXISTS soils (
    field_id     TEXT PRIMARY KEY REFERENCES fields(field_id) ON DELETE CASCADE,
    geom_hash    TEXT NOT NULL,               -- the outline the survey was read for
    source       TEXT NOT NULL,               -- ssurgo | manual
    profile_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# Connection handling
# --------------------------------------------------------------------------- #


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the cache database, creating its parent directory if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL allows one writer alongside many readers; waiting beats failing when a
    # long fetch happens to hold the write lock.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def session(db_path: Path, *, init: bool = True) -> Iterator[sqlite3.Connection]:
    """Yield a cache connection, committing on success and always closing."""
    conn = connect(db_path)
    if init:
        init_schema(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index if absent, and stamp the schema version.

    Returns without writing anything when the schema is already current, so a
    read-only command such as ``status`` never takes a write lock just to start up.
    """
    if _schema_is_current(conn):
        return
    conn.executescript(_SCHEMA)
    _add_missing_columns(conn, "fields", _FIELD_COLUMNS_V2)
    _add_missing_columns(conn, "weather", _WEATHER_COLUMNS_V3)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('created_at', ?) ON CONFLICT(key) DO NOTHING",
        (_now(),),
    )
    conn.commit()


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """True when the database already reports the schema version we expect."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False  # the meta table does not exist yet
    return row is not None and row["value"] == str(SCHEMA_VERSION)


def _add_missing_columns(
    conn: sqlite3.Connection, table: str, columns: Sequence[tuple[str, str]]
) -> None:
    """Add columns a newer schema defines, so an older cache upgrades in place.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, which would
    leave a version 1 cache without the columns version 2 reads.
    """
    present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, kind in columns:
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
            log.info("cache upgraded: %s.%s added", table, name)


def _now() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #


def upsert_fields(
    conn: sqlite3.Connection,
    fields: Sequence[Field],
    *,
    source_file: str | None = None,
) -> list[tuple[str, FieldSyncStatus]]:
    """Insert or update field rows, reporting what changed for each one.

    A ``geometry-changed`` result means the polygon was redrawn since the last
    run, so any observations cached against the old outline no longer describe
    the same ground.
    """
    results: list[tuple[str, FieldSyncStatus]] = []
    for field in fields:
        existing = conn.execute(
            "SELECT name, crop, acres_declared, geom_hash, irrigation, water_enters, "
            "soil_awc_in_ft FROM fields WHERE field_id = ?",
            (field.field_id,),
        ).fetchone()
        status = _field_status(field, existing)
        if status != "unchanged":
            _write_field(conn, field, source_file)
        results.append((field.field_id, status))
    return results


def _field_status(field: Field, existing: sqlite3.Row | None) -> FieldSyncStatus:
    """Classify a field against its cached row."""
    if existing is None:
        return "inserted"
    if existing["geom_hash"] != field.geom_hash:
        return "geometry-changed"
    metadata = (
        existing["name"], existing["crop"], existing["acres_declared"],
        existing["irrigation"], existing["water_enters"], existing["soil_awc_in_ft"],
    )
    if metadata != (
        field.name, field.crop, field.acres_declared,
        field.irrigation, field.water_enters, field.soil_awc_in_ft,
    ):
        return "metadata-updated"
    return "unchanged"


def _write_field(conn: sqlite3.Connection, field: Field, source_file: str | None) -> None:
    """Insert or replace one field row."""
    lon, lat = field.centroid_lonlat
    conn.execute(
        """
        INSERT INTO fields (field_id, name, crop, acres_declared, acres_computed,
                            utm_epsg, centroid_lon, centroid_lat, geometry_wkt,
                            geom_hash, source_file, updated_at,
                            irrigation, water_enters, soil_awc_in_ft)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(field_id) DO UPDATE SET
            irrigation     = excluded.irrigation,
            water_enters   = excluded.water_enters,
            soil_awc_in_ft = excluded.soil_awc_in_ft,
            name           = excluded.name,
            crop           = excluded.crop,
            acres_declared = excluded.acres_declared,
            acres_computed = excluded.acres_computed,
            utm_epsg       = excluded.utm_epsg,
            centroid_lon   = excluded.centroid_lon,
            centroid_lat   = excluded.centroid_lat,
            geometry_wkt   = excluded.geometry_wkt,
            geom_hash      = excluded.geom_hash,
            source_file    = excluded.source_file,
            updated_at     = excluded.updated_at
        """,
        (
            field.field_id,
            field.name,
            field.crop,
            field.acres_declared,
            field.acres_computed,
            field.utm_epsg,
            lon,
            lat,
            field.geometry_wkt,
            field.geom_hash,
            source_file,
            _now(),
            field.irrigation,
            field.water_enters,
            field.soil_awc_in_ft,
        ),
    )


def get_fields(
    conn: sqlite3.Connection, ids: Sequence[str] | None = None
) -> list[Field]:
    """Load cached fields, optionally restricted to ``ids``, ordered by id.

    Raises:
        KeyError: if any requested id is not in the cache.
    """
    if ids is None:
        rows = conn.execute("SELECT * FROM fields ORDER BY field_id").fetchall()
    else:
        ids = list(ids)
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM fields WHERE field_id IN ({placeholders}) ORDER BY field_id",
            ids,
        ).fetchall()
        missing = set(ids) - {row["field_id"] for row in rows}
        if missing:
            raise KeyError(
                f"unknown field id(s): {', '.join(sorted(missing))}. "
                "Run 'dosojos-sat init-fields' first."
            )
    return [_row_to_field(row) for row in rows]


def _row_to_field(row: sqlite3.Row) -> Field:
    """Rebuild a :class:`Field` from its cached row."""
    return Field(
        field_id=row["field_id"],
        name=row["name"],
        crop=row["crop"],
        geometry=wkt.loads(row["geometry_wkt"]),
        utm_epsg=row["utm_epsg"],
        acres_computed=row["acres_computed"],
        acres_declared=row["acres_declared"],
        irrigation=row["irrigation"],
        water_enters=row["water_enters"],
        soil_awc_in_ft=row["soil_awc_in_ft"],
    )


# --------------------------------------------------------------------------- #
# Clipped rasters on disk
# --------------------------------------------------------------------------- #

#: Band order inside a cached clip; SCL last so the reflectance bands stay 1-4.
CLIP_BANDS: tuple[str, ...] = tuple(b for b in BAND_ASSETS if b != "SCL") + ("SCL",)


def write_clip_geotiff(clip: "Clip", path: Path) -> Path:
    """Write one day's clipped bands to a small compressed GeoTIFF.

    Per-pixel arrays stay out of the database, but keeping them on disk means any
    index can be recomputed later, and a suspicious observation can be inspected,
    without going back to AWS.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = clip.scl.shape
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": len(CLIP_BANDS),
        "dtype": "float32",
        "crs": f"EPSG:{clip.epsg}",
        "transform": clip.transform,
        "nodata": float("nan"),
        "compress": "deflate",
        "predictor": 3,          # floating-point predictor
        "zlevel": 6,
    }
    with rasterio.open(path, "w", **profile) as dst:
        for position, name in enumerate(CLIP_BANDS, start=1):
            data = clip.scl if name == "SCL" else clip.bands[name]
            dst.write(np.asarray(data, dtype=np.float32), position)
            dst.set_band_description(position, name)
        dst.update_tags(
            field_id=clip.field_id,
            solar_date=clip.solar_date.isoformat(),
            scene_ids="+".join(clip.scene_ids),
        )
    return path


# --------------------------------------------------------------------------- #
# Scenes
# --------------------------------------------------------------------------- #


def upsert_scenes(conn: sqlite3.Connection, scenes: Sequence["SceneRef"]) -> None:
    """Record scene provenance, ignoring scenes already present."""
    conn.executemany(
        """
        INSERT INTO scenes (scene_id, solar_date, datetime_utc, platform, mgrs_tile,
                            epsg, cloud_cover, processing_baseline)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scene_id) DO NOTHING
        """,
        [
            (
                scene.scene_id,
                scene.solar_date.isoformat(),
                scene.datetime_utc.isoformat(),
                scene.platform,
                scene.mgrs_tile,
                scene.epsg,
                scene.cloud_cover,
                scene.processing_baseline,
            )
            for scene in scenes
        ],
    )


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ObservationRecord:
    """One index summary for one field on one solar day, ready to store."""

    field_id: str
    obs_date: date
    index_name: str
    stats: IndexStats
    scene_id: str
    clip_path: str | None = None


def upsert_observation(
    conn: sqlite3.Connection, record: ObservationRecord, *, force: bool = False
) -> bool:
    """Store one observation, returning True if the database actually changed.

    Without ``force`` an existing row is left untouched, which is what makes a
    repeated fetch idempotent.
    """
    conflict = _OBSERVATION_UPDATE if force else "DO NOTHING"
    stats = record.stats
    cursor = conn.execute(
        f"""
        INSERT INTO observations (field_id, date, index_name, mean, median, p10, p90,
                                  std, valid_fraction, scene_id, p25, p75,
                                  n_valid_px, n_total_px, doy, year, clip_path, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(field_id, date, index_name) {conflict}
        """,
        (
            record.field_id,
            record.obs_date.isoformat(),
            record.index_name,
            stats.mean,
            stats.median,
            stats.p10,
            stats.p90,
            stats.std,
            stats.valid_fraction,
            record.scene_id,
            stats.p25,
            stats.p75,
            stats.n_valid_px,
            stats.n_total_px,
            record.obs_date.timetuple().tm_yday,
            record.obs_date.year,
            record.clip_path,
            _now(),
        ),
    )
    return cursor.rowcount > 0


_OBSERVATION_UPDATE = """DO UPDATE SET
    mean = excluded.mean, median = excluded.median, p10 = excluded.p10,
    p90 = excluded.p90, std = excluded.std, valid_fraction = excluded.valid_fraction,
    scene_id = excluded.scene_id, p25 = excluded.p25, p75 = excluded.p75,
    n_valid_px = excluded.n_valid_px, n_total_px = excluded.n_total_px,
    clip_path = excluded.clip_path, fetched_at = excluded.fetched_at"""


def get_observations(
    conn: sqlite3.Connection,
    field_id: str,
    index_name: str,
    *,
    year_range: tuple[int, int] | None = None,
) -> pd.DataFrame:
    """Return one field's observations for an index, oldest first.

    The frame carries ``date`` as a real date plus ``doy`` and ``year``, which is
    what the climatology groups on.
    """
    sql = (
        "SELECT date, doy, year, median, mean, p10, p25, p75, p90, std, "
        "valid_fraction, scene_id FROM observations "
        "WHERE field_id = ? AND index_name = ?"
    )
    params: list[object] = [field_id, index_name]
    if year_range is not None:
        sql += " AND year BETWEEN ? AND ?"
        params.extend(year_range)
    frame = pd.read_sql_query(sql + " ORDER BY date", conn, params=params)
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


# --------------------------------------------------------------------------- #
# Fetch log
# --------------------------------------------------------------------------- #


def log_fetch(
    conn: sqlite3.Connection,
    *,
    field_id: str,
    obs_date: date,
    scene_id: str | None,
    valid_fraction: float | None,
    status: str,
    reason: str,
    duration_ms: int,
) -> None:
    """Append one audit row explaining what happened to a fetch attempt."""
    conn.execute(
        """
        INSERT INTO fetch_log (ts, field_id, date, scene_id, valid_fraction,
                               status, reason, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now(),
            field_id,
            obs_date.isoformat(),
            scene_id,
            valid_fraction,
            status,
            reason or None,
            duration_ms,
        ),
    )


def attempted_dates(conn: sqlite3.Connection, field_id: str) -> dict[date, str]:
    """Dates already resolved for a field, mapped to their latest status.

    Dropped days are remembered as well as kept ones, so a re-run does not pay to
    re-read every cloudy day it has already rejected. Errors are excluded, since
    those deserve another try.
    """
    rows = conn.execute(
        "SELECT date, status FROM fetch_log "
        "WHERE field_id = ? AND status IN ('kept', 'dropped') ORDER BY id",
        (field_id,),
    ).fetchall()
    return {date.fromisoformat(row["date"]): row["status"] for row in rows}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def coverage_report(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per field and index: observation counts, span and mean quality."""
    return pd.read_sql_query(
        """
        SELECT f.field_id, f.name, f.crop, o.index_name,
               COUNT(*)                AS n_obs,
               COUNT(DISTINCT o.year)  AS n_years,
               MIN(o.date)             AS first_date,
               MAX(o.date)             AS last_date,
               AVG(o.valid_fraction)   AS mean_valid_fraction
        FROM fields f
        JOIN observations o ON o.field_id = f.field_id
        GROUP BY f.field_id, o.index_name
        ORDER BY f.field_id, o.index_name
        """,
        conn,
    )


def observation_gaps(
    conn: sqlite3.Connection, *, index_name: str = "NDVI", min_days: int = 30
) -> pd.DataFrame:
    """Stretches with no usable observation, longest first.

    Long gaps are where a baseline goes thin, so they are worth seeing before
    trusting a deviation score that falls inside one. All three indices share a
    date, so examining one is enough.
    """
    empty = pd.DataFrame(columns=["field_id", "gap_start", "gap_end", "days"])
    frame = pd.read_sql_query(
        "SELECT field_id, date FROM observations WHERE index_name = ? ORDER BY field_id, date",
        conn,
        params=[index_name],
    )
    if frame.empty:
        return empty

    frame["date"] = pd.to_datetime(frame["date"])
    gaps: list[dict[str, object]] = []
    for field_id, group in frame.groupby("field_id"):
        dates = group["date"].sort_values().reset_index(drop=True)
        for position, delta in dates.diff().dt.days.items():
            if pd.notna(delta) and delta >= min_days:
                gaps.append(
                    {
                        "field_id": field_id,
                        "gap_start": dates[position - 1].date(),
                        "gap_end": dates[position].date(),
                        "days": int(delta),
                    }
                )
    if not gaps:
        return empty
    return pd.DataFrame(gaps).sort_values("days", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Baselines
# --------------------------------------------------------------------------- #


def upsert_baseline(
    conn: sqlite3.Connection,
    field_id: str,
    run: "BaselineRun",
    table: pd.DataFrame,
) -> int:
    """Replace the stored climatology for one field and index, returning row count.

    A baseline is cheap to recompute but caching it keeps ``score`` and ``chart``
    fast and identical between runs, which matters for an offline demo.
    """
    params = run.params
    year_min, year_max = params.year_range
    cursor = conn.execute(
        """
        INSERT INTO baseline_runs (created_at, index_name, season, history_years,
                                   year_min, year_max, doy_window, smooth_window)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run.created_at, run.index_name, params.season, params.history_years,
            year_min, year_max, params.doy_window, params.smooth_window,
        ),
    )
    run_id = int(cursor.lastrowid)

    conn.execute(
        "DELETE FROM baseline_doy WHERE field_id = ? AND index_name = ?",
        (field_id, run.index_name),
    )
    conn.executemany(
        """
        INSERT INTO baseline_doy (field_id, index_name, doy, median, p10, p25, p75,
                                  p90, n_obs, n_years, interpolated, confidence, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                field_id, run.index_name, int(row.doy),
                _none_if_nan(row.median), _none_if_nan(row.p10), _none_if_nan(row.p25),
                _none_if_nan(row.p75), _none_if_nan(row.p90),
                int(row.n_obs), int(row.n_years), int(row.interpolated),
                row.confidence, run_id,
            )
            for row in table.itertuples()
        ],
    )
    return len(table)


def _none_if_nan(value: object) -> float | None:
    """Store an absent statistic as SQL NULL rather than a NaN float."""
    number = float(value)  # type: ignore[arg-type]
    return None if pd.isna(number) else number


def get_baseline(
    conn: sqlite3.Connection, field_id: str, index_name: str
) -> pd.DataFrame:
    """Read back one field's stored climatology, ordered by day of year."""
    return pd.read_sql_query(
        """
        SELECT doy, median, p10, p25, p75, p90, n_obs, n_years,
               interpolated, confidence
        FROM baseline_doy
        WHERE field_id = ? AND index_name = ?
        ORDER BY doy
        """,
        conn,
        params=[field_id, index_name],
    )


def baseline_run_info(
    conn: sqlite3.Connection, field_id: str, index_name: str
) -> dict[str, object] | None:
    """Parameters behind the stored baseline, or None if there is not one."""
    row = conn.execute(
        """
        SELECT r.created_at, r.season, r.history_years, r.year_min, r.year_max,
               r.doy_window, r.smooth_window
        FROM baseline_doy b JOIN baseline_runs r ON r.run_id = b.run_id
        WHERE b.field_id = ? AND b.index_name = ? LIMIT 1
        """,
        (field_id, index_name),
    ).fetchone()
    return dict(row) if row is not None else None


#: A normalised index pinned at its bound means the inputs were mis-scaled.
SATURATION_LIMIT = 0.999

#: Above this share of pinned observations, suspect the scaling rather than the crop.
SATURATION_ALERT = 0.05


def saturation_report(conn: sqlite3.Connection) -> pd.DataFrame:
    """Share of observations whose median sits at +/-1, per field and index.

    A normalised difference only reaches its bound when one input band is zero.
    A few such observations are ordinary; a large share means the reflectance was
    mis-scaled, which is otherwise invisible because every stored value still
    looks like a plausible index. This is the check that caught the BOA offset
    being applied twice, so it earns a permanent place in ``status``.
    """
    return pd.read_sql_query(
        f"""
        SELECT field_id, index_name, COUNT(*) AS n_obs,
               SUM(CASE WHEN ABS(median) >= {SATURATION_LIMIT} THEN 1 ELSE 0 END) AS n_pinned,
               1.0 * SUM(CASE WHEN ABS(median) >= {SATURATION_LIMIT} THEN 1 ELSE 0 END)
                   / COUNT(*) AS pinned_fraction
        FROM observations
        GROUP BY field_id, index_name
        HAVING pinned_fraction > {SATURATION_ALERT}
        ORDER BY pinned_fraction DESC
        """,
        conn,
    )


def fetch_log_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    """Per field, how many days ended up kept, dropped or failed.

    Only the latest attempt for each day counts, so a day that failed once and
    succeeded on a retry is reported as kept rather than as both.
    """
    return pd.read_sql_query(
        """
        SELECT field_id, status, COUNT(*) AS n
        FROM (
            SELECT field_id, status,
                   ROW_NUMBER() OVER (PARTITION BY field_id, date ORDER BY id DESC) AS rn
            FROM fetch_log
        )
        WHERE rn = 1
        GROUP BY field_id, status ORDER BY field_id, status
        """,
        conn,
    )


# --------------------------------------------------------------------------- #
# Weather and soils, the water checkbook's inputs
# --------------------------------------------------------------------------- #


def upsert_weather(
    conn: sqlite3.Connection, field_id: str, frame: pd.DataFrame, source: str
) -> int:
    """Store daily ``eto_mm`` and ``rain_mm``, and highs and lows where given.

    Re-fetched days overwrite, since gridMET revises its most recent week. A frame
    without temperatures (most station files) leaves any stored ones alone.
    """
    now = _now()
    empty = pd.Series([float("nan")] * len(frame), index=frame.index)
    tmax = frame["tmax_c"] if "tmax_c" in frame else empty
    tmin = frame["tmin_c"] if "tmin_c" in frame else empty
    rows = [
        (field_id, day.isoformat(), source, _none_if_nan(eto), _none_if_nan(rain),
         _none_if_nan(high), _none_if_nan(low), now)
        for day, eto, rain, high, low in zip(frame["date"], frame["eto_mm"],
                                             frame["rain_mm"], tmax, tmin)
    ]
    conn.executemany(
        """
        INSERT INTO weather (field_id, date, source, eto_mm, rain_mm, tmax_c, tmin_c,
                             fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(field_id, date, source) DO UPDATE SET
            eto_mm = excluded.eto_mm, rain_mm = excluded.rain_mm,
            tmax_c = COALESCE(excluded.tmax_c, weather.tmax_c),
            tmin_c = COALESCE(excluded.tmin_c, weather.tmin_c),
            fetched_at = excluded.fetched_at
        """,
        rows,
    )
    return len(rows)


def weather_dates(conn: sqlite3.Connection, field_id: str, source: str) -> set[date]:
    """Days already stored for one field and source.

    For gridMET a day counts only once it has its highs and lows too, so a cache
    fetched before temperatures were kept fills them in on the next ``weather``.
    """
    temps = " AND tmax_c IS NOT NULL AND tmin_c IS NOT NULL" if source == "gridmet" else ""
    rows = conn.execute(
        "SELECT date FROM weather WHERE field_id = ? AND source = ? AND eto_mm IS NOT NULL"
        + temps,
        (field_id, source),
    ).fetchall()
    return {date.fromisoformat(row["date"]) for row in rows}


def get_weather(
    conn: sqlite3.Connection, field_id: str, start: date, end: date
) -> pd.DataFrame:
    """One field's daily weather, oldest first, one row per day.

    Where a day has both a station reading and gridMET, the station wins: it
    stands in the valley rather than averaging 4 km around it. A station file
    without temperatures still borrows gridMET's high and low for that day.
    """
    frame = pd.read_sql_query(
        """
        SELECT date, source, eto_mm, rain_mm, tmax_c, tmin_c FROM weather
        WHERE field_id = ? AND date BETWEEN ? AND ?
        ORDER BY date, CASE source WHEN 'power' THEN 2 WHEN 'gridmet' THEN 1 ELSE 0 END
        """,
        conn, params=[field_id, start.isoformat(), end.isoformat()],
    )
    if frame.empty:
        return frame
    temps = frame.groupby("date")[["tmax_c", "tmin_c"]].first()
    frame = frame.drop_duplicates("date", keep="first").reset_index(drop=True)
    frame[["tmax_c", "tmin_c"]] = temps.loc[frame["date"]].to_numpy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def weather_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    """Per field and source: first and last day, count, and totals."""
    return pd.read_sql_query(
        """
        SELECT field_id, source, MIN(date) AS first_date, MAX(date) AS last_date,
               COUNT(*) AS n_days, SUM(eto_mm) AS eto_mm, SUM(rain_mm) AS rain_mm
        FROM weather GROUP BY field_id, source ORDER BY field_id, source
        """,
        conn,
    )


def upsert_soil(
    conn: sqlite3.Connection, field_id: str, geom_hash: str, source: str, profile: dict
) -> None:
    """Store one field's soil water profile, tied to the outline it was read for."""
    conn.execute(
        """
        INSERT INTO soils (field_id, geom_hash, source, profile_json, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(field_id) DO UPDATE SET
            geom_hash = excluded.geom_hash, source = excluded.source,
            profile_json = excluded.profile_json, fetched_at = excluded.fetched_at
        """,
        (field_id, geom_hash, source, json.dumps(profile), _now()),
    )


def get_soil(conn: sqlite3.Connection, field_id: str) -> dict | None:
    """One field's stored soil profile with its ``source`` and ``geom_hash``, or None."""
    row = conn.execute(
        "SELECT geom_hash, source, profile_json, fetched_at FROM soils WHERE field_id = ?",
        (field_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "geom_hash": row["geom_hash"], "source": row["source"],
        "fetched_at": row["fetched_at"], "profile": json.loads(row["profile_json"]),
    }
