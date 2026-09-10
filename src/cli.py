"""Click command line interface for the Dos Ojos drone pipeline."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

import click

from . import video, viz
from .config import (
    Flight,
    ManifestError,
    Settings,
    default_root,
    get_flight,
    load_manifest,
    register_flight,
)
from .ingest import FlightSurvey, IngestError, estimate_coverage, survey_flight

log = logging.getLogger(__name__)


def setup_logging(verbosity: int = 0) -> None:
    """Configure stderr logging; ``verbosity`` >= 1 turns on DEBUG."""
    level = logging.DEBUG if verbosity else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("PIL", "matplotlib", "rasterio", "fiona"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", count=True, help="Show debug logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: int) -> None:
    """Dos Ojos - drone post-processing for Rio Grande Valley fields."""
    setup_logging(verbose)
    ctx.obj = Settings.from_root(default_root())


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


@cli.command("register")
@click.argument("flight_id")
@click.option("--field", "field_id", required=True,
              help="Satellite field id this flight covers; the join key.")
@click.option("--date", "flown_on", type=click.DateTime(formats=["%Y-%m-%d"]),
              default=None, help="Flight date.")
@click.option("--crop", default=None, help="Crop, e.g. sorghum or sugarcane.")
@click.option("--notes", default=None, help="Anything worth remembering.")
@click.option("--ground-elevation", type=float, default=None,
              help="Terrain elevation in metres, for height above ground.")
@click.option("--force", is_flag=True, help="Remap a flight already registered.")
@click.pass_obj
def register_cmd(
    settings: Settings,
    flight_id: str,
    field_id: str,
    flown_on: datetime | None,
    crop: str | None,
    notes: str | None,
    ground_elevation: float | None,
    force: bool,
) -> None:
    """Map a flight to a satellite field in flights.json."""
    flight = Flight(
        flight_id=flight_id,
        field_id=field_id,
        flown_on=flown_on.date() if flown_on else None,
        crop=crop,
        notes=notes,
        ground_elevation_m=ground_elevation,
    )
    try:
        stored = register_flight(settings.manifest_path, flight, overwrite=force)
    except ManifestError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"{stored.flight_id} -> field {stored.field_id}")
    click.echo(f"Manifest: {settings.manifest_path}")


@cli.command("flights")
@click.pass_obj
def flights_cmd(settings: Settings) -> None:
    """List the registered flights and whether their images are present."""
    try:
        flights = load_manifest(settings.manifest_path)
    except ManifestError as exc:
        raise click.ClickException(str(exc)) from exc

    if not flights:
        click.echo(
            "No flights registered. Add one with:\n"
            "  dosojos-drone register <flight_id> --field <field_id>"
        )
        return

    rows = []
    for flight_id in sorted(flights):
        flight = flights[flight_id]
        folder = settings.flight_raw(flight_id)
        n_images = (
            sum(1 for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg"))
            if folder.is_dir()
            else 0
        )
        rows.append(
            (
                flight_id,
                flight.field_id,
                str(flight.flown_on or "-"),
                flight.crop or "-",
                str(n_images) if n_images else "no images",
            )
        )
    click.echo(_table(("FLIGHT", "FIELD", "DATE", "CROP", "IMAGES"), rows, "<<<<>"))


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


@cli.command("survey")
@click.argument("flight_id")
@click.option("--folder", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Image folder  [default: data/raw/<flight_id>]")
@click.option("--no-quicklook", is_flag=True, help="Skip the coverage PNG.")
@click.pass_obj
def survey_cmd(
    settings: Settings, flight_id: str, folder: Path | None, no_quicklook: bool
) -> None:
    """Validate a flight's images and report whether ODM is worth running."""
    folder = folder or settings.flight_raw(flight_id)
    flight = None
    try:
        flight = get_flight(settings.manifest_path, flight_id)
    except ManifestError as exc:
        click.secho(f"NOTE: {exc}", fg="yellow")

    try:
        survey = survey_flight(
            flight_id, folder,
            ground_elevation_m=flight.ground_elevation_m if flight else None,
        )
    except IngestError as exc:
        raise click.ClickException(str(exc)) from exc

    settings.ensure_dirs(flight_id)
    field = _load_field(settings, flight.field_id if flight else None)
    coverage = None
    if field is not None and survey.alt_mean_m is not None:
        ground = flight.ground_elevation_m if flight and flight.ground_elevation_m else None
        agl = survey.alt_mean_m - (ground if ground is not None else 0.0)
        coverage = estimate_coverage(survey, field, agl)

    click.echo(_format_survey(survey, flight.field_id if flight else None))
    if coverage is not None:
        click.echo()
        click.echo(_format_coverage(coverage))

    if not no_quicklook:
        path = viz.save_quicklook(
            survey, settings.flight_out(flight_id) / "quicklook.png",
            field=field, field_label=flight.field_id if flight else None,
        )
        click.echo(f"\nQuicklook: {path}")

    _report_warnings(survey)
    (settings.flight_out(flight_id) / "survey.json").write_text(
        json.dumps(_survey_payload(survey, flight, coverage), indent=2), encoding="utf-8"
    )
    if not survey.usable:
        raise SystemExit(1)


