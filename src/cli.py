"""Click command line interface for the Dos Ojos drone pipeline."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

import click
import numpy as np

from . import chm as chm_mod
from . import crowns
from . import flags as flags_mod
from . import metrics as metrics_mod
from . import odm_runner, video, viz
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


# --------------------------------------------------------------------------- #
# ODM
# --------------------------------------------------------------------------- #


@cli.command("doctor")
@click.option("--images", type=int, default=None,
              help="Check whether this machine can handle N images.")
def doctor_cmd(images: int | None) -> None:
    """Check Docker, memory and the ODM image before committing to a run."""
    status = odm_runner.check_docker()
    rows = [
        ("docker", "yes" if status.available else "NO"),
        ("version", status.version or "-"),
        ("container memory", f"{status.memory_gb:.1f} GB" if status.memory_gb else "-"),
        ("container cpus", str(status.cpus) if status.cpus else "-"),
        ("odm image", "present" if status.has_image else "NOT PULLED"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        click.echo(f"  {label:<{width}}  {value}")

    problems = list(status.problems)
    if status.available and not status.has_image:
        problems.append(
            f"the ODM image is not pulled yet. Run: docker pull {odm_runner.ODM_IMAGE}"
        )
    if images:
        problems.extend(odm_runner.memory_advice(images, status.memory_bytes))

    click.echo()
    if problems:
        for problem in problems:
            click.secho(f"WARNING: {problem}", fg="yellow")
    else:
        click.secho("Ready to run ODM.", fg="green")


@cli.command("odm")
@click.argument("flight_id")
@click.option("--orthophoto-resolution", type=float, default=2.0, show_default=True,
              help="Orthophoto resolution in cm/pixel.")
@click.option("--dem-resolution", type=float, default=5.0, show_default=True,
              help="DSM and DTM resolution in cm/pixel.")
@click.option("--feature-quality", default="medium", show_default=True,
              type=click.Choice(["ultra", "high", "medium", "low", "lowest"]),
              help="Feature extraction detail.")
@click.option("--pc-quality", default="medium", show_default=True,
              type=click.Choice(["ultra", "high", "medium", "low", "lowest"]),
              help="Point cloud density. The main driver of memory and time.")
@click.option("--max-concurrency", type=int, default=None,
              help="Parallel workers  [default: ODM decides from container CPUs]")
@click.option("--rerun-from", default=None,
              type=click.Choice(list(odm_runner.ODM_STAGES)),
              help="Resume from a stage instead of starting over.")
@click.option("--fast-orthophoto", is_flag=True,
              help="Skip meshing for a quicker, rougher orthophoto.")
@click.option("--dry-run", is_flag=True,
              help="Stage images and print the command without running it.")
@click.pass_obj
def odm_cmd(
    settings: Settings,
    flight_id: str,
    orthophoto_resolution: float,
    dem_resolution: float,
    feature_quality: str,
    pc_quality: str,
    max_concurrency: int | None,
    rerun_from: str | None,
    fast_orthophoto: bool,
    dry_run: bool,
) -> None:
    """Run OpenDroneMap on a flight, producing orthophoto, DSM, DTM and point cloud."""
    config = odm_runner.OdmConfig(
        orthophoto_resolution_cm=orthophoto_resolution,
        dem_resolution_cm=dem_resolution,
        feature_quality=feature_quality,
        pc_quality=pc_quality,
        max_concurrency=max_concurrency,
        rerun_from=rerun_from,
        fast_orthophoto=fast_orthophoto,
    )
    settings.ensure_dirs(flight_id)
    raw_dir = settings.flight_raw(flight_id)

    if dry_run:
        try:
            config.validate()
            staged = odm_runner.stage_images(raw_dir, settings.flight_odm(flight_id))
        except odm_runner.OdmError as exc:
            raise click.ClickException(str(exc)) from exc
        command = odm_runner.build_command(settings.odm_dir, flight_id, config)
        click.echo(f"Staged {staged} image(s). Command:\n")
        click.echo(odm_runner.as_shell(command))
        return

    click.echo(
        "ODM will take a while: expect hours on a laptop for a few hundred images. "
        "The full command is written to the project folder if you want to run it "
        "by hand.\n"
    )
    try:
        result = odm_runner.run_odm(flight_id, raw_dir, settings.odm_dir, config)
    except odm_runner.OdmError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo()
    click.echo(_format_odm_result(result))
    if not result.ok:
        raise SystemExit(1)


def _format_odm_result(result) -> str:
    """Report what a run produced, or where it broke and what to check."""
    minutes = result.duration_s / 60
    lines = [
        f"  flight           {result.flight_id}",
        f"  duration         {minutes:.1f} min",
        f"  exit code        {result.returncode}",
        f"  log              {result.log_path}",
    ]
    for name in odm_runner.EXPECTED_OUTPUTS:
        path = result.outputs.get(name)
        lines.append(f"  {name:<16} {path if path else 'MISSING'}")

    if result.ok:
        lines.append("")
        lines.append("  All products present and georeferenced.")
        return "\n".join(lines)

    lines.append("")
    if result.failed_stage:
        lines.append(f"  FAILED during the '{result.failed_stage}' stage.")
    else:
        lines.append("  FAILED before any stage completed.")
    if result.advice:
        lines.append(f"  {result.advice}")
    lines.append(f"  Full log: {result.log_path}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Canopy height model
# --------------------------------------------------------------------------- #


@cli.command("chm")
@click.argument("flight_id")
@click.option("--fill-holes", type=float, default=chm_mod.DEFAULT_FILL_HOLES_M2,
              show_default=True,
              help="Fill nodata patches up to this area in m2; larger voids stay empty.")
@click.option("--smooth", type=float, default=chm_mod.DEFAULT_SMOOTH_M, show_default=True,
              help="Gaussian smoothing radius in metres. 0 disables it.")
@click.option("--max-height", type=float, default=chm_mod.DEFAULT_MAX_HEIGHT_M,
              show_default=True, help="Clip canopy taller than this, in metres.")
@click.option("--keep-negative", is_flag=True,
              help="Keep sub-ground values instead of clamping them to zero.")
@click.option("--clip-field/--no-clip-field", default=True, show_default=True,
              help="Mask everything outside the registered field outline.")
@click.pass_obj
def chm_cmd(
    settings: Settings,
    flight_id: str,
    fill_holes: float,
    smooth: float,
    max_height: float,
    keep_negative: bool,
    clip_field: bool,
) -> None:
    """Build the canopy height model from a flight's DSM and DTM."""
    project = settings.flight_odm(flight_id)
    out_dir = settings.flight_out(flight_id)
    settings.ensure_dirs(flight_id)

    dsm_path = project / "odm_dem" / "dsm.tif"
    dtm_path = project / "odm_dem" / "dtm.tif"
    for path, name in ((dsm_path, "DSM"), (dtm_path, "DTM")):
        if not path.exists():
            raise click.ClickException(
                f"{name} not found at {path}. Run 'dosojos-drone odm {flight_id}' "
                "first, or check that the run produced DEMs."
            )

    try:
        dsm = chm_mod.load_surface(dsm_path)
        dtm = chm_mod.load_surface(dtm_path)
        canopy, stats = chm_mod.compute_chm(
            dsm, dtm,
            clamp_negative=not keep_negative,
            fill_holes_m2=fill_holes,
            smooth_m=smooth,
            max_height_m=max_height,
        )
    except chm_mod.ChmError as exc:
        raise click.ClickException(str(exc)) from exc

    flight = None
    try:
        flight = get_flight(settings.manifest_path, flight_id)
    except ManifestError:
        pass

    n_in_field = None
    if clip_field and flight:
        field = _load_field(settings, flight.field_id)
        if field is not None:
            canopy, n_in_field = chm_mod.clip_to_field(
                canopy, dsm.transform, dsm.crs, field
            )

    tif = chm_mod.save_chm(canopy, dsm, out_dir / "chm.tif")
    png = viz.save_chm_png(
        canopy, out_dir / "chm.png",
        resolution_m=stats.resolution_m,
        title=f"{flight_id} - canopy height",
        subtitle=(
            f"{stats.resolution_m * 100:.0f} cm/px  -  mean {stats.mean_m:.2f} m  -  "
            f"95th percentile {stats.p95_m:.2f} m"
            + (f"  -  field {flight.field_id}" if flight else "")
        ),
    )

    click.echo(_format_chm(stats, n_in_field))
    click.echo(f"\n  {tif}\n  {png}")


