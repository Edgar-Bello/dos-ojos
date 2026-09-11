"""Click command line interface for the Dos Ojos satellite pipeline."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

import click

from . import baseline as baseline_mod
from . import cache, charts, pipeline, stac
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


if __name__ == "__main__":  # pragma: no cover
    cli()


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
