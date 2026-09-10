"""Generate a synthetic geotagged flight for testing the pipeline without an SD card.

Writes JPEGs carrying real EXIF GPS, altitude, focal length and camera model, laid
out in a lawnmower pattern over a field polygon from the satellite project.

One caveat worth knowing: frames are written at a reduced pixel count so a couple
of hundred of them stay small. Ground footprint and overlap are unaffected, since
both depend only on altitude, focal length and sensor width. Ground sample
distance does scale with pixel count, so a synthetic flight reports a coarser GSD
than the real camera would.

Usage:
    python tools/make_synthetic_flight.py demo-001 --field rgv-002 --count 200
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import numpy as np
import piexif
from PIL import Image

# DJI Phantom 4 Pro, a common agricultural mapping camera.
CAMERA_MAKE = "DJI"
CAMERA_MODEL = "FC6310"
SENSOR_WIDTH_MM = 13.2
FOCAL_MM = 8.8


def _deg_to_dms_rational(value: float) -> tuple[tuple[int, int], ...]:
    """Convert signed decimal degrees into the EXIF degrees/minutes/seconds form."""
    value = abs(value)
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60, 5)
    return ((degrees, 1), (minutes, 1), (int(seconds * 100000), 100000))


def _rational(value: float, denominator: int = 1000) -> tuple[int, int]:
    """Express a float as an EXIF rational."""
    fraction = Fraction(value).limit_denominator(denominator)
    return fraction.numerator, fraction.denominator


def build_exif(
    lon: float, lat: float, altitude_m: float, when: datetime, width: int, height: int
) -> bytes:
    """Assemble an EXIF block with GPS, camera and lens fields ODM can read."""
    stamp = when.strftime("%Y:%m:%d %H:%M:%S")
    zeroth = {
        piexif.ImageIFD.Make: CAMERA_MAKE,
        piexif.ImageIFD.Model: CAMERA_MODEL,
        piexif.ImageIFD.DateTime: stamp,
        piexif.ImageIFD.Software: "dosojos synthetic flight",
    }
    exif = {
        piexif.ExifIFD.DateTimeOriginal: stamp,
        piexif.ExifIFD.FocalLength: _rational(FOCAL_MM),
        piexif.ExifIFD.PixelXDimension: width,
        piexif.ExifIFD.PixelYDimension: height,
        # Focal plane resolution in pixels per millimetre lets the reader derive
        # sensor width without needing the camera in its lookup table.
        piexif.ExifIFD.FocalPlaneXResolution: _rational(width / SENSOR_WIDTH_MM, 100000),
        piexif.ExifIFD.FocalPlaneResolutionUnit: 4,       # 4 = millimetres
    }
    gps = {
        piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
        piexif.GPSIFD.GPSLatitude: _deg_to_dms_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
        piexif.GPSIFD.GPSLongitude: _deg_to_dms_rational(lon),
        piexif.GPSIFD.GPSAltitudeRef: 0,
        piexif.GPSIFD.GPSAltitude: _rational(altitude_m, 100),
    }
    return piexif.dump({"0th": zeroth, "Exif": exif, "GPS": gps, "1st": {}, "thumbnail": None})


def synthetic_frame(width: int, height: int, seed: int) -> Image.Image:
    """A plausible-looking crop canopy frame: green rows with noise.

    Content does not matter for ingest, but real-looking texture keeps the JPEGs
    a realistic size rather than compressing to nothing.
    """
    rng = np.random.default_rng(seed)
    rows = np.sin(np.linspace(0, 40 * math.pi, width)) * 0.5 + 0.5
    canvas = np.zeros((height, width, 3), dtype=np.float32)
    canvas[..., 1] = 0.35 + 0.35 * rows[None, :]            # green channel carries rows
    canvas[..., 0] = 0.18 + 0.10 * rows[None, :]
    canvas[..., 2] = 0.12 + 0.05 * rows[None, :]
    canvas += rng.normal(0, 0.04, canvas.shape).astype(np.float32)
    return Image.fromarray((np.clip(canvas, 0, 1) * 255).astype(np.uint8))


def field_bounds(fields_geojson: Path, field_id: str) -> tuple[float, float, float, float]:
    """Bounding box of one field from the satellite project's polygons."""
    payload = json.loads(Path(fields_geojson).read_text(encoding="utf-8"))
    for feature in payload["features"]:
        if str(feature["properties"]["id"]) == field_id:
            ring = feature["geometry"]["coordinates"][0]
            lons = [point[0] for point in ring]
            lats = [point[1] for point in ring]
            return min(lons), min(lats), max(lons), max(lats)
    raise SystemExit(f"field {field_id!r} not found in {fields_geojson}")