def _format_chm(stats, n_in_field: int | None) -> str:
    """Report the canopy model's shape, coverage and cleaning actions."""
    rows = [
        ("grid", f"{stats.shape[0]} x {stats.shape[1]} px at "
                 f"{stats.resolution_m * 100:.0f} cm"),
        ("coverage", f"{stats.coverage:.1%} of the grid has data"),
        ("mean height", f"{stats.mean_m:.2f} m"),
        ("median height", f"{stats.median_m:.2f} m"),
        ("95th percentile", f"{stats.p95_m:.2f} m"),
        ("max height", f"{stats.max_m:.2f} m"),
        ("holes filled", f"{stats.n_filled} px"),
        ("negatives clamped", f"{stats.n_clipped_negative} px"),
        ("tall pixels clipped", f"{stats.n_clipped_tall} px"),
    ]
    if n_in_field is not None:
        rows.append(("inside field", f"{n_in_field} px"))
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}}  {value}" for label, value in rows)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


@cli.command("detect")
@click.argument("flight_id")
@click.option("--method", type=click.Choice(list(crowns.METHODS)), default="rows",
              show_default=True,
              help="rows for sorghum and cane, watershed for orchards, "
                   "deepforest for citrus from RGB.")
@click.option("--segment", type=float, default=crowns.DEFAULT_SEGMENT_M,
              show_default=True, help="Row segment length in metres.")
