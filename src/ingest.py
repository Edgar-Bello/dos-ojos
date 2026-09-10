"""Flight ingest: read EXIF from a folder of geotagged JPEGs and judge the survey.

The point of this module is to fail before ODM does. A flight with missing GPS,
a wandering altitude, or thin forward overlap will burn hours in photogrammetry
and then produce a hole-ridden orthomosaic, so every one of those is detected
here and reported loudly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ExifTags

log = logging.getLogger(__name__)

JPEG_SUFFIXES = frozenset({".jpg", ".jpeg", ".JPG", ".JPEG"})

#: Thresholds at which a survey stops being worth sending to ODM.
MIN_FORWARD_OVERLAP = 0.70
MAX_ALTITUDE_VARIATION = 0.15
MIN_IMAGES = 12

#: Sensor widths in millimetres for cameras whose EXIF omits the focal plane tags.
#: Without one of these the ground sample distance cannot be derived from focal
#: length and altitude, so the survey reports GSD as unknown rather than guessing.
SENSOR_WIDTHS_MM: dict[str, float] = {
    "FC220": 6.16,        # Mavic Pro
    "FC300X": 6.16,       # Phantom 3
    "FC330": 6.16,        # Phantom 4
    "FC6310": 13.2,       # Phantom 4 Pro
    "FC7303": 6.16,       # Mavic Air 2
    "FC3411": 6.16,       # Air 2S sub-model
    "FC3582": 9.65,       # Mini 3 / Mini 4 Pro
    "L1D-20C": 13.2,      # Mavic 2 Pro (Hasselblad)
    "ZenmuseP1": 35.9,
    "M3M": 17.3,          # Mavic 3 Multispectral, RGB sensor
}


class IngestError(RuntimeError):
    """Raised when a folder cannot be read as a flight at all."""


@dataclass(frozen=True)
class Shot:
    """One geotagged frame, with the EXIF fields photogrammetry cares about."""

    path: Path
    lon: float | None
    lat: float | None
    alt_m: float | None
    timestamp: datetime | None
    focal_mm: float | None
    camera: str | None
    width_px: int
    height_px: int
    sensor_width_mm: float | None = None
    relative_alt_m: float | None = None

    @property
    def has_gps(self) -> bool:
        """True when the frame carries a usable position."""
        return self.lon is not None and self.lat is not None


@dataclass(frozen=True)
class FlightSurvey:
    """Everything worth knowing about a flight before committing to ODM."""

    flight_id: str
    folder: Path
    shots: list[Shot]
    bounds_wgs84: tuple[float, float, float, float] | None
    gsd_cm: float | None
    footprint_m: tuple[float, float] | None      # across-track, along-track
    mean_spacing_m: float | None
    forward_overlap: float | None
    alt_mean_m: float | None
    alt_variation: float | None
    duration_s: float | None
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def n_images(self) -> int:
        """Number of frames found."""
        return len(self.shots)

    @property
    def n_with_gps(self) -> int:
        """Number of frames carrying a position."""
        return sum(1 for shot in self.shots if shot.has_gps)

    @property
    def usable(self) -> bool:
        """False when a warning would make photogrammetry a waste of time."""
        return not any(w.startswith("BLOCKER") for w in self.warnings)


# --------------------------------------------------------------------------- #
# EXIF reading
# --------------------------------------------------------------------------- #


_EXIF_TAGS = {value: key for key, value in ExifTags.TAGS.items()}
_GPS_TAGS = {value: key for key, value in ExifTags.GPSTAGS.items()}


def _rational(value: object) -> float | None:
    """Coerce an EXIF rational, tuple or number into a float."""
    if value is None:
        return None
    if isinstance(value, tuple) and len(value) == 2:
        return float(value[0]) / float(value[1]) if value[1] else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dms_to_degrees(dms: Sequence[object], ref: str | None) -> float | None:
    """Convert EXIF degrees/minutes/seconds plus a hemisphere ref to signed degrees."""
    if not dms or len(dms) != 3:
        return None
    parts = [_rational(part) for part in dms]
    if any(part is None for part in parts):
        return None
    degrees = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    if ref and ref.upper() in ("S", "W"):
        degrees = -degrees
    return degrees


def _gps_block(exif: Image.Exif) -> dict[str, object]:
    """Pull the GPS IFD out of an EXIF block, keyed by readable name.

    ``getexif()`` reports the GPS tag as a byte offset rather than its contents,
    so the sub-IFD has to be requested explicitly or every frame reads as having
    no position at all.
    """
    try:
        raw = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except (AttributeError, KeyError, OSError, ValueError):
        return {}
    if not raw:
        return {}
    return {ExifTags.GPSTAGS.get(key, key): value for key, value in raw.items()}


def _altitude(gps: dict[str, object]) -> float | None:
    """Absolute altitude in metres, honouring the below-sea-level reference."""
    altitude = _rational(gps.get("GPSAltitude"))
    if altitude is None:
        return None
    ref = gps.get("GPSAltitudeRef")
    below_sea_level = ref in (1, b"\x01", "1")
    return -altitude if below_sea_level else altitude


def _timestamp(exif: dict) -> datetime | None:
    """Capture time from DateTimeOriginal, falling back to DateTime."""
    for name in ("DateTimeOriginal", "DateTime"):
        raw = exif.get(_EXIF_TAGS.get(name, -1))
        if isinstance(raw, str):
            try:
                return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
    return None


def _sensor_width(exif: dict, camera: str | None, width_px: int) -> float | None:
    """Sensor width in millimetres, from EXIF focal-plane tags or the model table.

    The focal plane resolution tags give it directly when present; otherwise the
    camera model is looked up. Returning None is deliberate, since a guessed
    sensor size produces a confidently wrong ground sample distance.
    """
    resolution = _rational(exif.get(_EXIF_TAGS.get("FocalPlaneXResolution", -1)))
    unit = exif.get(_EXIF_TAGS.get("FocalPlaneResolutionUnit", -1))
    if resolution and resolution > 0:
        per_mm = {2: 25.4, 3: 10.0, 4: 1.0}.get(unit if isinstance(unit, int) else 2, 25.4)
        return width_px / resolution * per_mm
    if camera:
        return SENSOR_WIDTHS_MM.get(camera.strip())
    return None


def read_relative_altitude(path: Path) -> float | None:
    """Height above the takeoff point, from the XMP block DJI embeds in each JPEG.

    This is the only reliable source of flying height. Absolute GPS altitude is
    referenced to the ellipsoid and says nothing about the ground beneath, so
    without either this tag or a surveyed ground elevation the height above
    ground is genuinely unknown.
    """
    try:
        head = path.read_bytes()[:65536]
    except OSError:
        return None
    marker = b"drone-dji:RelativeAltitude="
    start = head.find(marker)
    if start < 0:
        return None
    chunk = head[start + len(marker):start + len(marker) + 24]
    text = chunk.decode("ascii", "ignore").strip().strip('"').split('"')[0]
    try:
        return float(text)
    except ValueError:
        return None


def read_shot(path: Path) -> Shot:
    """Read one JPEG's EXIF into a :class:`Shot`, tolerating missing fields."""
    with Image.open(path) as image:
        width_px, height_px = image.size
        exif = image.getexif()
        merged = dict(exif)
        try:
            merged.update(dict(exif.get_ifd(0x8769)))   # ExifIFD holds focal plane tags
        except Exception:  # noqa: BLE001 - malformed EXIF must not sink the read
            pass

    gps = _gps_block(exif)
    camera = merged.get(_EXIF_TAGS.get("Model", -1))
    camera = str(camera).strip() if camera else None

    return Shot(
        path=path,
        lon=_dms_to_degrees(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")),
        lat=_dms_to_degrees(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")),
        alt_m=_altitude(gps),
        timestamp=_timestamp(merged),
        focal_mm=_rational(merged.get(_EXIF_TAGS.get("FocalLength", -1))),
        camera=camera,
        width_px=width_px,
        height_px=height_px,
        sensor_width_mm=_sensor_width(merged, camera, width_px),
        relative_alt_m=read_relative_altitude(path),
    )


def read_shots(folder: Path) -> list[Shot]:
    """Read every JPEG in a folder, ordered by capture time then filename.

    Raises:
        IngestError: if the folder is absent or holds no JPEGs.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise IngestError(f"{folder} is not a directory")

    paths = sorted(p for p in folder.iterdir() if p.suffix in JPEG_SUFFIXES)
    if not paths:
        raise IngestError(f"{folder} contains no JPEG files")

    shots = []
    for path in paths:
        try:
            shots.append(read_shot(path))
        except Exception as exc:  # noqa: BLE001 - report and continue past bad frames
            log.warning("skipping %s: %s", path.name, exc)

    if not shots:
        raise IngestError(f"none of the {len(paths)} file(s) in {folder} could be read")

    shots.sort(key=lambda s: (s.timestamp or datetime.min, s.path.name))
    log.info("read %d image(s) from %s", len(shots), folder)
    return shots


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres between two WGS84 positions."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def estimate_gsd_cm(shot: Shot, altitude_agl_m: float) -> float | None:
    """Ground sample distance in centimetres per pixel.

    GSD = altitude * sensor_width / (focal_length * image_width). Returns None
    when the sensor width is unknown, since the whole overlap estimate downstream
    would otherwise inherit a fabricated number.
    """
    if not (shot.focal_mm and shot.sensor_width_mm and shot.width_px and altitude_agl_m):
        return None
    if shot.focal_mm <= 0 or shot.sensor_width_mm <= 0:
        return None
    metres_per_px = (altitude_agl_m * shot.sensor_width_mm) / (shot.focal_mm * shot.width_px)
    return metres_per_px * 100.0


def footprint_m(shot: Shot, gsd_cm: float) -> tuple[float, float]:
    """Ground footprint of one frame in metres, as (width, height)."""
    metres_per_px = gsd_cm / 100.0
    return shot.width_px * metres_per_px, shot.height_px * metres_per_px


def forward_overlap(spacing_m: float, along_track_m: float) -> float | None:
    """Fraction of a frame shared with the next one along the flight line.

    Assumes the camera's long axis lies across track, the usual nadir mapping
    layout. Clamped to [0, 1] so a stationary hover cannot report above 100%.
    """
    if along_track_m <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - spacing_m / along_track_m))


# --------------------------------------------------------------------------- #
# Survey
# --------------------------------------------------------------------------- #


def _consecutive_spacings(shots: Sequence[Shot]) -> list[float]:
    """Distances in metres between consecutive geotagged frames."""
    located = [s for s in shots if s.has_gps]
    return [
        haversine_m(a.lon, a.lat, b.lon, b.lat)
        for a, b in zip(located, located[1:])
    ]


def _bounds(shots: Sequence[Shot]) -> tuple[float, float, float, float] | None:
    """Bounding box of the camera positions, as (min_lon, min_lat, max_lon, max_lat)."""
    located = [s for s in shots if s.has_gps]
    if not located:
        return None
    lons = [s.lon for s in located]
    lats = [s.lat for s in located]
    return min(lons), min(lats), max(lons), max(lats)


def _median(values: Iterable[float]) -> float | None:
    """Median of a possibly empty iterable."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def survey_flight(
    flight_id: str, folder: Path, *, ground_elevation_m: float | None = None
) -> FlightSurvey:
    """Read a flight folder and judge whether it is worth sending to ODM.

    Args:
        ground_elevation_m: Terrain elevation, used to turn absolute GPS altitude
            into height above ground. Defaults to the lowest altitude seen, which
            is a reasonable stand-in for flat Rio Grande Valley fields.
    """
    shots = read_shots(folder)
    warnings: list[str] = []

    altitudes = [s.alt_m for s in shots if s.alt_m is not None]
    alt_mean = sum(altitudes) / len(altitudes) if altitudes else None
    alt_variation = _altitude_variation(altitudes)
    agl = _height_above_ground(shots, altitudes, ground_elevation_m)

    gsd_cm = _median(estimate_gsd_cm(s, agl) for s in shots) if agl else None
    footprint = footprint_m(shots[0], gsd_cm) if gsd_cm else None

    spacings = _consecutive_spacings(shots)
    mean_spacing = sum(spacings) / len(spacings) if spacings else None
    overlap = (
        forward_overlap(_median(spacings), footprint[1])
        if spacings and footprint
        else None
    )

    survey = FlightSurvey(
        flight_id=flight_id,
        folder=Path(folder),
        shots=shots,
        bounds_wgs84=_bounds(shots),
        gsd_cm=gsd_cm,
        footprint_m=footprint,
        mean_spacing_m=mean_spacing,
        forward_overlap=overlap,
        alt_mean_m=alt_mean,
        alt_variation=alt_variation,
        duration_s=_duration_s(shots),
        warnings=warnings,
    )
    warnings.extend(check_survey(survey))
    for warning in warnings:
        log.warning("%s: %s", flight_id, warning)
    return survey


def _altitude_variation(altitudes: Sequence[float]) -> float | None:
    """Peak-to-peak altitude spread as a fraction of the mean."""
    if len(altitudes) < 2:
        return None
    mean = sum(altitudes) / len(altitudes)
    if mean == 0:
        return None
    return (max(altitudes) - min(altitudes)) / abs(mean)


def _height_above_ground(
    shots: Sequence[Shot],
    altitudes: Sequence[float],
    ground_elevation_m: float | None,
) -> float | None:
    """Flying height above the field, or None when it cannot be known.

    Prefers the XMP relative altitude DJI writes, then an explicitly supplied
    ground elevation. It deliberately does not fall back to the lowest GPS
    altitude seen: a mapping drone never descends to ground, so that estimate
    returns roughly the altitude jitter and produces a false overlap failure.
    """
    relative = [s.relative_alt_m for s in shots if s.relative_alt_m is not None]
    if relative:
        return sum(relative) / len(relative)
    if ground_elevation_m is not None and altitudes:
        height = sum(altitudes) / len(altitudes) - ground_elevation_m
        return height if height > 1.0 else None
    return None


def _duration_s(shots: Sequence[Shot]) -> float | None:
    """Wall-clock seconds between the first and last timestamped frame."""
    stamps = sorted(s.timestamp for s in shots if s.timestamp)
    if len(stamps) < 2:
        return None
    return (stamps[-1] - stamps[0]).total_seconds()


def _overlap_blocker(survey: FlightSurvey) -> str:
    """Name the specific missing input, rather than blaming the last one checked."""
    first = survey.shots[0]
    if survey.n_with_gps < 2:
        return "fewer than two frames carry GPS, so shot spacing is unknown"
    if survey.alt_mean_m is None:
        return "no frame carries GPS altitude, so flying height is unknown"
    if not any(s.relative_alt_m is not None for s in survey.shots):
        return (
            "flying height above ground is unknown. These frames carry no XMP "
            "relative altitude, so register the flight with --ground-elevation "
            "<metres AMSL> to enable the GSD and overlap checks"
        )
    if first.sensor_width_mm is None:
        return (
            f"sensor width for camera {first.camera or 'unknown'!r} is unknown; "
            "add it to SENSOR_WIDTHS_MM"
        )
    if not first.focal_mm:
        return "focal length is missing from EXIF"
    return "flying height above ground resolved to zero; pass --ground-elevation"


def check_survey(survey: FlightSurvey) -> list[str]:
    """Return every reason this flight might not reconstruct well.

    Messages prefixed ``BLOCKER`` mean ODM will either refuse the set or produce
    something unusable, and are worth stopping for; the rest are advisory.
    """
    problems: list[str] = []

    if survey.n_images < MIN_IMAGES:
        problems.append(
            f"BLOCKER: only {survey.n_images} image(s); photogrammetry needs at "
            f"least {MIN_IMAGES} and realistically many more"
        )

    missing_gps = survey.n_images - survey.n_with_gps
    if missing_gps == survey.n_images:
        problems.append(
            "BLOCKER: no image carries GPS. ODM can still run unreferenced, but "
            "nothing will be georeferenced and no output will join to a field id"
        )
    elif missing_gps:
        problems.append(
            f"{missing_gps} of {survey.n_images} image(s) lack GPS and will be "
            "positioned by feature matching alone"
        )

    if survey.alt_variation is not None and survey.alt_variation > MAX_ALTITUDE_VARIATION:
        problems.append(
            f"altitude varies by {survey.alt_variation:.0%} (over "
            f"{MAX_ALTITUDE_VARIATION:.0%}), so ground sample distance is uneven "
            "across the survey"
        )

    if survey.forward_overlap is None:
        problems.append(f"forward overlap could not be estimated: {_overlap_blocker(survey)}")
    elif survey.forward_overlap < MIN_FORWARD_OVERLAP:
        problems.append(
            f"BLOCKER: forward overlap is about {survey.forward_overlap:.0%}, "
            f"below the {MIN_FORWARD_OVERLAP:.0%} needed for a dense "
            "reconstruction. Expect holes in the orthomosaic"
        )

    if survey.gsd_cm and survey.gsd_cm > 10:
        problems.append(
            f"ground sample distance is about {survey.gsd_cm:.1f} cm/px, coarse "
            "for individual plant work"
        )
    return problems


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Coverage:
    """How much of the target field this flight actually photographs."""

    covered_fraction: float
    field_area_m2: float
    covered_area_m2: float
    images_for_full_coverage: int
    altitude_for_full_coverage_m: float | None

    @property
    def complete(self) -> bool:
        """True when essentially the whole field is inside the survey."""
        return self.covered_fraction >= 0.98


def estimate_coverage(survey: FlightSurvey, field, altitude_agl_m: float) -> Coverage | None:
    """Estimate the share of a field the survey photographs.

    Each frame is approximated by an axis-aligned rectangle of the computed
    footprint, which holds for the usual nadir lawnmower where the camera's long
    axis lies across track. A flight flown on a diagonal will read pessimistically.

    Returns None when the footprint is unknown, since a coverage figure derived
    from a guessed footprint would be worse than no figure at all.
    """
    if not survey.footprint_m or not survey.n_with_gps:
        return None

    from shapely.geometry import box
    from shapely.ops import transform, unary_union
    from pyproj import Transformer

    centroid = field.centroid
    zone = int(math.floor((centroid.x + 180.0) / 6.0)) + 1
    epsg = (32600 if centroid.y >= 0 else 32700) + zone
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform

    field_utm = transform(to_utm, field)
    across, along = survey.footprint_m
    frames = []
    for shot in survey.shots:
        if not shot.has_gps:
            continue
        x, y = to_utm(shot.lon, shot.lat)
        frames.append(box(x - across / 2, y - along / 2, x + across / 2, y + along / 2))

    covered = unary_union(frames).intersection(field_utm)
    fraction = covered.area / field_utm.area if field_utm.area else 0.0

    # Frames needed scales with area, so with the inverse square of altitude.
    needed = _images_for_field(field_utm.area, across, along, survey.forward_overlap)
    higher = (
        altitude_agl_m * math.sqrt(needed / survey.n_images)
        if survey.n_images and needed > survey.n_images and altitude_agl_m
        else None
    )
    return Coverage(
        covered_fraction=float(fraction),
        field_area_m2=float(field_utm.area),
        covered_area_m2=float(covered.area),
        images_for_full_coverage=needed,
        altitude_for_full_coverage_m=higher,
    )


def _images_for_field(
    area_m2: float, across_m: float, along_m: float, overlap: float | None
) -> int:
    """Frames needed to cover an area, allowing for forward and side overlap.

    Side overlap is assumed to be 0.70, the usual mapping default, since nothing
    in EXIF records what the flight was actually planned with.
    """
    forward = overlap if overlap is not None else 0.80
    effective = (along_m * (1.0 - forward)) * (across_m * (1.0 - 0.70))
    return int(math.ceil(area_m2 / effective)) if effective > 0 else 0
