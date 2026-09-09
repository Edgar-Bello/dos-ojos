"""Fetch orchestration: search, read, summarise and store, one field at a time.

Reads run on a small thread pool because they are almost entirely waiting on
HTTP. Database writes stay on the calling thread, since a SQLite connection
belongs to the thread that opened it.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field as dc_field
from datetime import date
from pathlib import Path
from typing import Sequence

from pystac_client import Client

from . import cache, indices, stac
from .config import INDEX_NAMES, Settings
from .fields import Field
from .indices import IndexStats
from .stac import ClipOutcome, SceneRef

log = logging.getLogger(__name__)

DEFAULT_WORKERS = 6


@dataclass(frozen=True)
class DayResult:
    """Everything one solar day produced, ready to be written to the cache."""

    outcome: ClipOutcome
    stats: dict[str, IndexStats] = dc_field(default_factory=dict)
    clip_path: Path | None = None


@dataclass
class FetchSummary:
    """What a fetch did for one field."""

    field_id: str
    days_available: int = 0
    days_skipped: int = 0
    days_read: int = 0
    kept: int = 0
    dropped: int = 0
    errors: int = 0
    observations_written: int = 0
    elapsed_s: float = 0.0


def season_range(years: int, today: date | None = None) -> tuple[date, date]:
    """Return the fetch window covering ``years`` calendar years up to today.

    ``years=5`` in 2026 gives 2022-01-01 to today: four complete history years
    plus the current season, which is exactly what the baseline consumes.
    """
    if years < 1:
        raise ValueError("years must be at least 1")
    today = today or date.today()
    return date(today.year - years + 1, 1, 1), today


# --------------------------------------------------------------------------- #
# One day
# --------------------------------------------------------------------------- #


def process_day(
    field: Field, day_scenes: Sequence[SceneRef], settings: Settings
) -> DayResult:
    """Read one solar day, compute every index and cache the clipped raster.

    Runs on a worker thread and touches no database.
    """
    outcome = stac.load_and_mask(field, day_scenes, settings)
    if outcome.status != "kept" or outcome.clip is None or outcome.mask is None:
        return DayResult(outcome)

    clip = outcome.clip
    stats: dict[str, IndexStats] = {}
    for index_name in INDEX_NAMES:
        values = indices.compute_index(clip.bands, index_name)
        summary = indices.summarize(values, outcome.mask, n_total_px=clip.n_total_px)
        if summary is None:
            log.warning(
                "%s %s: %s had no usable pixels after masking",
                field.field_id, clip.solar_date, index_name,
            )
            continue
        stats[index_name] = summary

    clip_path = cache.write_clip_geotiff(
        clip, settings.clip_path(field.field_id, clip.solar_date.isoformat())
    )
    return DayResult(outcome, stats, clip_path)


def persist_day(
    conn: sqlite3.Connection,
    field: Field,
    result: DayResult,
    settings: Settings,
    *,
    force: bool = False,
) -> int:
    """Write one day's outcome and observations, returning rows written."""
    outcome = result.outcome
    cache.log_fetch(
        conn,
        field_id=field.field_id,
        obs_date=outcome.solar_date,
        scene_id=outcome.scene_id_key,
        valid_fraction=outcome.valid_fraction,
        status=outcome.status,
        reason=outcome.reason,
        duration_ms=outcome.duration_ms,
    )

    written = 0
    relative = (
        str(result.clip_path.relative_to(settings.root))
        if result.clip_path is not None
        else None
    )
    for index_name, summary in result.stats.items():
        written += cache.upsert_observation(
            conn,
            cache.ObservationRecord(
                field_id=field.field_id,
                obs_date=outcome.solar_date,
                index_name=index_name,
                stats=summary,
                scene_id=outcome.scene_id_key,
                clip_path=relative,
            ),
            force=force,
        )
    return written


# --------------------------------------------------------------------------- #
# One field
# --------------------------------------------------------------------------- #


def fetch_field(
    conn: sqlite3.Connection,
    field: Field,
    start: date,
    end: date,
    settings: Settings,
    *,
    force: bool = False,
    workers: int = DEFAULT_WORKERS,
    client: Client | None = None,
) -> FetchSummary:
    """Fetch every usable observation for one field between two dates.

    Days already resolved in a previous run are skipped unless ``force`` is set,
    which is what lets an interrupted fetch resume where it stopped.
    """
    started = time.monotonic()
    summary = FetchSummary(field_id=field.field_id)

    scenes = stac.search_scenes(field, start, end, settings, client=client)
    cache.upsert_scenes(conn, scenes)
    conn.commit()

    days = stac.group_by_solar_day(scenes)
    summary.days_available = len(days)

    already = {} if force else cache.attempted_dates(conn, field.field_id)
    todo = [(day, group) for day, group in days.items() if day not in already]
    summary.days_skipped = len(days) - len(todo)

    if not todo:
        log.info("%s: nothing to do, %d day(s) already cached", field.field_id, len(days))
        summary.elapsed_s = time.monotonic() - started
        return summary

    log.info(
        "%s: reading %d day(s), skipping %d already cached",
        field.field_id, len(todo), summary.days_skipped,
    )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(process_day, field, group, settings): day for day, group in todo
        }
        for done, future in enumerate(as_completed(futures), start=1):
            day = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad day must not sink the run
                log.error("%s %s: %s", field.field_id, day, exc)
                summary.errors += 1
                cache.log_fetch(
                    conn,
                    field_id=field.field_id,
                    obs_date=day,
                    scene_id=None,
                    valid_fraction=None,
                    status="error",
                    reason=str(exc),
                    duration_ms=0,
                )
                continue

            summary.days_read += 1
            if result.outcome.status == "kept":
                summary.kept += 1
            elif result.outcome.status == "dropped":
                summary.dropped += 1
            else:
                summary.errors += 1
            summary.observations_written += persist_day(
                conn, field, result, settings, force=force
            )
            if done % 10 == 0:
                conn.commit()
                log.info("%s: %d/%d day(s) processed", field.field_id, done, len(todo))

    conn.commit()
    summary.elapsed_s = time.monotonic() - started
    return summary


def fetch_fields(
    conn: sqlite3.Connection,
    fields: Sequence[Field],
    start: date,
    end: date,
    settings: Settings,
    *,
    force: bool = False,
    workers: int = DEFAULT_WORKERS,
) -> list[FetchSummary]:
    """Fetch several fields in turn, sharing one STAC client across them."""
    stac.assert_online(settings, "fetch imagery")
    client = stac.open_client(settings)
    return [
        fetch_field(
            conn, field, start, end, settings,
            force=force, workers=workers, client=client,
        )
        for field in fields
    ]