@click.option("--row-width", type=float, default=None,
              help="Segment width in metres  [default: the detected row spacing]")
@click.option("--min-spacing", type=float, default=crowns.DEFAULT_MIN_SPACING_M,
              show_default=True, help="Smallest row spacing to search for.")
@click.option("--max-spacing", type=float, default=crowns.DEFAULT_MAX_SPACING_M,
              show_default=True, help="Largest row spacing to search for.")
@click.option("--min-height", type=float, default=crowns.DEFAULT_MIN_CROWN_HEIGHT_M,
              show_default=True, help="Watershed: canopy below this is ground.")
@click.option("--min-distance", type=float, default=1.5, show_default=True,
              help="Watershed: minimum metres between crown peaks.")
@click.option("--clip-field/--no-clip-field", default=True, show_default=True,
              help="Drop units falling mostly outside the field.")
@click.pass_obj
def detect_cmd(
    settings: Settings,
    flight_id: str,
    method: str,
    segment: float,
    row_width: float | None,
    min_spacing: float,
    max_spacing: float,
    min_height: float,
    min_distance: float,
    clip_field: bool,
) -> None:
    """Detect the units later steps measure: row segments or crowns."""
    out_dir = settings.flight_out(flight_id)
    settings.ensure_dirs(flight_id)
    chm_path = out_dir / "chm.tif"
    if not chm_path.exists():
        raise click.ClickException(
            f"no canopy model at {chm_path}. Run 'dosojos-drone chm {flight_id}' first."
        )

    surface = chm_mod.load_surface(chm_path)
    resolution = float(surface.resolution_m[0])
    geometry = None

    try:
        if method == "rows":
            geometry = crowns.estimate_row_geometry(
                surface.data, resolution,
                min_spacing_m=min_spacing, max_spacing_m=max_spacing,
            )
            units = crowns.build_row_segments(
                surface.data, resolution, geometry,
                segment_m=segment, width_m=row_width,
            )
        elif method == "watershed":
            units = crowns.detect_crowns_watershed(
                surface.data, resolution,
                min_height_m=min_height, min_distance_m=min_distance,
            )
        else:
            raise click.ClickException(
                "deepforest runs on the orthophoto, not the canopy model, and is "
                "not wired into this command yet. For row crops use --method rows."
            )
    except crowns.DetectionError as exc:
        raise click.ClickException(str(exc)) from exc

    frame = crowns.to_geodataframe(
        units, surface.transform, surface.crs, method=method, geometry=geometry
    )

    flight = None
    try:
        flight = get_flight(settings.manifest_path, flight_id)
    except ManifestError:
        pass
    if clip_field and flight:
        field = _load_field(settings, flight.field_id)
        if field is not None:
            frame = crowns.clip_units(frame, field)

    if frame.empty:
        raise click.ClickException("no units survived clipping to the field outline")

    path = out_dir / f"units_{method}.geojson"
    frame.to_file(path, driver="GeoJSON")

    overlay = viz.save_units_overlay(
        surface.data, frame, out_dir / f"units_{method}.png",
        transform=surface.transform, resolution_m=resolution,
        title=f"{flight_id} - detected "
              f"{'row segments' if method == 'rows' else 'crowns'}",
        subtitle=(
            f"{len(frame)} units"
            + (
                f"  -  rows {geometry.spacing_m:.2f} m apart at "
                f"{geometry.direction_deg:.0f} deg"
                if geometry
                else ""
            )
        ),
    )

    click.echo(_format_detection(frame, geometry, method, resolution))
    click.echo(f"\n  {path}\n  {overlay}")