def _load_field(settings: Settings, field_id: str | None):
    """Fetch one field polygon from the satellite project, if available."""
    if not field_id or not settings.fields_geojson.exists():
        if field_id:
            log.warning(
                "satellite fields file not found at %s; the quicklook will have no "
                "field outline to check coverage against", settings.fields_geojson,
            )
        return None
    import geopandas as gpd

    frame = gpd.read_file(settings.fields_geojson)
    match = frame[frame["id"].astype(str) == field_id]
    if match.empty:
        log.warning("field %s is not in %s", field_id, settings.fields_geojson)
        return None
    return match.geometry.iloc[0]


def _format_survey(survey: FlightSurvey, field_id: str | None) -> str:
    """Render the survey as an aligned key/value block."""
    def fmt(value, spec: str = "", suffix: str = "") -> str:
        return "unknown" if value is None else f"{value:{spec}}{suffix}"

    bounds = survey.bounds_wgs84
    rows = [
        ("field", field_id or "not registered"),
        ("images", f"{survey.n_images}  ({survey.n_with_gps} with GPS)"),
        ("camera", survey.shots[0].camera or "unknown"),
        ("resolution", f"{survey.shots[0].width_px} x {survey.shots[0].height_px} px"),
        ("focal length", fmt(survey.shots[0].focal_mm, ".1f", " mm")),
        ("sensor width", fmt(survey.shots[0].sensor_width_mm, ".2f", " mm")),
        ("altitude", fmt(survey.alt_mean_m, ".0f", " m")),
        ("altitude spread", fmt(survey.alt_variation, ".1%")),
        ("GSD", fmt(survey.gsd_cm, ".2f", " cm/px")),
        ("footprint", f"{survey.footprint_m[0]:.0f} x {survey.footprint_m[1]:.0f} m"
                      if survey.footprint_m else "unknown"),
        ("shot spacing", fmt(survey.mean_spacing_m, ".1f", " m")),
        ("forward overlap", fmt(survey.forward_overlap, ".0%")),
        ("duration", fmt(survey.duration_s, ".0f", " s")),
        ("bounds", f"{bounds[0]:.5f}, {bounds[1]:.5f} to {bounds[2]:.5f}, {bounds[3]:.5f}"
                   if bounds else "unknown"),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}}  {value}" for label, value in rows)


def _report_warnings(survey: FlightSurvey) -> None:
    """Print warnings, separating the ones that should stop a run."""
    if not survey.warnings:
        click.echo()
        click.secho("No problems found; this flight is worth sending to ODM.", fg="green")
        return

    blockers = [w for w in survey.warnings if w.startswith("BLOCKER")]
    advisories = [w for w in survey.warnings if not w.startswith("BLOCKER")]

    if advisories:
        click.echo()
        for warning in advisories:
            click.secho(f"WARNING: {warning}", fg="yellow")
    if blockers:
        click.echo()
        for warning in blockers:
            click.secho(warning.replace("BLOCKER: ", "BLOCKER: "), fg="red")
        click.echo()
        click.secho(
            "Running ODM on this set would most likely waste hours. Fix the above "
            "or re-fly before continuing.", fg="red",
        )


def _format_coverage(coverage) -> str:
    """Report how much of the field the survey reaches, and what full coverage costs."""
    lines = [
        f"  coverage         {coverage.covered_fraction:.0%} of the registered field "
        f"({coverage.covered_area_m2 / 4046.86:.1f} of "
        f"{coverage.field_area_m2 / 4046.86:.1f} acres)"
    ]
    if not coverage.complete:
        lines.append(
            f"  full coverage    about {coverage.images_for_full_coverage} images at "
            "this altitude"
        )
        if coverage.altitude_for_full_coverage_m:
            lines.append(
                f"                   or roughly "
                f"{coverage.altitude_for_full_coverage_m:.0f} m AGL to cover it with "
                "the images you have"
            )
    return "\n".join(lines)