def lawnmower(
    bounds: tuple[float, float, float, float],
    count: int,
    altitude_m: float,
    forward_overlap: float,
    side_overlap: float,
    width: int,
    height: int,
) -> list[tuple[float, float]]:
    """Plan a serpentine survey covering the bounds, returning positions in order."""
    min_lon, min_lat, max_lon, max_lat = bounds
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))

    metres_per_px = (altitude_m * SENSOR_WIDTH_MM) / (FOCAL_MM * width)
    across_m = width * metres_per_px
    along_m = height * metres_per_px

    shot_step_m = along_m * (1.0 - forward_overlap)
    line_step_m = across_m * (1.0 - side_overlap)

    field_h_m = (max_lat - min_lat) * m_per_deg_lat
    per_line = max(2, int(field_h_m / shot_step_m) + 1)
    n_lines = max(1, math.ceil(count / per_line))

    positions: list[tuple[float, float]] = []
    for line in range(n_lines):
        lon = min_lon + (line * line_step_m) / m_per_deg_lon
        offsets = range(per_line) if line % 2 == 0 else range(per_line - 1, -1, -1)
        for step in offsets:
            lat = min_lat + (step * shot_step_m) / m_per_deg_lat
            positions.append((lon, lat))
            if len(positions) >= count:
                return positions
    return positions


def main() -> None:
    """Write a synthetic flight folder."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flight_id")
    parser.add_argument("--field", default="rgv-002")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--altitude", type=float, default=60.0, help="metres above ground")
    parser.add_argument("--ground-elevation", type=float, default=12.0, help="metres AMSL")
    parser.add_argument("--forward-overlap", type=float, default=0.80)
    parser.add_argument("--side-overlap", type=float, default=0.70)
    parser.add_argument("--width", type=int, default=1368)
    parser.add_argument("--height", type=int, default=912)
    parser.add_argument("--altitude-jitter", type=float, default=0.8,
                        help="metres of random altitude wobble")
    parser.add_argument("--drop-gps", type=int, default=0,
                        help="strip GPS from this many frames, to test the warning")
    parser.add_argument("--fields-geojson", type=Path,
                        default=Path(__file__).resolve().parents[2]
                        / "dosojos_sat" / "fields.geojson")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    out_dir = args.out or (
        Path(__file__).resolve().parents[1] / "data" / "raw" / args.flight_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    bounds = field_bounds(args.fields_geojson, args.field)
    positions = lawnmower(
        bounds, args.count, args.altitude, args.forward_overlap,
        args.side_overlap, args.width, args.height,
    )

    rng = random.Random(7)
    start = datetime(2026, 9, 5, 15, 30, 0)
    no_gps = set(rng.sample(range(len(positions)), min(args.drop_gps, len(positions))))

    for index, (lon, lat) in enumerate(positions):
        when = start + timedelta(seconds=2 * index)
        altitude = args.ground_elevation + args.altitude + rng.uniform(
            -args.altitude_jitter, args.altitude_jitter
        )
        image = synthetic_frame(args.width, args.height, seed=index)
        path = out_dir / f"DJI_{index:04d}.JPG"
        if index in no_gps:
            image.save(path, quality=82)
        else:
            image.save(
                path, quality=82,
                exif=build_exif(lon, lat, altitude, when, args.width, args.height),
            )

    print(f"wrote {len(positions)} frame(s) to {out_dir}")
    print(f"field {args.field} bounds: {bounds}")
    if no_gps:
        print(f"{len(no_gps)} frame(s) written without GPS")


if __name__ == "__main__":
    main()