def _format_detection(frame, geometry, method: str, resolution: float) -> str:
    """Report what was detected and, for rows, the planting pattern behind it."""
    areas = frame.geometry.area
    rows = [
        ("method", method),
        ("units", str(len(frame))),
        ("resolution", f"{resolution * 100:.0f} cm/px"),
        ("median unit area", f"{areas.median():.2f} m2"),
        ("total area", f"{areas.sum():.0f} m2 ({areas.sum() / 4046.86:.2f} acres)"),
    ]
    if geometry is not None:
        rows[2:2] = [
            ("row spacing", f"{geometry.spacing_m:.3f} m"),
            ("row direction", f"{geometry.direction_deg:.1f} deg (grid)"),
            ("signal strength", f"{geometry.strength:.1f}"
                                f"{'' if geometry.confident else '  <- weak'}"),
            ("rows found", str(frame['row'].nunique()) if 'row' in frame else "-"),
        ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}}  {value}" for label, value in rows)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


@cli.command("metrics")
@click.argument("flight_id")
@click.option("--method", type=click.Choice(list(crowns.METHODS)), default="rows",
              show_default=True, help="Which detection to measure.")
@click.option("--cover-height", type=float, default=metrics_mod.DEFAULT_COVER_HEIGHT_M,
              show_default=True, help="Canopy below this counts as ground for cover.")
@click.pass_obj
def metrics_cmd(
    settings: Settings, flight_id: str, method: str, cover_height: float
) -> None:
    """Measure every detected unit: area, height, volume and RGB stress indices."""
    out_dir = settings.flight_out(flight_id)
    units_path = out_dir / f"units_{method}.geojson"
    chm_path = out_dir / "chm.tif"
    for path, hint in ((chm_path, "chm"), (units_path, f"detect --method {method}")):
        if not path.exists():
            raise click.ClickException(
                f"{path.name} not found. Run 'dosojos-drone {hint} {flight_id}' first."
            )

    import geopandas as gpd

    units = gpd.read_file(units_path)
    surface = chm_mod.load_surface(chm_path)
    ortho = settings.flight_odm(flight_id) / "odm_orthophoto" / "odm_orthophoto.tif"

    try:
        measured = metrics_mod.compute_metrics(
            units, surface.data, surface.transform,
            ortho_path=ortho if ortho.exists() else None,
            cover_height_m=cover_height,
        )
    except metrics_mod.MetricsError as exc:
        raise click.ClickException(str(exc)) from exc

    geojson = out_dir / f"metrics_{method}.geojson"
    csv = out_dir / f"metrics_{method}.csv"
    measured.to_file(geojson, driver="GeoJSON")
    measured.drop(columns="geometry").to_csv(csv, index=False)

    summary = metrics_mod.summarise(measured)
    click.echo(_format_metrics(summary, measured, bool(ortho.exists())))
    click.echo("")
    click.echo(f"  {geojson}")
    click.echo(f"  {csv}")


def _format_metrics(summary: dict, measured, has_colour: bool) -> str:
    """Report flight-level figures and the spread of each measure."""
    rows = [
        ("units", f"{int(summary['n_units'])}"),
        ("total volume", f"{summary['total_volume_m3']:.1f} m3"),
        ("median volume", f"{summary['median_volume_m3']:.3f} m3 per unit"),
        ("median height", f"{summary['median_height_m']:.2f} m"),
        ("median cover", f"{summary['median_cover']:.0%}"),
    ]
    if has_colour:
        rows.append(("mean ExG", f"{summary['mean_exg']:.3f}"))
    else:
        rows.append(("RGB indices", "none - no orthophoto"))

    empty = int((measured["n_pixels"] == 0).sum())
    if empty:
        rows.append(("empty units", f"{empty} (slivers under one pixel)"))
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}}  {value}" for label, value in rows)


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #


FLAG_COLOURS = {
    "HEALTHY": "#199e70",
    "STRESSED": "#eda100",
    "DEAD": "#d03b3b",
    "MISSING": "#6250d6",
    "NO_DATA": "#888780",
}


@cli.command("flag")
@click.argument("flight_id")
@click.option("--method", type=click.Choice(list(crowns.METHODS)), default="rows",
              show_default=True, help="Which detection to flag.")
@click.option("--segment", type=float, default=crowns.DEFAULT_SEGMENT_M,
              show_default=True, help="Row segment length used at detection.")
