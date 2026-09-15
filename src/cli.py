"""Click command line interface for the Dos Ojos satellite pipeline."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

import click

from . import baseline as baseline_mod
from . import cache, charts, cropmap, pipeline, soils, stac
from . import water as water_mod
from . import weather as weather_mod
from .config import INDEX_NAMES, Settings, default_root, setup_logging
from .fields import FieldValidationError, format_field_table, load_fields

log = logging.getLogger(__name__)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="SQLite cache location  [default: cache/dosojos.sqlite]",
)
@click.option(
    "--offline",
    is_flag=True,
    default=False,
    help="Forbid all network access and work purely from the cache.",
)
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Keep cache/ and out/ here instead of the project folder, e.g. to keep "
         "public demo fields apart from your own.",
)
@click.option("-v", "--verbose", count=True, help="Show debug logging.")
@click.pass_context
def cli(
    ctx: click.Context, db_path: Path | None, offline: bool, workspace: Path | None,
    verbose: int,
) -> None:
    """Dos Ojos - Sentinel-2 crop-stress triage for Rio Grande Valley fields."""
    setup_logging(verbose)
    root = workspace.resolve() if workspace else default_root()
    settings = Settings.from_root(root, db_path=db_path, offline=offline or None)
    settings.ensure_dirs()
    if settings.offline:
        stac.install_offline_guard()
    else:
        stac.configure_gdal_env()
    ctx.obj = settings


@cli.command("init-fields")
@click.argument(
    "geojson",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.pass_obj
def init_fields_cmd(settings: Settings, geojson: Path) -> None:
    """Validate field polygons from GEOJSON and register them in the cache."""
    try:
        fields = load_fields(geojson)
    except FieldValidationError as exc:
        raise click.ClickException(str(exc)) from exc

    with cache.session(settings.db_path) as conn:
        results = cache.upsert_fields(conn, fields, source_file=str(geojson.resolve()))

    click.echo(format_field_table(fields))
    click.echo()
    _report_sync(results)


def _report_sync(results: list[tuple[str, cache.FieldSyncStatus]]) -> None:
    """Summarise what changed, calling out redrawn polygons explicitly."""
    counts: dict[str, int] = {}
    for _, status in results:
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    click.echo(f"Registered {len(results)} field(s): {summary}")

    redrawn = [field_id for field_id, status in results if status == "geometry-changed"]
    if redrawn:
        click.echo()
        click.secho(
            f"WARNING: geometry changed for {', '.join(redrawn)}. Observations cached "
            "against the previous outline describe different ground; re-run fetch "
            "with --force for these fields.",
            fg="yellow",
        )


@cli.command("scenes")
@click.option("--field", "field_id", required=True, help="Field id to inspect.")
@click.option(
    "--start",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    required=True,
    help="First date of the window (inclusive).",
)
@click.option(
    "--end",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    required=True,
    help="Last date of the window (inclusive).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Only read the first N solar days, for a quick look.",
)
@click.pass_obj
def scenes_cmd(
    settings: Settings, field_id: str, start: datetime, end: datetime, limit: int | None
) -> None:
    """Search, clip and mask scenes for one field, reporting what would be kept.

    A diagnostic for the search and masking stage: it does the full windowed read
    and SCL mask but writes nothing to the cache.
    """
    with cache.session(settings.db_path) as conn:
        try:
            field = cache.get_fields(conn, [field_id])[0]
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc

    try:
        found = stac.search_scenes(field, start.date(), end.date(), settings)
    except (stac.StacUnavailableError, stac.OfflineViolation) as exc:
        raise click.ClickException(str(exc)) from exc

    days = stac.group_by_solar_day(found)
    if not days:
        click.echo(f"No scenes found for {field_id} between {start.date()} and {end.date()}.")
        return

    selected = list(days.items())[:limit] if limit else list(days.items())
    click.echo(
        f"{field.name} ({field.crop}) - {len(days)} solar day(s) available, "
        f"reading {len(selected)}\n"
    )

    outcomes = [stac.load_and_mask(field, day_scenes, settings) for _, day_scenes in selected]
    click.echo()
    click.echo(_format_outcomes(outcomes))

    kept = sum(1 for o in outcomes if o.status == "kept")
    click.echo(
        f"\n{kept}/{len(outcomes)} observation(s) usable "
        f"at a {settings.min_valid_fraction:.0%} valid-pixel threshold."
    )


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: str = ""
) -> str:
    """Render a fixed-width table; ``aligns`` is one of '<' or '>' per column."""
    if not rows:
        return "(nothing to show)"
    aligns = aligns or "<" * len(headers)
    widths = [
        max(len(headers[i]), *(len(str(row[i])) for row in rows))
        for i in range(len(headers))
    ]

    def render(cells: Sequence[str]) -> str:
        return "  ".join(
            f"{str(c):{a}{w}}" for c, a, w in zip(cells, aligns, widths)
        ).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([render(headers), rule, *(render(row) for row in rows)])


def _format_outcomes(outcomes: list[stac.ClipOutcome]) -> str:
    """Render one row per solar day with its masking verdict."""
    rows = [
        (
            outcome.solar_date.isoformat(),
            str(len(outcome.scene_ids)),
            f"{outcome.n_valid_px}/{outcome.n_total_px}",
            f"{outcome.valid_fraction:.3f}",
            outcome.status.upper(),
            outcome.reason or "-",
        )
        for outcome in outcomes
    ]
    return _table(
        ("DATE", "SCENES", "PIXELS", "VALID", "STATUS", "REASON"), rows, "<>>><<"
    )


@cli.command("fetch")
@click.option("--years", type=int, default=5, show_default=True,
              help="Calendar years to cover, counting back from this year.")
@click.option("--fields", "field_csv", default=None,
              help="Comma-separated field ids  [default: all registered fields]")
@click.option("--force", is_flag=True, default=False,
              help="Re-read and overwrite days already in the cache.")
@click.option("--workers", type=int, default=pipeline.DEFAULT_WORKERS,
              show_default=True, help="Parallel scene reads.")
@click.option("--start", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Override the window start, for a quick partial fetch.")
@click.option("--end", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Override the window end.")
@click.pass_obj
def fetch_cmd(
    settings: Settings,
    years: int,
    field_csv: str | None,
    force: bool,
    workers: int,
    start: datetime | None,
    end: datetime | None,
) -> None:
    """Download, mask and cache observations for the registered fields."""
    window_start, window_end = pipeline.season_range(years)
    if start is not None:
        window_start = start.date()
    if end is not None:
        window_end = end.date()
    if window_start > window_end:
        raise click.ClickException(f"start {window_start} is after end {window_end}")

    ids = [part.strip() for part in field_csv.split(",") if part.strip()] if field_csv else None
    with cache.session(settings.db_path) as conn:
        try:
            fields = cache.get_fields(conn, ids)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        if not fields:
            raise click.ClickException(
                "no fields registered. Run 'dosojos-sat init-fields <geojson>' first."
            )

        click.echo(
            f"Fetching {len(fields)} field(s) from {window_start} to {window_end}"
            f"{' (forced re-read)' if force else ''}\n"
        )
        try:
            summaries = pipeline.fetch_fields(
                conn, fields, window_start, window_end, settings,
                force=force, workers=workers,
            )
        except (stac.StacUnavailableError, stac.OfflineViolation) as exc:
            raise click.ClickException(str(exc)) from exc

    click.echo()
    click.echo(_format_summaries(summaries))


def _format_summaries(summaries: list[pipeline.FetchSummary]) -> str:
    """Render the per-field fetch tally."""
    rows = [
        (
            s.field_id, str(s.days_available), str(s.days_skipped), str(s.days_read),
            str(s.kept), str(s.dropped), str(s.errors),
            str(s.observations_written), f"{s.elapsed_s:.0f}s",
        )
        for s in summaries
    ]
    rows.append(
        (
            "TOTAL",
            str(sum(s.days_available for s in summaries)),
            str(sum(s.days_skipped for s in summaries)),
            str(sum(s.days_read for s in summaries)),
            str(sum(s.kept for s in summaries)),
            str(sum(s.dropped for s in summaries)),
            str(sum(s.errors for s in summaries)),
            str(sum(s.observations_written for s in summaries)),
            f"{sum(s.elapsed_s for s in summaries):.0f}s",
        )
    )
    headers = ("FIELD", "DAYS", "SKIP", "READ", "KEPT", "DROP", "ERR", "ROWS", "TIME")
    return _table(headers, rows, "<>>>>>>>>")


@cli.command("baseline")
@click.option("--index", "index_choice", default="all", show_default=True,
              type=click.Choice(["NDVI", "NDMI", "NDWI", "all"], case_sensitive=False),
              help="Which index to build a climatology for.")
@click.option("--season", type=int, default=None,
              help="Season being judged  [default: the current year]")
@click.option("--fields", "field_csv", default=None,
              help="Comma-separated field ids  [default: all registered fields]")
@click.option("--history-years", type=int, default=4, show_default=True,
              help="Full calendar years of history behind the baseline.")
@click.option("--history", "history_span", default=None, metavar="START-END",
              help="Explicit history years, e.g. 2019-2022, for a past season whose "
                   "preceding years predate Sentinel-2. Overrides --history-years.")
@click.option("--doy-window", type=int, default=12, show_default=True,
              help="Days either side of a date pooled into its bin.")
@click.option("--smooth-window", type=int, default=15, show_default=True,
              help="Width of the circular smoother, in days.")
@click.pass_obj
def baseline_cmd(
    settings: Settings,
    index_choice: str,
    season: int | None,
    field_csv: str | None,
    history_years: int,
    history_span: str | None,
    doy_window: int,
    smooth_window: int,
) -> None:
    """Build and cache each field's day-of-year climatology from its own history."""
    season = season or date.today().year
    start = end = None
    if history_span:
        try:
            start, end = (int(part) for part in history_span.split("-"))
        except ValueError as exc:
            raise click.ClickException(
                f"--history {history_span!r} is not START-END, e.g. 2019-2022"
            ) from exc
        history_years = end - start + 1
    try:
        params = baseline_mod.BaselineParams(
            season=season,
            history_years=history_years,
            doy_window=doy_window,
            smooth_window=smooth_window,
            history_start=start,
            history_end=end,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    indices = (
        list(INDEX_NAMES) if index_choice.lower() == "all" else [index_choice.upper()]
    )
    ids = [p.strip() for p in field_csv.split(",") if p.strip()] if field_csv else None

    year_min, year_max = params.year_range
    click.echo(
        f"Baseline for season {season}: history {year_min}-{year_max}, "
        f"+/-{doy_window}d window, {smooth_window}d smoothing\n"
    )

    rows: list[tuple[str, ...]] = []
    thin: list[tuple[str, str, str]] = []
    with cache.session(settings.db_path) as conn:
        try:
            fields = cache.get_fields(conn, ids)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc

        for field in fields:
            for index_name in indices:
                history = cache.get_observations(
                    conn, field.field_id, index_name, year_range=params.year_range
                )
                table = baseline_mod.build_baseline(history, params)
                run = baseline_mod.BaselineRun.now(index_name, params)
                cache.upsert_baseline(conn, field.field_id, run, table)

                grades = baseline_mod.confidence_summary(table)
                rows.append(
                    (
                        field.field_id, index_name, str(len(history)),
                        str(grades["high"]), str(grades["medium"]),
                        str(grades["low"]), str(grades["none"]),
                        f"{int(table['interpolated'].sum())}",
                    )
                )
                if index_name == indices[0]:
                    for start, end, grade in baseline_mod.thin_stretches(table):
                        thin.append(
                            (
                                f"{field.field_id} {index_name}",
                                f"{_doy_label(start)} - {_doy_label(end)}",
                                f"{grade} ({(end - start) % 366 + 1}d)",
                            )
                        )

    click.echo(
        _table(
            ("FIELD", "INDEX", "HIST OBS", "HIGH", "MEDIUM", "LOW", "NONE", "INTERP"),
            rows, "<<>>>>>>",
        )
    )
    if thin:
        click.echo("\nStretches where the baseline is thin")
        click.echo(_table(("FIELD", "DAYS OF YEAR", "GRADE"), thin, "<<<"))
    else:
        click.echo("\nNo thin stretches: every day of year is backed by 2+ years.")


def _doy_label(doy: int) -> str:
    """Render a day of year as a calendar date, using a leap year for labelling.

    Builds the day number by hand rather than with a strftime flag, since the
    no-padding flag differs between platforms (%-d on glibc, %#d on Windows).
    """
    when = date(2024, 1, 1) + timedelta(days=int(doy) - 1)
    return f"{when.day} {when.strftime('%b')}"


@cli.command("status")
@click.pass_obj
def status_cmd(settings: Settings) -> None:
    """Report what is cached: coverage per field, gaps and disk use."""
    with cache.session(settings.db_path) as conn:
        fields = cache.get_fields(conn)
        coverage = cache.coverage_report(conn)
        attempts = cache.fetch_log_summary(conn)
        gaps = cache.observation_gaps(conn)
        saturated = cache.saturation_report(conn)

    click.echo(f"Cache: {settings.db_path}")
    click.echo(f"Mode:  {'OFFLINE' if settings.offline else 'online'}\n")

    if not fields:
        click.echo("No fields registered. Run 'dosojos-sat init-fields <geojson>'.")
        return

    click.echo(format_field_table(fields))

    click.echo("\nObservations")
    if coverage.empty:
        click.echo("(none cached yet - run 'dosojos-sat fetch')")
    else:
        rows = [
            (
                row.field_id, row.index_name, str(row.n_obs), str(row.n_years),
                str(row.first_date), str(row.last_date), f"{row.mean_valid_fraction:.3f}",
            )
            for row in coverage.itertuples()
        ]
        click.echo(
            _table(
                ("FIELD", "INDEX", "N", "YEARS", "FIRST", "LAST", "MEAN VALID"),
                rows, "<<>>>><",
            )
        )

    if not attempts.empty:
        click.echo("\nFetch attempts by day")
        pivot = attempts.pivot_table(
            index="field_id", columns="status", values="n", fill_value=0
        )
        statuses = [s for s in ("kept", "dropped", "error") if s in pivot.columns]
        rows = [
            (str(field_id), *(str(int(pivot.loc[field_id, s])) for s in statuses))
            for field_id in pivot.index
        ]
        click.echo(_table(("FIELD", *(s.upper() for s in statuses)), rows, "<" + ">" * len(statuses)))

    if not gaps.empty:
        click.echo("\nLongest gaps with no usable observation (30+ days)")
        rows = [
            (row.field_id, str(row.gap_start), str(row.gap_end), f"{row.days}d")
            for row in gaps.head(8).itertuples()
        ]
        click.echo(_table(("FIELD", "FROM", "TO", "LENGTH"), rows, "<<<>"))

    if not saturated.empty:
        click.echo()
        click.secho(
            "WARNING: some indices are pinned at their bound unusually often, "
            "which points at mis-scaled reflectance rather than real crop cover:",
            fg="yellow",
        )
        rows = [
            (row.field_id, row.index_name, f"{row.n_pinned}/{row.n_obs}",
             f"{row.pinned_fraction:.0%}")
            for row in saturated.itertuples()
        ]
        click.echo(_table(("FIELD", "INDEX", "PINNED", "SHARE"), rows, "<<>>"))

    click.echo(f"\nCached clips: {_clip_cache_summary(settings)}")


def _clip_cache_summary(settings: Settings) -> str:
    """Count and total size of the GeoTIFF clips on disk."""
    clips = list(settings.clips_dir.rglob("*.tif"))
    if not clips:
        return "none"
    total_mb = sum(p.stat().st_size for p in clips) / (1024 * 1024)
    return f"{len(clips)} file(s), {total_mb:.1f} MB in {settings.clips_dir}"


@cli.command("score")
@click.option("--season", type=int, default=None,
              help="Season to judge  [default: the current year]")
@click.option("--out", "out_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Where to write the ranked JSON  [default: out/flags.json]")
@click.option("--fields", "field_csv", default=None,
              help="Comma-separated field ids  [default: all registered fields]")
@click.option("--as-of", "as_of", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Judge the season as it stood on this date, e.g. a drone flight's.")
@click.pass_obj
def score_cmd(
    settings: Settings, season: int | None, out_path: Path | None, field_csv: str | None,
    as_of: datetime | None,
) -> None:
    """Rank fields by how far this season has drifted below their own normal."""
    season = season or (as_of.year if as_of else date.today().year)
    cutoff = as_of.date() if as_of else None
    out_path = out_path or (settings.out_dir / "flags.json")
    ids = [p.strip() for p in field_csv.split(",") if p.strip()] if field_csv else None

    scores: list[baseline_mod.FieldScore] = []
    missing: list[str] = []
    with cache.session(settings.db_path) as conn:
        try:
            fields = cache.get_fields(conn, ids)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc

        for field in fields:
            per_index = {}
            for index_name in ("NDVI", "NDMI"):
                base = cache.get_baseline(conn, field.field_id, index_name)
                if base.empty:
                    missing.append(f"{field.field_id}/{index_name}")
                    per_index[index_name] = []
                    continue
                info = cache.baseline_run_info(conn, field.field_id, index_name) or {}
                history = cache.get_observations(
                    conn, field.field_id, index_name,
                    year_range=(int(info.get("year_min", season - 4)),
                                int(info.get("year_max", season - 1))),
                )
                season_obs = _until(cache.get_observations(
                    conn, field.field_id, index_name, year_range=(season, season)
                ), cutoff)
                per_index[index_name] = baseline_mod.score_observations(
                    season_obs, base, history
                )
            scores.append(
                baseline_mod.field_stress_score(
                    field.field_id, field.name, field.crop,
                    per_index["NDVI"], per_index["NDMI"],
                )
            )

    if missing:
        raise click.ClickException(
            "no baseline cached for " + ", ".join(missing)
            + ". Run 'dosojos-sat baseline' first."
        )

    ranked = baseline_mod.rank_fields(scores)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"season": season, "generated": _utc_now(), "fields": ranked}
    if cutoff:
        payload["as_of"] = cutoff.isoformat()
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    click.echo(f"Season {season} triage, worst first"
               + (f", as it stood on {cutoff}" if cutoff else ""))
    click.echo()
    click.echo(_format_scores(ranked))
    click.echo()
    flagged = sum(1 for r in ranked if r["flagged"])
    click.echo(f"{flagged}/{len(ranked)} field(s) flagged. Written to {out_path}")


def _until(observations, cutoff: date | None):
    """Drop observations after ``cutoff``, so a past date is judged on what was known then."""
    if cutoff is None or observations.empty:
        return observations
    return observations[observations["date"] <= cutoff].reset_index(drop=True)


def _utc_now() -> str:
    """Timestamp for the output file."""
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_scores(ranked: list[dict]) -> str:
    """Render the ranked triage table."""
    rows = [
        (
            r["field_id"], str(r["name"])[:22], str(r["crop"])[:14],
            f"{r['score']:.1f}",
            "YES" if r["flagged"] else "-",
            "water" if r["water_stress"] else "-",
            str(r["n_consecutive_low"]),
            "-" if r["latest_ndvi"] is None else f"{r['latest_ndvi']:.3f}",
            "-" if r["baseline_median"] is None else f"{r['baseline_median']:.3f}",
            "-" if r["percentile"] is None else f"{r['percentile']:.0f}",
            str(r["last_observation_date"] or "-"),
            str(r["baseline_confidence"]),
        )
        for r in ranked
    ]
    return _table(
        ("FIELD", "NAME", "CROP", "SCORE", "FLAG", "TYPE", "RUN",
         "NDVI", "BASE", "PCTL", "LAST OBS", "CONF"),
        rows, "<<<>>><>>>><",
    )


@cli.command("chart")
@click.option("--field", "field_id", default=None,
              help="Field id to chart  [default: every registered field]")
@click.option("--index", "index_choice", default="NDVI", show_default=True,
              type=click.Choice(["NDVI", "NDMI", "NDWI", "all"], case_sensitive=False),
              help="Which index to plot.")
@click.option("--season", type=int, default=None,
              help="Season to overlay  [default: the current year]")
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Directory for the PNGs  [default: out/]")
@click.option("--as-of", "as_of", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Judge the season as it stood on this date; later points are faded.")
@click.option("--banner", default=None,
              help="Text for a band above the title, e.g. to mark public demo data.")
@click.pass_obj
def chart_cmd(
    settings: Settings,
    field_id: str | None,
    index_choice: str,
    season: int | None,
    out_dir: Path | None,
    as_of: datetime | None,
    banner: str | None,
) -> None:
    """Draw each field's season against its own baseline as a PNG."""
    season = season or (as_of.year if as_of else date.today().year)
    cutoff = as_of.date() if as_of else None
    out_dir = out_dir or settings.out_dir
    indices = (
        list(INDEX_NAMES) if index_choice.lower() == "all" else [index_choice.upper()]
    )
    ids = [field_id] if field_id else None

    written: list[Path] = []
    with cache.session(settings.db_path) as conn:
        try:
            fields = cache.get_fields(conn, ids)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc

        for field in fields:
            for index_name in indices:
                baseline = cache.get_baseline(conn, field.field_id, index_name)
                if baseline.empty:
                    raise click.ClickException(
                        f"no baseline cached for {field.field_id}/{index_name}. "
                        "Run 'dosojos-sat baseline' first."
                    )
                info = cache.baseline_run_info(conn, field.field_id, index_name) or {}
                history = (int(info.get("year_min", season - 4)),
                           int(info.get("year_max", season - 1)))
                season_obs = cache.get_observations(
                    conn, field.field_id, index_name, year_range=(season, season)
                )
                hist_obs = cache.get_observations(
                    conn, field.field_id, index_name, year_range=history
                )
                scored = baseline_mod.score_observations(
                    _until(season_obs, cutoff), baseline, hist_obs
                )
                flagged = {s.obs_date for s in scored if s.below_p10}

                ndmi = baseline_mod.score_observations(
                    _until(cache.get_observations(
                        conn, field.field_id, "NDMI", year_range=(season, season)
                    ), cutoff),
                    cache.get_baseline(conn, field.field_id, "NDMI"),
                    cache.get_observations(
                        conn, field.field_id, "NDMI", year_range=history
                    ),
                )
                summary = baseline_mod.field_stress_score(
                    field.field_id, field.name, field.crop, scored, ndmi
                )
                run = baseline_mod.trailing_depressed_run(scored)

                written.append(
                    charts.plot_field(
                        field_id=field.field_id, name=field.name, crop=field.crop,
                        index_name=index_name, baseline=baseline, season=season_obs,
                        year=season, out_dir=out_dir, history_years=history,
                        flagged_dates=flagged,
                        verdict=_verdict(summary),
                        run_start=scored[-run].obs_date if run else None,
                        cutoff=cutoff, banner=banner,
                    )
                )

    for path in written:
        click.echo(f"  {path}")
    click.echo(f"{len(written)} chart(s) written to {out_dir}")


def _verdict(summary: baseline_mod.FieldScore) -> str:
    """The one line a chart carries under its title."""
    if summary.flagged:
        kind = "water stress" if summary.water_stress else "stress, cause unclear"
        route = (
            "sustained shortfall"
            if summary.trigger == "sustained-shortfall"
            else f"{summary.n_consecutive_low} obs below p10"
        )
        return f"FLAGGED  -  score {summary.score:.0f}/100  -  {kind}  ({route})"
    return f"not flagged  -  score {summary.score:.0f}/100"


# --------------------------------------------------------------------------- #
# Water: weather, soil and the checkbook
# --------------------------------------------------------------------------- #

#: gridMET revises its last week or so; these days are re-read on every fetch.
WEATHER_REFRESH_DAYS = 10


def _ids(field_csv: str | None) -> list[str] | None:
    return [p.strip() for p in field_csv.split(",") if p.strip()] if field_csv else None


@cli.command("weather")
@click.option("--season", type=int, default=None,
              help="Year to cover  [default: the current year]")
@click.option("--start", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="First day  [default: 1 January of the season]")
@click.option("--end", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Last day  [default: today, or 31 December of a past season]")
@click.option("--fields", "field_csv", default=None,
              help="Comma-separated field ids  [default: all registered fields]")
@click.option("--station", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Load a weather station CSV (a date column plus ETo and rain with "
                   "their units, e.g. eto_in, rain_in) for the chosen fields instead.")
@click.option("--force", is_flag=True, help="Re-fetch days already cached.")
@click.pass_obj
def weather_cmd(
    settings: Settings, season: int | None, start: datetime | None, end: datetime | None,
    field_csv: str | None, station: Path | None, force: bool,
) -> None:
    """Fetch each field's daily reference ET and rain from gridMET, or load a station."""
    today = date.today()
    season = season or (start.year if start else today.year)
    window_start = start.date() if start else date(season, 1, 1)
    window_end = end.date() if end else min(today, date(season, 12, 31))
    if window_start > window_end:
        raise click.ClickException(f"start {window_start} is after end {window_end}")

    rows = []
    with cache.session(settings.db_path) as conn:
        try:
            fields = cache.get_fields(conn, _ids(field_csv))
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        if not fields:
            raise click.ClickException(
                "no fields registered. Run 'dosojos-sat init-fields <geojson>' first."
            )

        if station is not None:
            try:
                frame = weather_mod.read_station_csv(station)
            except weather_mod.WeatherError as exc:
                raise click.ClickException(str(exc)) from exc
            source = weather_mod.station_source(station)
            for field in fields:
                written = cache.upsert_weather(conn, field.field_id, frame, source)
                rows.append(_weather_row(field.field_id, source, frame, written))
        else:
            try:
                stac.assert_online(settings, "fetch weather from gridMET")
            except stac.OfflineViolation as exc:
                raise click.ClickException(str(exc)) from exc
            for field in fields:
                cached = cache.weather_dates(conn, field.field_id, weather_mod.GRIDMET_SOURCE)
                wanted = [window_start + timedelta(days=i)
                          for i in range((window_end - window_start).days + 1)]
                stale = today - timedelta(days=WEATHER_REFRESH_DAYS)
                needed = [d for d in wanted if force or d not in cached or d >= stale]
                if not needed:
                    rows.append((field.field_id, "gridmet", str(window_start), str(window_end),
                                 "-", "-", "-", "cached"))
                    continue
                lon, lat = field.centroid_lonlat
                try:
                    frame = weather_mod.fetch_gridmet(lat, lon, min(needed), window_end)
                except weather_mod.WeatherError as exc:
                    raise click.ClickException(f"{field.field_id}: {exc}") from exc
                written = cache.upsert_weather(conn, field.field_id, frame,
                                               weather_mod.GRIDMET_SOURCE)
                rows.append(_weather_row(field.field_id, "gridmet", frame, written))

    click.echo(_table(("FIELD", "SOURCE", "FROM", "TO", "DAYS", "ETO IN", "RAIN IN", "STORED"),
                      rows, "<<<<>>>>"))
    if station is None and rows:
        click.echo("\ngridMET runs a day or two behind; the checkbook fills the gap with "
                   "the week before it.")


def _weather_row(field_id: str, source: str, frame, written: int) -> tuple[str, ...]:
    """One line of the weather table: span, day count and inch totals."""
    if frame.empty:
        return (field_id, source, "-", "-", "0", "-", "-", str(written))
    mm = weather_mod.MM_PER_INCH
    return (
        field_id, source, str(min(frame["date"])), str(max(frame["date"])),
        str(int(frame["eto_mm"].notna().sum())),
        f"{frame['eto_mm'].sum() / mm:.1f}", f"{frame['rain_mm'].sum() / mm:.1f}",
        str(written),
    )


@cli.command("soil")
@click.option("--fields", "field_csv", default=None,
              help="Comma-separated field ids  [default: all registered fields]")
@click.option("--force", is_flag=True, help="Re-read soils already cached.")
@click.pass_obj
def soil_cmd(settings: Settings, field_csv: str | None, force: bool) -> None:
    """Read how much water each field's soil holds, from the USDA soil survey."""
    rows = []
    with cache.session(settings.db_path) as conn:
        try:
            fields = cache.get_fields(conn, _ids(field_csv))
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        for field in fields:
            cached = cache.get_soil(conn, field.field_id)
            if field.soil_awc_in_ft:
                profile = soils.manual_profile(field.soil_awc_in_ft)
                cache.upsert_soil(conn, field.field_id, field.geom_hash, "manual",
                                  profile.to_dict())
                note = "from fields.geojson"
            elif (cached and not force and cached["source"] == "ssurgo"
                  and cached["geom_hash"] == field.geom_hash):
                profile = soils.SoilProfile.from_dict(cached["profile"])
                note = "cached"
            else:
                try:
                    stac.assert_online(settings, "read the soil survey")
                    profile = soils.fetch_ssurgo(field.geometry)
                except stac.OfflineViolation as exc:
                    raise click.ClickException(str(exc)) from exc
                except soils.SoilError as exc:
                    raise click.ClickException(f"{field.field_id}: {exc}") from exc
                cache.upsert_soil(conn, field.field_id, field.geom_hash, "ssurgo",
                                  profile.to_dict())
                note = "USDA SSURGO"
            rows.append((
                field.field_id, profile.name[:40], f"{profile.awc_in_per_ft:.2f}",
                f"{profile.taw_mm(1.2) / weather_mod.MM_PER_INCH:.1f}",
                profile.texture, profile.intake, profile.drainage or "-", note,
            ))

    click.echo(_table(
        ("FIELD", "SOIL", "IN/FT", "IN TO 4 FT", "TEXTURE", "INTAKE", "DRAINAGE", "SOURCE"),
        rows, "<<>><<<<",
    ))


def _soil_note(profile: soils.SoilProfile) -> dict:
    """The soil facts carried into each field's checkbook result."""
    return {
        "name": profile.name[:48], "source": profile.source,
        "awc_in_per_ft": round(profile.awc_in_per_ft, 2), "texture": profile.texture,
        "intake": profile.intake, "hydgrp": profile.hydgrp, "drainage": profile.drainage,
    }


@cli.command("water")
@click.option("--as-of", "as_of", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Judge the fields as they stood on this date  [default: today]")
@click.option("--log", "log_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None,
              help="Field log of plantings, irrigations and rain gauge readings  "
                   "[default: field_log.csv beside cache/ and out/]")
@click.option("--fields", "field_csv", default=None,
              help="Comma-separated field ids  [default: all registered fields]")
@click.option("--out", "out_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Ranked JSON  [default: out/water.json]")
@click.option("--banner", default=None,
              help="Text for a band above each chart's title, e.g. to mark demo data.")
@click.pass_obj
def water_cmd(
    settings: Settings, as_of: datetime | None, log_path: Path | None,
    field_csv: str | None, out_path: Path | None, banner: str | None,
) -> None:
    """Work out how much water each field has left, and who needs it first."""
    cutoff = as_of.date() if as_of else date.today()
    log_path = log_path or (settings.root / "field_log.csv")
    out_path = out_path or (settings.out_dir / "water.json")
    try:
        events = water_mod.read_field_log(log_path)
    except water_mod.FieldLogError as exc:
        raise click.ClickException(str(exc)) from exc
    if not log_path.exists():
        click.secho(f"NOTE: no field log at {log_path}; every start will be assumed. Add "
                    "rows of field_id,date,event,inches (planted / irrigated / rain / "
                    "harvested) for a real answer.", fg="yellow")

    statuses, failures = [], []
    daily_dir = settings.out_dir / "water"
    daily_dir.mkdir(parents=True, exist_ok=True)
    with cache.session(settings.db_path) as conn:
        try:
            fields = cache.get_fields(conn, _ids(field_csv))
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        known = {f.field_id for f in cache.get_fields(conn)}
        strays = sorted({e.field_id for e in events} - known)
        if strays:
            click.secho(f"WARNING: the field log names unregistered field(s): "
                        f"{', '.join(strays)}", fg="yellow")

        for field in fields:
            if field.soil_awc_in_ft:
                profile = soils.manual_profile(field.soil_awc_in_ft)
            else:
                cached = cache.get_soil(conn, field.field_id)
                if cached is None:
                    failures.append(f"{field.field_id}: no soil cached. Run 'dosojos-sat soil'.")
                    continue
                if cached["geom_hash"] != field.geom_hash:
                    click.secho(f"WARNING: {field.field_id} was redrawn after its soil was "
                                "read; run 'dosojos-sat soil --force'.", fg="yellow")
                profile = soils.SoilProfile.from_dict(cached["profile"])
            weather = cache.get_weather(conn, field.field_id,
                                        cutoff - timedelta(days=400), cutoff)
            ndvi = cache.get_observations(conn, field.field_id, "NDVI",
                                          year_range=(cutoff.year - 1, cutoff.year))
            try:
                status, daily, projection = water_mod.checkbook(
                    field_id=field.field_id, name=field.name, crop_text=field.crop,
                    soil=profile, soil_note=_soil_note(profile), weather=weather, ndvi=ndvi,
                    events=[e for e in events if e.field_id == field.field_id],
                    as_of=cutoff, method=field.irrigation,
                )
            except water_mod.WaterError as exc:
                failures.append(f"{field.field_id}: {exc}")
                continue
            daily.to_csv(daily_dir / f"{field.field_id}_daily.csv", index=False)
            charts.plot_water(status=status.to_dict(), daily=daily, projection=projection,
                              out_dir=settings.out_dir, banner=banner)
            statuses.append(status)

    for failure in failures:
        click.secho(f"WARNING: {failure}", fg="yellow")
    if not statuses:
        raise click.ClickException("no field could be judged; see the warnings above")

    ranked = water_mod.rank(statuses)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "as_of": cutoff.isoformat(), "generated": _utc_now(),
        "field_log": str(log_path) if log_path.exists() else None,
        "fields": [s.to_dict() for s in ranked],
    }, indent=2), encoding="utf-8")

    click.echo(f"Water checkbook as of {cutoff}, who needs water first\n")
    click.echo(_format_water(ranked))
    for status in ranked:
        extra = ([status.sensitive] if status.sensitive else []) + status.notes
        for line in extra:
            click.echo(f"  {status.field_id}: {line}")
    click.echo(f"\nWritten to {out_path}, with a chart and a daily CSV per field.")


def _format_water(ranked: Sequence[water_mod.WaterStatus]) -> str:
    """The ranked table: urgency, water left, when, and how much to put back."""
    def inches(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f}"

    rows = []
    for s in ranked:
        days = "-" if s.days_left is None else (
            "now" if s.days_left == 0 else
            f"{s.days_left} ({s.days_range[0]}-{s.days_range[1]})" if s.days_range else str(s.days_left)
        )
        harvested = s.status == water_mod.STATUS_HARVESTED
        if harvested:
            days = "-"
        refill = "-" if s.refill_net_in is None else (
            f"{s.refill_net_in:.1f}" + (f" / {s.refill_gross_in:.1f}" if s.refill_gross_in else "")
        )
        rows.append((
            str(s.rank), s.field_id, str(s.name)[:22], str(s.crop)[:14], s.status.upper(),
            days, s.water_by or "-", "-" if harvested else inches(s.water_left_in),
            inches(s.until_stress_in), refill, s.confidence,
        ))
    return _table(
        ("#", "FIELD", "NAME", "CROP", "STATUS", "DAYS LEFT", "WATER BY", "LEFT IN",
         "TO STRESS", "REFILL NET/GROSS", "CONF"),
        rows, "><<<<><>>><",
    )


@cli.command("recheck-offsets")
@click.option("--fields", "field_csv", default=None,
              help="Comma-separated field ids  [default: all registered fields]")
@click.option("--dry-run", is_flag=True, help="List the days without reading them again.")
@click.pass_obj
def recheck_offsets_cmd(settings: Settings, field_csv: str | None, dry_run: bool) -> None:
    """Re-read cached days whose scenes were wrongly flagged as still needing the offset.

    Some Earth Search items say ``earthsearch:boa_offset_applied: false`` although
    their files already carry the -0.1 offset. Days fetched before this was caught
    had it subtracted twice. This finds them by the scenes' own pixels and reads
    them again, correctly.
    """
    try:
        stac.assert_online(settings, "look the cached scenes up again")
    except stac.OfflineViolation as exc:
        raise click.ClickException(str(exc)) from exc
    with cache.session(settings.db_path) as conn:
        try:
            fields = cache.get_fields(conn, _ids(field_csv))
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        wanted = {f.field_id for f in fields}
        days = [(r["field_id"], date.fromisoformat(r["date"]), r["scene_id"]) for r in conn.execute(
            "SELECT DISTINCT field_id, date, scene_id FROM observations WHERE index_name = 'NDVI'")
            if r["field_id"] in wanted]
        scene_ids = sorted({part for _, _, joined in days for part in joined.split("+")})
        client = stac.open_client(settings)
        items = []
        for start in range(0, len(scene_ids), 100):
            batch = scene_ids[start:start + 100]
            items += list(client.search(collections=[settings.collection], ids=batch).items())
        doubled = {s.scene_id for s in stac._to_scene_refs(items)
                   if not s.boa_offset_applied and stac.harmonised(s)}
        redo: dict[str, list[date]] = {}
        for field_id, day, joined in days:
            if doubled & set(joined.split("+")):
                redo.setdefault(field_id, []).append(day)
        rows = [(f.field_id, str(len(redo.get(f.field_id, []))),
                 ", ".join(str(d) for d in sorted(redo.get(f.field_id, []))[:6])
                 + (" ..." if len(redo.get(f.field_id, [])) > 6 else "")) for f in fields]
        click.echo(f"{len(scene_ids)} cached scene(s) looked up; {len(doubled)} flagged raw but "
                   "already harmonised.\n")
        click.echo(_table(("FIELD", "DAYS", "WHICH"), rows, "<><"))
        if dry_run or not redo:
            return
        for field in fields:
            for day in sorted(redo.get(field.field_id, [])):
                pipeline.fetch_fields(conn, [field], day, day, settings, force=True, workers=1)
        click.echo(f"\nRead {sum(len(v) for v in redo.values())} day(s) again. Re-run "
                   "baseline, score and water to use them.")


@cli.command("cropmap")
@click.option("--year", type=int, default=2025, show_default=True, help="Crop map year.")
@click.option("--bbox", default="-98.45,26.05,-97.45,26.55", show_default=True,
              help="west,south,east,north in degrees; the default is the Valley's farmland.")
@click.option("--crops", default="sorghum,cotton,corn,citrus", show_default=True,
              help=f"Comma-separated, from: {', '.join(cropmap.CROP_CODES)}")
@click.option("--per-crop", type=int, default=1, show_default=True)
@click.option("--prefix", default="PUBLIC-rgv", show_default=True, help="Start of each field id.")
@click.option("--out", "out_path", type=click.Path(dir_okay=False, path_type=Path), required=True,
              help="The fields.geojson to write.")
@click.option("--banner", default=None, help="Band above the map, e.g. to mark public data.")
@click.option("--history", "history_span", default=None,
              help="Earlier years in which an annual crop's field must have been one crop, "
                   "e.g. 2021-2024; 'none' to skip  [default: the four years before --year]")
@click.pass_obj
def cropmap_cmd(settings: Settings, year: int, bbox: str, crops: str, per_crop: int, prefix: str,
                out_path: Path, banner: str | None, history_span: str | None) -> None:
    """Pick real fields of each crop from USDA's public crop map (Cropland Data Layer).

    A block of one crop can be two fields with a farm road between them, which a
    30 m map cannot see. For annual crops the maps of earlier years settle it: a
    field is kept only if one crop covered it in each of those years too.
    """
    try:
        box_ = tuple(float(v) for v in bbox.split(","))
        assert len(box_) == 4 and box_[0] < box_[2] and box_[1] < box_[3]
    except (ValueError, AssertionError):
        raise click.ClickException(f"--bbox {bbox!r} must be west,south,east,north in degrees")
    if history_span is None:
        history_years: tuple[int, ...] = tuple(range(year - 4, year))
    elif history_span.strip().lower() == "none":
        history_years = ()
    else:
        try:
            first, last = (int(v) for v in history_span.split("-"))
            assert first <= last < year
        except (ValueError, AssertionError):
            raise click.ClickException(f"--history {history_span!r} must be like 2021-2024, "
                                       f"before {year}, or 'none'")
        history_years = tuple(range(first, last + 1))
    folder = settings.root / "cache" / "cdl"
    try:
        if not (folder / f"cdl_{year}_classes.json").exists():
            stac.assert_online(settings, "read the USDA crop map")
        map_path = cropmap.fetch(box_, year, folder)
    except (stac.OfflineViolation, cropmap.CropMapError) as exc:
        raise click.ClickException(str(exc)) from exc

    chosen, rows, skipped = [], [], []

    def one_field(candidate: cropmap.Candidate) -> bool:
        """Whether the candidate was one crop in every earlier year (annual crops only)."""
        if candidate.crop in cropmap.PERENNIAL or not history_years:
            return True
        shares = cropmap.history(candidate, history_years, folder)
        mixed = {y: s for y, s in sorted(shares.items()) if s < cropmap.MIN_HISTORY_PURITY}
        if mixed:
            skipped.append(f"{candidate.crop} near {candidate.town} "
                           f"({candidate.geometry.centroid.y:.4f}, "
                           f"{candidate.geometry.centroid.x:.4f}): one crop over only "
                           + ", ".join(f"{s:.0%} in {y}" for y, s in mixed.items()))
        return not mixed

    for crop in [c.strip() for c in crops.split(",") if c.strip()]:
        try:
            found = cropmap.trace(map_path, crop)
            picked = cropmap.pick(found, per_crop, accept=one_field)
        except (cropmap.CropMapError, stac.OfflineViolation) as exc:
            raise click.ClickException(str(exc)) from exc
        if not picked:
            why = (f" that was one crop in each of {history_years[0]}-{history_years[-1]} too"
                   if found and history_years and crop not in cropmap.PERENNIAL else "")
            click.secho(f"WARNING: no {crop} field of 15-250 acres on the {year} crop map in "
                        f"this box{why}.", fg="yellow")
        chosen += picked
        rows += [(crop, str(len(found)), c.town, f"{c.acres:.0f}", f"{c.purity:.0%}",
                  f"{c.geometry.centroid.y:.4f}, {c.geometry.centroid.x:.4f}") for c in picked]
    if not chosen:
        raise click.ClickException("no field found for any crop; widen --bbox or change --crops")
    collection = cropmap.features(chosen, year=year, prefix=prefix, history_years=history_years)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(collection, indent=2) + "\n", encoding="utf-8")
    picture = charts.plot_cropmap(map_path, cropmap.class_table(map_path), collection,
                                  settings.out_dir / f"cropmap_{year}.png", year=year,
                                  banner=banner)
    click.echo(_table(("CROP", "FIELDS", "NEAR", "ACRES", "PURE", "CENTER"), rows, "<><>><"))
    if skipped:
        click.echo(f"\nPassed over {len(skipped)} block(s) that earlier maps show as two fields, "
                   "or one farmed in parts:")
        for line in skipped[:8]:
            click.echo(f"  {line}")
        if len(skipped) > 8:
            click.echo(f"  ... and {len(skipped) - 8} more")
    click.echo(f"\n{len(chosen)} field(s) written to {out_path}\nMap: {picture}")
    click.echo("Outlines are traced inside each field's edge on a 30 m map, not surveyed.")


if __name__ == "__main__":  # pragma: no cover  (last, once every command is registered)
    cli()
