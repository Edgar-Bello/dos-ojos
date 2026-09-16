"""Click command line interface for the Dos Ojos drone pipeline."""

from __future__ import annotations

import json
import logging
import sys
import textwrap
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import click
import numpy as np

from . import chm as chm_mod
from . import crowns
from . import flags as flags_mod
from . import report as report_mod
from . import metrics as metrics_mod
from . import odm_runner, video, viz
from . import terrain as terrain_mod
from . import thermal as thermal_mod
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


def _public_banner(flight: Flight | None) -> str | None:
    """The band printed on every figure of a flight that is not ours.

    Borrowed public data and our own synthetic test fields are both marked, and
    worded differently so neither is taken for the other.
    """
    if flight is None or not flight.source:
        return None
    if flight.source.lower().startswith("synthetic"):
        detail = flight.source[len("synthetic"):].lstrip(" :,-")
        return "SYNTHETIC TEST DATA, NOT A REAL FIELD" + (f"  -  {detail}" if detail else "")
    return f"FREE PUBLIC DATA, NOT OUR FLIGHT  -  {flight.source}"


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
@click.option("--workspace", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Keep flights.json, data/ and out/ here instead of the project "
                   "folder, e.g. to keep public demo data apart from your own.")
@click.option("-v", "--verbose", count=True, help="Show debug logging.")
@click.pass_context
def cli(ctx: click.Context, workspace: Path | None, verbose: int) -> None:
    """Dos Ojos - drone post-processing for Rio Grande Valley fields."""
    setup_logging(verbose)
    root = workspace.resolve() if workspace else default_root()
    if workspace:
        root.mkdir(parents=True, exist_ok=True)
        log.info("workspace %s", root)
    ctx.obj = Settings.from_root(root)


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
@click.option("--source", default=None,
              help="Origin of data that is not your own flight, e.g. a public dataset. "
                   "Printed on every figure.")
@click.option("--row-spacing", type=float, default=None,
              help="Planter row spacing in metres (30 in = 0.762, 40 in = 1.016, "
                   "5 ft cane = 1.524). Used by 'detect' so it need not guess.")
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
    source: str | None,
    row_spacing: float | None,
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
        source=source,
        row_spacing_m=row_spacing,
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