@click.option("--stressed-quantile", type=float,
              default=flags_mod.DEFAULT_STRESSED_QUANTILE, show_default=True,
              help="Bottom share of the field considered for STRESSED.")
@click.option("--stressed-min-shortfall", type=float,
              default=flags_mod.DEFAULT_STRESSED_MIN_SHORTFALL, show_default=True,
              help="Also require this share below the field median. 0 disables it.")
@click.option("--stressed-min-z", type=float, default=flags_mod.DEFAULT_STRESSED_MIN_Z,
              show_default=True,
              help="Also require this many robust sd below the median. 0 disables it.")
@click.pass_obj
def flag_cmd(
    settings: Settings,
    flight_id: str,
    method: str,
    segment: float,
    stressed_quantile: float,
    stressed_min_shortfall: float,
    stressed_min_z: float,
) -> None:
    """Classify units as HEALTHY, STRESSED, DEAD or MISSING, and find absent plants."""
    import geopandas as gpd
    import pandas as pd

    out_dir = settings.flight_out(flight_id)
    metrics_path = out_dir / f"metrics_{method}.geojson"
    if not metrics_path.exists():
        raise click.ClickException(
            f"{metrics_path.name} not found. Run "
            f"'dosojos-drone metrics {flight_id} --method {method}' first."
        )

    rules = flags_mod.FlagRules(
        stressed_quantile=stressed_quantile,
        stressed_min_shortfall=stressed_min_shortfall or None,
        stressed_min_z=stressed_min_z or None,
    )
    measured = gpd.read_file(metrics_path)
    try:
        flagged = flags_mod.classify_units(measured, method=method, rules=rules)
    except flags_mod.FlagError as exc:
        raise click.ClickException(str(exc)) from exc

    missing_frame = None
    n_missing_positions = 0
    if method == "rows":
        missing_frame = flags_mod.gap_runs(flagged, segment_m=segment)
    else:
        centroids = np.array([[g.centroid.x, g.centroid.y] for g in flagged.geometry])
        try:
            grid = flags_mod.infer_planting_grid(centroids)
            positions = flags_mod.find_missing_positions(grid, centroids)
        except flags_mod.FlagError as exc:
            click.secho(f"WARNING: no missing-tree search: {exc}", fg="yellow")
            positions = np.empty((0, 2))
        n_missing_positions = len(positions)
        missing_frame = gpd.GeoDataFrame(
            {"kind": ["missing"] * len(positions)},
            geometry=gpd.points_from_xy(positions[:, 0], positions[:, 1]),
            crs=flagged.crs,
        )

    flagged.to_file(out_dir / f"flags_{method}.geojson", driver="GeoJSON")
    missing_path = out_dir / f"missing_{method}.geojson"
    if missing_frame is not None and not missing_frame.empty:
        missing_frame.to_file(missing_path, driver="GeoJSON")

    summary = flags_mod.summarise_flags(flagged, n_missing_positions=n_missing_positions)
    pd.DataFrame([{"flight_id": flight_id, "method": method, **summary}]).to_csv(
        out_dir / f"flags_{method}_summary.csv", index=False
    )

    click.echo(_format_flags(summary, method, missing_frame))
    click.echo("")
    for name in (f"flags_{method}.geojson", f"flags_{method}_summary.csv"):
        click.echo(f"  {out_dir / name}")
    if missing_frame is not None and not missing_frame.empty:
        click.echo(f"  {missing_path}")


def _format_flags(summary: dict, method: str, missing_frame) -> str:
    """Report the count and share of every flag."""
    lines = []
    for flag in flags_mod.FLAGS:
        key = flag.lower()
        lines.append(
            f"  {flag:<9} {summary[f'n_{key}']:>6}   {summary[f'share_{key}']:6.1%}"
        )
    if summary.get("n_no_data"):
        lines.append(f"  {'NO_DATA':<9} {summary['n_no_data']:>6}")
    if method == "rows" and missing_frame is not None and not missing_frame.empty:
        lines.append("")
        lines.append(
            f"  {len(missing_frame)} gap(s) along rows, "
            f"{missing_frame['length_m'].sum():.0f} m in total, "
            f"longest {missing_frame['length_m'].max():.0f} m"
        )
    elif method != "rows":
        n = 0 if missing_frame is None else len(missing_frame)
        lines.append("")
        lines.append(f"  {n} empty position(s) in the inferred planting grid")
    return "\n".join(lines)