def _survey_payload(survey: FlightSurvey, flight: Flight | None, coverage=None) -> dict:
    """The survey as JSON, so later steps need not re-read every EXIF header."""
    return {
        "flight_id": survey.flight_id,
        "field_id": flight.field_id if flight else None,
        "folder": str(survey.folder),
        "n_images": survey.n_images,
        "n_with_gps": survey.n_with_gps,
        "bounds_wgs84": survey.bounds_wgs84,
        "gsd_cm": survey.gsd_cm,
        "footprint_m": survey.footprint_m,
        "mean_spacing_m": survey.mean_spacing_m,
        "forward_overlap": survey.forward_overlap,
        "alt_mean_m": survey.alt_mean_m,
        "alt_variation": survey.alt_variation,
        "duration_s": survey.duration_s,
        "camera": survey.shots[0].camera,
        "usable": survey.usable,
        "warnings": survey.warnings,
        "coverage": (
            {
                "covered_fraction": coverage.covered_fraction,
                "field_area_m2": coverage.field_area_m2,
                "images_for_full_coverage": coverage.images_for_full_coverage,
                "altitude_for_full_coverage_m": coverage.altitude_for_full_coverage_m,
            }
            if coverage is not None
            else None
        ),
    }


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


if __name__ == "__main__":  # pragma: no cover
    cli()


@cli.command("ingest-video")
@click.argument("flight_id")
@click.option("--video", "video_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="MP4 recorded by the drone.")
@click.option("--srt", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="DJI SRT sidecar carrying per-frame GPS.")
@click.option("--fps", type=float, default=video.DEFAULT_FPS, show_default=True,
              help="Frames per second to extract.")
@click.option("--blur-quantile", type=float, default=video.DEFAULT_BLUR_QUANTILE,
              show_default=True, help="Drop this fraction as the blurriest.")
@click.option("--blur-threshold", type=float, default=None,
              help="Absolute variance-of-Laplacian cut-off, overriding the quantile.")
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Frame folder  [default: data/raw/<flight_id>]")
@click.pass_obj
def ingest_video_cmd(
    settings: Settings,
    flight_id: str,
    video_path: Path,
    srt: Path | None,
    fps: float,
    blur_quantile: float,
    blur_threshold: float | None,
    out_dir: Path | None,
) -> None:
    """Turn an MP4 plus DJI SRT into geotagged JPEGs. Secondary to shooting stills."""
    out_dir = out_dir or settings.flight_raw(flight_id)
    click.secho(
        "Video is the fallback path: frames are compressed, rolling-shutter "
        "distorted and motion blurred, so expect a worse reconstruction than "
        "stills would give.", fg="yellow",
    )
    if srt is None:
        click.secho(
            "No SRT given, so frames will carry no GPS and nothing will be "
            "georeferenced.", fg="yellow",
        )

    try:
        result = video.video_to_frames(
            flight_id, video_path, srt, out_dir,
            fps=fps, blur_quantile=blur_quantile,
            absolute_blur_threshold=blur_threshold,
        )
    except video.VideoError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo()
    click.echo(_format_video_ingest(result))
    click.echo(
        f"\n{len(result.kept)} frame(s) written to {result.out_dir}\n"
        f"Next: dosojos-drone survey {flight_id}"
    )


def _format_video_ingest(result) -> str:
    """Summarise extraction, with a breakdown of why frames were dropped."""
    dropped = [f for f in result.frames if not f.kept]
    reasons: dict[str, int] = {}
    for frame in dropped:
        key = frame.reason.split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1

    sharp = [f.sharpness for f in result.frames]
    lines = [
        f"  video            {result.video.name}",
        f"  telemetry        {result.srt.name if result.srt else 'none'}",
        f"  extracted        {len(result.frames)} frame(s) at {result.fps:g} fps",
        f"  sharpness        min {min(sharp):.0f}, median "
        f"{sorted(sharp)[len(sharp)//2]:.0f}, max {max(sharp):.0f}",
        f"  cut-off          {result.blur_threshold:.0f}",
        f"  kept             {len(result.kept)}",
    ]
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"  dropped          {count} - {reason}")
    return "\n".join(lines)