def _median_or_none(values) -> float | None:
    found = sorted(v for v in values if v is not None)
    return found[len(found) // 2] if found else None


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
        ("flying height", fmt(_median_or_none(s.relative_alt_m for s in survey.shots),
                              ".0f", " m above takeoff")),
        ("GPS altitude", fmt(survey.alt_mean_m, ".0f", " m")),
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
    """Check Docker, memory and the ODM image before committing to a run.

    Exits 1 when Docker or the image is missing, so a script stops here instead
    of letting 'odm' pull a 3-4 GB image nobody asked for.
    """
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
    if not (status.available and status.has_image):
        sys.exit(1)


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


@cli.command("import")
@click.argument("flight_id")
@click.option("--ortho", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Orthomosaic GeoTIFF.")
@click.option("--dsm", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True, help="Surface model: a GeoTIFF, or a LAS/LAZ point cloud.")
@click.option("--dtm", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True,
              help="Ground: a GeoTIFF, or a LAS/LAZ cloud from a bare-soil or early flight.")
@click.option("--crs", default=None,
              help="Coordinate system for inputs that carry none, e.g. EPSG:26916.")
@click.option("--resolution", type=float, default=None,
              help="Grid for point clouds, in metres  [default: from the point spacing]")
@click.option("--crop/--no-crop", default=True, show_default=True,
              help="Crop the surfaces to the orthophoto's footprint.")
@click.option("--force", is_flag=True, help="Replace products already in the project.")
@click.pass_obj
def import_cmd(
    settings: Settings, flight_id: str, ortho: Path | None, dsm: Path, dtm: Path,
    crs: str | None, resolution: float | None, crop: bool, force: bool,
) -> None:
    """Bring in finished maps made elsewhere, in place of an ODM run."""
    from . import external

    project = settings.flight_odm(flight_id)
    try:
        done = external.import_products(
            project, ortho=ortho, dsm=dsm, dtm=dtm, crs=crs, resolution_m=resolution,
            crop_to_ortho=crop, overwrite=force,
        )
    except external.ProductImportError as exc:
        raise click.ClickException(str(exc)) from exc

    found = odm_runner.verify_outputs(project)
    rows = [
        (item.name, f"{item.shape[1]} x {item.shape[0]}",
         f"{item.resolution_m * 100:.1f} cm", item.crs, item.how)
        for item in done
    ]
    click.echo(_table(("PRODUCT", "PIXELS", "GRID", "CRS", "HOW"), rows, "<>><<"))
    click.echo(f"\n  {len(found)} product(s) in {project}, provenance in "
               f"{external.PROVENANCE_NAME}")
    click.echo(f"  Next: dosojos-drone chm {flight_id}")


@cli.command("lidar")
@click.argument("flight_id")
@click.option("--field", "field_id", required=True,
              help="Satellite field id whose outline to read the ground under.")
@click.option("--force", is_flag=True, help="Replace a ground already imported for this flight.")
@click.pass_obj
def lidar_cmd(settings: Settings, flight_id: str, field_id: str, force: bool) -> None:
    """A field's ground from public USGS 3DEP airborne LiDAR, for the terrain check; no drone."""
    from . import external, lidar as lidar_mod

    outline = _field_settings(settings, field_id).get("geometry")
    if outline is None:
        raise click.ClickException(
            f"field {field_id!r} is not in {settings.fields_geojson}; the ground is read "
            "under the field's outline, so the satellite half must know the field first."
        )
    folder = settings.flight_raw(flight_id) / "3dep"
    try:
        ground = lidar_mod.fetch_ground(outline, folder)
    except lidar_mod.LidarError as exc:
        raise click.ClickException(str(exc)) from exc
    covered = lidar_mod.covered_share(ground)
    if covered > lidar_mod.COVERED_REFUSE:
        raise click.ClickException(
            f"when the {ground.project} survey flew in {ground.year}, the laser hit plants "
            f"taller than 1 m over {covered:.0%} of this field (a standing crop such as cane), "
            "so its ground model is mostly guessed and the terrain check would judge the crop, "
            "not the ground. Nothing was registered. A bare-soil drone flight is the way to "
            "map this field."
        )
    source = f"USGS 3DEP lidar {ground.project}, flown {ground.year} (public domain)"
    try:
        register_flight(settings.manifest_path, Flight(
            flight_id=flight_id, field_id=field_id, flown_on=date(ground.year, 1, 1),
            source=source, notes="Ground from airborne lidar, not a drone flight: "
                                 f"{ground.resolution_m:g} m cells, older than the crop."),
            overwrite=force)
        done = external.import_products(
            settings.flight_odm(flight_id), dsm=ground.dsm, dtm=ground.dtm, crop_to_ortho=False,
            overwrite=force, source=source, lidar=True)
    except (ManifestError, external.ProductImportError) as exc:
        raise click.ClickException(str(exc)) from exc
    rows = [(item.name, f"{item.shape[1]} x {item.shape[0]}", f"{item.resolution_m:g} m")
            for item in done]
    click.echo(_table(("PRODUCT", "CELLS", "GRID"), rows, "<>>"))
    click.echo(f"\n  {source}, {ground.tiles} tile(s); read only the field's window.")
    if covered > lidar_mod.COVERED_WARN:
        click.secho(f"  WARNING: the laser hit plants or trees taller than 1 m over {covered:.0%} "
                    "of the field; the ground under them is interpolated, so treat spots and "
                    "leveling there with care (an orchard's beds are not seen at 2 m).",
                    fg="yellow")
    click.echo(f"  Next: dosojos-drone terrain {flight_id}")


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
        banner=_public_banner(flight),
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
@click.option("--spacing", "known_spacing", type=float, default=None,
              help="Row spacing you already know, in metres (30 in = 0.762, 40 in = "
                   "1.016, 5 ft cane = 1.524). Needed once the canopy closes over the rows.")
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
@click.option("--blocks", "blocks_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Polygons of parts judged separately (varieties, planting dates, trial "
                   "plots). Units are cut at their edges; units outside them are dropped.")
@click.option("--block-field", default=None,
              help="Column naming each block  [default: the first attribute]")
@click.pass_obj
def detect_cmd(
    settings: Settings,
    flight_id: str,
    method: str,
    segment: float,
    row_width: float | None,
    known_spacing: float | None,
    min_spacing: float,
    max_spacing: float,
    min_height: float,
    min_distance: float,
    clip_field: bool,
    blocks_path: Path | None,
    block_field: str | None,
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

    flight = None
    try:
        flight = get_flight(settings.manifest_path, flight_id)
    except ManifestError:
        pass
    if known_spacing is None and flight is not None:
        known_spacing = flight.row_spacing_m

    try:
        if method == "rows":
            geometry = crowns.estimate_row_geometry(
                surface.data, resolution,
                min_spacing_m=min_spacing, max_spacing_m=max_spacing,
                known_spacing_m=known_spacing,
            )
            warning = None if known_spacing else crowns.spacing_warning(
                geometry.spacing_m, flight.crop if flight else None
            )
            if warning:
                click.secho(f"WARNING: {warning}", fg="yellow")
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

    if clip_field and flight:
        field = _load_field(settings, flight.field_id)
        if field is not None:
            frame = crowns.clip_units(frame, field)

    if frame.empty:
        raise click.ClickException("no units survived clipping to the field outline")

    if blocks_path is not None:
        import geopandas as gpd

        try:
            frame = crowns.assign_blocks(frame, gpd.read_file(blocks_path),
                                         label_column=block_field)
        except crowns.DetectionError as exc:
            raise click.ClickException(str(exc)) from exc

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
        banner=_public_banner(flight),
    )

    click.echo(_format_detection(frame, geometry, method, resolution, known_spacing))
    click.echo(f"\n  {path}\n  {overlay}")


def _format_detection(
    frame, geometry, method: str, resolution: float, known_spacing: float | None = None
) -> str:
    """Report what was detected and, for rows, the planting pattern behind it."""
    areas = frame.geometry.area
    rows = [
        ("method", method),
        ("units", str(len(frame))),
        ("resolution", f"{resolution * 100:.0f} cm/px"),
        ("median unit area", f"{areas.median():.2f} m2"),
        ("total area", f"{areas.sum():.0f} m2 ({areas.sum() / 4046.86:.2f} acres)"),
    ]
    if "block" in frame:
        rows.append(("blocks", f"{frame['block'].nunique()}, units judged within each"))
    if geometry is not None:
        rows[2:2] = [
            ("row spacing", f"{geometry.spacing_m:.3f} m"
                            + ("  (given)" if known_spacing else "")),
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
@click.option("--within-blocks/--whole-field", default=True, show_default=True,
              help="When detection assigned blocks, judge each unit against its own "
                   "block rather than the whole flight.")
@click.pass_obj
def flag_cmd(
    settings: Settings,
    flight_id: str,
    method: str,
    segment: float,
    stressed_quantile: float,
    stressed_min_shortfall: float,
    stressed_min_z: float,
    within_blocks: bool,
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
    group = "block" if within_blocks and "block" in measured else None
    try:
        flagged = flags_mod.classify_units(measured, method=method, rules=rules,
                                           group_column=group)
    except flags_mod.FlagError as exc:
        raise click.ClickException(str(exc)) from exc
    if group:
        click.echo(f"Judging units within {measured['block'].nunique()} block(s)\n")

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


# --------------------------------------------------------------------------- #
# Demo outputs and the satellite join
# --------------------------------------------------------------------------- #


@cli.command("report")
@click.argument("flight_id")
@click.option("--method", type=click.Choice(list(crowns.METHODS)), default="rows",
              show_default=True, help="Which detection to report on.")
@click.pass_obj
def report_cmd(settings: Settings, flight_id: str, method: str) -> None:
    """Draw the flag overlay and histogram, and write the block summary JSON."""
    import geopandas as gpd

    out_dir = settings.flight_out(flight_id)
    flags_path = out_dir / f"flags_{method}.geojson"
    if not flags_path.exists():
        raise click.ClickException(
            f"{flags_path.name} not found. Run "
            f"'dosojos-drone flag {flight_id} --method {method}' first."
        )

    flagged = gpd.read_file(flags_path)
    missing_path = out_dir / f"missing_{method}.geojson"
    missing_points = None
    n_missing_positions = 0
    if method != "rows" and missing_path.exists():
        missing_points = gpd.read_file(missing_path)
        n_missing_positions = len(missing_points)

    flight = None
    try:
        flight = get_flight(settings.manifest_path, flight_id)
    except ManifestError:
        pass
    field_id = flight.field_id if flight else None
    crop = (flight.crop if flight and flight.crop else "").strip()
    source = flight.source if flight else None
    banner = _public_banner(flight)
    within = "block" if "block" in flagged else None

    ortho = settings.flight_odm(flight_id) / "odm_orthophoto" / "odm_orthophoto.tif"
    summary = report_mod.block_summary(
        flagged, flight_id=flight_id, field_id=field_id, method=method,
        flown_on=flight.flown_on.isoformat() if flight and flight.flown_on else None,
        n_missing_positions=n_missing_positions, source=source,
    )
    problems = summary["n_stressed"] + summary["n_dead"] + summary["n_missing"]
    total = summary["n_trees"] + n_missing_positions
    noun = "row segments" if method == "rows" else "trees"
    heading = f"{field_id or flight_id}" + (f" - {crop}" if crop else "")

    try:
        overlay = report_mod.save_flag_overlay(
            flagged, ortho if ortho.exists() else None, out_dir / "flag_overlay.png",
            title=heading,
            subtitle=f"{problems} of {total} {noun} need a look  -  drone flight {flight_id}",
            missing_points=missing_points, banner=banner,
        )
        histogram = report_mod.save_flag_histogram(
            flagged, method, out_dir / "flag_histogram.png",
            title=f"{heading} - where the flagged {noun} sit", banner=banner,
            within=within,
        )
    except report_mod.ReportError as exc:
        raise click.ClickException(str(exc)) from exc

    summary_path = out_dir / "block_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    click.echo(_format_block_summary(summary))
    click.echo("")
    for path in (overlay, histogram, summary_path):
        click.echo(f"  {path}")


def _format_block_summary(summary: dict) -> str:
    """Render the block summary as an aligned key/value block."""
    rows = [
        ("field", summary["field_id"] or "not registered"),
        ("unit", summary["unit_type"]),
        ("units", str(summary["n_trees"])),
        ("healthy", str(summary["n_healthy"])),
        ("stressed", f"{summary['n_stressed']}  ({summary['share_stressed']:.1%})"),
        ("dead", f"{summary['n_dead']}  ({summary['share_dead']:.1%})"),
        ("missing", f"{summary['n_missing']}  ({summary['share_missing']:.1%})"),
        ("median volume", f"{summary['median_canopy_volume']} m3"),
        ("mean ExG", str(summary["mean_ExG"])),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}}  {value}" for label, value in rows)


# --------------------------------------------------------------------------- #
# Terrain and irrigation advice
# --------------------------------------------------------------------------- #

_SIDE_WORDS = {"n": "N", "north": "N", "s": "S", "south": "S",
               "e": "E", "east": "E", "w": "W", "west": "W"}


def _field_settings(settings: Settings, field_id: str | None) -> dict:
    """A field's outline and water settings from the satellite project's fields file.

    Read as plain GeoJSON: the two halves share this file and a field id, and
    nothing else.
    """
    if not field_id or not settings.fields_geojson.exists():
        return {}
    from shapely.geometry import shape

    payload = json.loads(settings.fields_geojson.read_text(encoding="utf-8"))
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        if str(props.get("id")) == field_id:
            side = _SIDE_WORDS.get(str(props.get("water_enters") or "").strip().lower())
            method = str(props.get("irrigation") or "").strip().lower() or None
            return {"geometry": shape(feature["geometry"]), "irrigation": method,
                    "water_enters": side}
    return {}


def _satellite_water(settings: Settings) -> dict:
    """The satellite's water checkbook by field id, or empty if it has not run."""
    if not settings.satellite_water.exists():
        return {}
    payload = json.loads(settings.satellite_water.read_text(encoding="utf-8"))
    return {entry["field_id"]: entry for entry in payload.get("fields", [])}


@cli.command("terrain")
@click.argument("flight_id")
@click.option("--method", type=click.Choice(list(crowns.METHODS)), default="rows",
              show_default=True, help="Whose flags to compare with the ground.")
@click.option("--water-enters", type=click.Choice(["N", "S", "E", "W"], case_sensitive=False),
              default=None,
              help="Side the irrigation water comes in from  [default: the field's "
                   "water_enters, else downhill along the rows]")
@click.option("--cell", type=float, default=terrain_mod.GROUND_CELL_M, show_default=True,
              help="Analysis grid, metres.")
@click.pass_obj
def terrain_cmd(settings: Settings, flight_id: str, method: str, water_enters: str | None,
                cell: float) -> None:
    """Check whether the ground is level and whether the stress follows it; advise how to irrigate."""
    import geopandas as gpd
    import rasterio
    from shapely.geometry import box

    project = settings.flight_odm(flight_id)
    dtm_path = project / "odm_dem" / "dtm.tif"
    if not dtm_path.exists():
        raise click.ClickException(
            f"no ground model at {dtm_path}. Run 'dosojos-drone odm {flight_id}' or "
            "'dosojos-drone import' first."
        )
    out_dir = settings.flight_out(flight_id)
    settings.ensure_dirs(flight_id)
    flight = None
    try:
        flight = get_flight(settings.manifest_path, flight_id)
    except ManifestError:
        pass
    field_id = flight.field_id if flight else None
    field = _field_settings(settings, field_id)

    with rasterio.open(dtm_path) as dataset:
        crs, footprint = dataset.crs, box(*dataset.bounds)
        native = max(abs(dataset.res[0]), abs(dataset.res[1]))
    if native > cell:
        # A finer grid than the ground was measured on would only interpolate it.
        click.echo(f"  ground measured every {native:g} m; judging it on that grid")
        cell = native
    extent = footprint
    if field.get("geometry") is not None:
        outline = gpd.GeoSeries([field["geometry"]], crs=4326).to_crs(crs).iloc[0]
        clipped = outline.intersection(footprint)
        if clipped.is_empty:
            click.secho(f"WARNING: the flight does not overlap field {field_id}; judging the "
                        "whole flight footprint.", fg="yellow")
        else:
            extent = clipped

    flags_path = out_dir / f"flags_{method}.geojson"
    units_path = out_dir / f"units_{method}.geojson"
    flags = gpd.read_file(flags_path).to_crs(crs) if flags_path.exists() else None
    units = flags if flags is not None else (
        gpd.read_file(units_path).to_crs(crs) if units_path.exists() else None)
    extent, judged_over = terrain_mod.cropped_area(extent, units)
    try:
        ground = terrain_mod.load_ground(dtm_path, extent, cell_m=cell)
    except terrain_mod.TerrainError as exc:
        raise click.ClickException(str(exc)) from exc
    canopy = None
    chm_path = out_dir / "chm.tif"
    if chm_path.exists():
        canopy = float(np.nanmedian(chm_mod.load_surface(chm_path).data))
    soil = (_satellite_water(settings).get(field_id or "") or {}).get("soil") or {}

    report = terrain_mod.analyse(
        ground, flight_id=flight_id, field_id=field_id, extent=extent, flags=flags,
        method=field.get("irrigation"),
        water_enters=(water_enters.upper() if water_enters else field.get("water_enters")),
        ground_source=terrain_mod.ground_source(project), canopy_median_m=canopy,
        intake=soil.get("intake"),
    )
    report.notes.insert(0, f"ground judged over {judged_over}")
    json_path = out_dir / "terrain.json"
    json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    relief_path = terrain_mod.write_relief(ground, out_dir / "ground_relief.tif")
    spots_path = terrain_mod.write_spots(report, crs, out_dir / "terrain_spots.geojson")
    map_path = report_mod.save_terrain_map(
        ground, report, flags, extent, out_dir / "terrain.png",
        title=f"{field_id or flight_id} - ground and water",
        banner=_public_banner(flight),
    )

    click.echo(_format_terrain(report))
    click.echo("\nWhat to do")
    for number, item in enumerate(report.advice, start=1):
        mark = {1: "ACT", 2: "NOTE", 3: "OK"}[item.priority]
        click.echo(f"  {number}. [{mark}] {item.finding}")
        click.echo(f"     {item.advice}")
    for note in report.notes:
        click.secho(f"  NOTE: {note}", fg="yellow")
    click.echo("")
    for path in (map_path, json_path, relief_path, spots_path):
        if path is not None:
            click.echo(f"  {path}")


def _format_terrain(report) -> str:
    """The ground in numbers: grade, evenness, and where the flagged pieces bunch."""
    rows = [
        ("ground from", f"{report.ground_source}, {report.cell_m:g} m grid, "
                        f"{report.area_m2 / 4046.86:.1f} acres"),
        ("irrigation", report.method or "not set on the field"),
        ("overall slope", f"{report.slope_pct:.2f}% toward the {report.downhill}"),
    ]
    if report.along_pct is not None:
        label = "along the rows" if report.row_bearing_deg is not None else "along the flow"
        rows.append((label, f"{report.along_pct:.2f}%"
                     + (f", water runs {report.flow} ({report.flow_source})"
                        if report.flow else ", water's direction unknown")))
    if report.cross_pct is not None:
        rows.append(("across the rows", f"{report.cross_pct:.2f}%, low side "
                                        f"{report.cross_low_side}"))
    rows += [
        ("evenness", f"{report.within_tolerance:.0%} within 3 cm of a smooth plane, "
                     f"spread {report.sd_cm:.1f} cm"),
        ("to level", f"{report.cut_m3:,.0f} m3 of cut ({report.cut_yd3_per_acre:,.0f} yd3/acre)"),
        ("spots", ", ".join(f"{s.label} {s.peak_cm:+.0f} cm {s.area_m2:,.0f} m2"
                            for s in report.spots) or "none over 4 cm"),
    ]
    for item in report.links:
        rows.append((item.place, f"{item.share_in:.0%} flagged vs {item.share_out:.0%} "
                                 f"elsewhere (n={item.n_in}, p={item.p_value:.3g})"
                                 + ("  <- linked" if item.linked else "")))
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}}  {value}" for label, value in rows)


@cli.command("thermal")
@click.argument("flight_id")
@click.option("--thermal", "thermal_path", required=True,
              type=click.Path(dir_okay=False, exists=True, path_type=Path),
              help="Radiometric thermal orthophoto (GeoTIFF), in Celsius, Kelvin or "
                   "hundredths of a Kelvin.")
@click.option("--method", type=click.Choice(list(crowns.METHODS)), default="rows",
              show_default=True, help="Whose flags to cross-check the warm patches against.")
@click.option("--water-days", type=int, default=None,
              help="Days of water the field has left  [default: the satellite checkbook's]")
@click.option("--canopy-min", type=float, default=thermal_mod.CANOPY_MIN_M, show_default=True,
              help="Canopy at least this tall is crop; anything lower is soil.")
@click.option("--warm-z", type=float, default=thermal_mod.WARM_Z, show_default=True,
              help="Robust standard deviations above the canopy median to count as warm.")
@click.option("--min-patch", type=float, default=thermal_mod.MIN_PATCH_M2, show_default=True,
              help="Smallest warm patch worth reporting, square metres.")
@click.pass_obj
def thermal_cmd(settings: Settings, flight_id: str, thermal_path: Path, method: str,
                water_days: int | None, canopy_min: float, warm_z: float,
                min_patch: float) -> None:
    """Optional: find canopy running hot on a thermal mosaic and score it for pests.

    Thermal cannot tell a bitten plant from a thirsty one on its own, so each
    warm patch is scored against the ground model, the colour camera's flags and
    the water checkbook. The score ranks patches to walk out to; it diagnoses
    nothing.
    """
    import geopandas as gpd

    out_dir = settings.flight_out(flight_id)
    settings.ensure_dirs(flight_id)
    chm_path = out_dir / "chm.tif"
    if not chm_path.exists():
        raise click.ClickException(
            f"no canopy model at {chm_path}. Run 'dosojos-drone chm {flight_id}' first: "
            "without it there is no way to measure leaves only, and sunlit soil runs far "
            "hotter than any crop."
        )
    flight = None
    try:
        flight = get_flight(settings.manifest_path, flight_id)
    except ManifestError:
        pass
    field_id = flight.field_id if flight else None

    try:
        heat, unit = thermal_mod.load_thermal(thermal_path)
    except (chm_mod.ChmError, thermal_mod.ThermalError) as exc:
        raise click.ClickException(str(exc)) from exc
    canopy = thermal_mod.align(chm_mod.load_surface(chm_path), heat)

    field = _field_settings(settings, field_id)
    outline = None
    if field.get("geometry") is not None:
        outline = gpd.GeoSeries([field["geometry"]], crs=4326).to_crs(heat.crs).iloc[0]
    frame = thermal_mod.frame_of(heat, outline)
    cell_m = max(heat.resolution_m)

    try:
        patches, stats = thermal_mod.find_patches(
            heat.data, canopy, heat.transform, cell_m=cell_m, frame=frame,
            canopy_min_m=canopy_min, warm_z=warm_z, min_patch_m2=min_patch,
        )
    except thermal_mod.ThermalError as exc:
        raise click.ClickException(str(exc)) from exc

    spots_path = out_dir / "terrain_spots.geojson"
    spots: tuple = ()
    if spots_path.exists():
        spots = tuple(gpd.read_file(spots_path).to_crs(heat.crs).itertuples())
        thermal_mod.place_on_ground(patches, spots)

    field_share = None
    flags_path = out_dir / f"flags_{method}.geojson"
    if flags_path.exists() and patches:
        units = gpd.read_file(flags_path).to_crs(heat.crs)
        thermal_mod.place_on_units(patches, units)
        judged = units[~units["flag"].isin(flags_mod.NOT_ASSESSED)]
        if len(judged):
            field_share = round(float(
                judged["flag"].isin(("STRESSED", "DEAD", "MISSING")).sum()) / len(judged), 3)

    if water_days is None:
        status = _satellite_water(settings).get(field_id or "") or {}
        water_days = status.get("days_left")
    evidence = thermal_mod.Evidence(
        water_days=water_days, field_problem_share=field_share, spots=spots,
    )
    report = thermal_mod.build_report(
        flight_id, field_id, unit=unit, cell_m=cell_m, patches=patches, stats=stats,
        evidence=evidence,
        notes=[] if flags_path.exists() else
        [f"no flags_{method}.geojson, so nobody checked whether the colour camera sees "
         "damage in these patches too; run 'flag' first for a better score."],
    )

    json_path = out_dir / "thermal.json"
    json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    patches_path = out_dir / "thermal_patches.geojson"
    if patches:
        gpd.GeoDataFrame([p.to_dict() for p in patches],
                         geometry=[p.geometry for p in patches],
                         crs=heat.crs).to_file(patches_path, driver="GeoJSON")
    # The figure shows leaves only, for the same reason the statistics do.
    leaves = np.where(thermal_mod.canopy_mask(heat.data, canopy, canopy_min), heat.data, np.nan)
    map_path = viz.save_thermal_png(
        leaves, patches, out_dir / "thermal.png", transform=heat.transform,
        title=f"{field_id or flight_id} - canopy temperature",
        subtitle=f"canopy median {report.canopy_median_c:.1f} C, "
                 f"{len(patches)} warm patch(es); the percentage is our own score, not a test",
        banner=_public_banner(flight),
    )

    click.echo(_format_thermal(report))
    for note in report.notes:
        click.secho(f"  NOTE: {note}", fg="yellow")
    click.echo("")
    for path in (map_path, json_path, patches_path if patches else None):
        if path is not None:
            click.echo(f"  {path}")


def _format_thermal(report) -> str:
    """The canopy's temperature, then every warm patch with the signs behind its score."""
    rows = [
        ("thermal read as", f"{report.unit}, {report.cell_m:g} m pixels"),
        ("canopy measured", f"{report.canopy_m2 / 4046.86:.1f} acres"),
        ("canopy temperature", f"{report.canopy_median_c:.1f} C, spread {report.canopy_spread_c:.1f} C"),
        ("running hot", f"{report.warm_share:.1%} of the canopy"),
    ]
    width = max(len(label) for label, _ in rows)
    lines = [f"  {label:<{width}}  {value}" for label, value in rows]
    if not report.patches:
        lines.append("")
        lines.append("  No warm patch big enough to report. Nothing to walk out to.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Warm patches, most worth a look first")
    for number, patch in enumerate(report.patches, start=1):
        lines.append(
            f"  {number}. {patch.chance:.0%} chance it is a pest or disease - "
            f"{patch.where}, {patch.area_m2:,.0f} m2, {patch.above_c:+.1f} C"
        )
        for sign in patch.signs:
            lines.append(f"     - {sign}")
        lines.append("     Go and look at it: the camera cannot name what it is.")
    return "\n".join(lines)


AGREEMENT_TEXT = {
    "confirmed": "CONFIRMED - both eyes see it",
    "not_confirmed": "not confirmed - check for harvest",
    "drone_only": "drone found what satellite missed",
    "both_clear": "clear",
    "satellite_only": "satellite only - no flight yet",
    "drone_only_no_satellite": "drone only - no satellite record",
}


@cli.command("join")
@click.option("--satellite", "satellite_path",
              type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Satellite flags.json  [default: ../dosojos_sat/out/flags.json]")
@click.option("--out", "out_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Joined output  [default: out/triage.json]")
@click.option("--concern", type=float, default=report_mod.DRONE_CONCERN_SHARE,
              show_default=True,
              help="Share of flagged units at which the drone confirms a problem.")
@click.pass_obj
def join_cmd(
    settings: Settings, satellite_path: Path | None, out_path: Path | None, concern: float
) -> None:
    """Merge every flight's block summary into the satellite flags on field_id."""
    satellite_path = satellite_path or settings.satellite_flags
    out_path = out_path or (settings.out_dir / "triage.json")

    try:
        satellite = report_mod.load_satellite_flags(satellite_path)
    except report_mod.ReportError as exc:
        raise click.ClickException(str(exc)) from exc

    summaries = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(settings.out_dir.glob("*/block_summary.json"))
    ]
    if not summaries:
        click.secho(
            "WARNING: no drone block summaries yet; run 'dosojos-drone report' on a "
            "flight first. Writing the satellite ranking on its own.", fg="yellow",
        )

    terrain: dict[str, dict] = {}
    for path in sorted(settings.out_dir.glob("*/terrain.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        flight = next((s for s in summaries if s.get("flight_id") == report.get("flight_id")), {})
        field_id = report.get("field_id")
        current = terrain.get(field_id or "")
        if field_id and (current is None or (flight.get("flown_on") or "")
                         >= (current.get("_flown_on") or "")):
            terrain[field_id] = {**report, "_flown_on": flight.get("flown_on") or ""}

    water = _satellite_water(settings)
    joined = report_mod.join_with_satellite(satellite, summaries, concern=concern,
                                            water=water, terrain=terrain)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(joined, indent=2), encoding="utf-8")

    click.echo(_format_join(joined))
    if water:
        click.echo("")
        click.echo(_format_water_plan(joined))
    click.echo("")
    click.echo(f"  {out_path}")


def _format_water_plan(joined: dict) -> str:
    """Fields in the order they need water: when, how much, and how, where the drone knows."""
    entries = [f for f in joined["fields"] if f.get("water")]
    entries.sort(key=lambda f: f["water"].get("rank") or 999)
    lines = [f"Where to water first (checkbook as of {joined.get('water_as_of')})"]
    for entry in entries:
        w = entry["water"]
        name = str(entry.get("name") or "")[:24]
        if w["status"] == "harvested":
            when = "harvested, nothing needed"
        elif w["days_left"] == 0:
            when = "WATER NOW"
        elif w["days_left"] is None:
            when = "ok for over six weeks"
        else:
            low, high = w["days_range"] or (w["days_left"], w["days_left"])
            when = f"in about {w['days_left']} days ({low}-{high}), by {w['water_by']}"
        amount = ""
        if w.get("refill_net_in") and w["status"] != "harvested":
            amount = f"; about {w['refill_net_in']:.1f} in into the root zone"
            if w.get("refill_gross_in"):
                amount += f", {w['refill_gross_in']:.1f} in delivered by {w['method']}"
        lines.append(f"  {w.get('rank')}. {entry['field_id']}  {name}: {when}{amount}"
                     f"  [confidence {w.get('confidence')}]")
        if w.get("sensitive"):
            lines.append(f"     stage: {w['sensitive']}")
        irrigation = entry.get("irrigation")
        if irrigation:
            for item in irrigation["advice"][:3]:
                lines.append(textwrap.fill(
                    f"ground: {item['finding']} {item['advice']}", 110,
                    initial_indent="     ", subsequent_indent="       "))
    return "\n".join(lines)


def _format_join(joined: dict) -> str:
    """One row per field: satellite verdict, drone verdict, and how they compare."""
    rows = []
    for entry in joined["fields"]:
        drone = entry.get("drone")
        satellite = "-"
        if "score" in entry:
            satellite = f"{entry['score']:.0f}" + (" FLAG" if entry.get("flagged") else "")
        water = entry.get("water")
        rows.append((
            entry["field_id"],
            str(entry.get("name", "-"))[:22],
            satellite,
            "-" if drone is None else (
                f"{drone['share_problem']:.0%} of {drone.get('n_judged') or drone['n_trees']}"
            ),
            _water_cell(water),
            AGREEMENT_TEXT.get(entry["agreement"], entry["agreement"]),
        ))
    return _table(("FIELD", "NAME", "SATELLITE", "DRONE", "WATER", "VERDICT"), rows, "<<>><<")


def _water_cell(water: dict | None) -> str:
    """The water column: now, days left, or harvested."""
    if not water:
        return "-"
    if water["status"] == "harvested":
        return "harvested"
    if water["days_left"] == 0:
        return "NOW"
    if water["days_left"] is None:
        return "ok 6+ wk"
    return f"{water['days_left']} days"


if __name__ == "__main__":  # pragma: no cover  (last, once every command is registered)
    cli()
