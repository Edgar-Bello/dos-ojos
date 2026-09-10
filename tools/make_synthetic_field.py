"""Generate a synthetic ODM output set: DSM, DTM, orthophoto and ground truth.

Writes into the layout ODM produces, so the rest of the pipeline cannot tell the
difference. The point is to have a field whose true canopy structure is known,
so canopy heights, detections and flags can be checked against an answer rather
than merely eyeballed.

Two patterns are supported:

* ``rows``  - sorghum or cane: continuous rows with height variation, deliberate
  gaps where plants are missing, and stretches of stunted canopy.
* ``trees`` - orchard or citrus: discrete crowns on a planting grid, with some
  positions empty and some crowns small.

Usage:
    python tools/make_synthetic_field.py demo-chm --pattern rows
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from pyproj import Transformer

UTM_EPSG = 32614          # Rio Grande Valley
NODATA = -9999.0


def field_origin(fields_geojson: Path, field_id: str) -> tuple[float, float]:
    """South-west corner of a field, in UTM metres."""
    payload = json.loads(Path(fields_geojson).read_text(encoding="utf-8"))
    for feature in payload["features"]:
        if str(feature["properties"]["id"]) == field_id:
            ring = feature["geometry"]["coordinates"][0]
            to_utm = Transformer.from_crs(
                "EPSG:4326", f"EPSG:{UTM_EPSG}", always_xy=True
            ).transform
            points = [to_utm(lon, lat) for lon, lat in ring]
            return min(p[0] for p in points), min(p[1] for p in points)
    raise SystemExit(f"field {field_id!r} not found in {fields_geojson}")


def make_terrain(shape: tuple[int, int], res_m: float, rng) -> np.ndarray:
    """A gently undulating ground surface, the truth the DTM should recover."""
    rows, cols = shape
    y, x = np.mgrid[0:rows, 0:cols] * res_m
    # A slight slope plus a long-wavelength swell, as a levelled field has.
    terrain = 12.0 + 0.004 * x + 0.002 * y
    terrain += 0.15 * np.sin(2 * math.pi * x / 45.0) * np.cos(2 * math.pi * y / 60.0)
    return terrain + rng.normal(0, 0.01, shape)


def row_canopy(
    shape: tuple[int, int],
    res_m: float,
    rng,
    *,
    spacing_m: float,
    bearing_deg: float,
    height_m: float,
    gap_fraction: float,
    stress_fraction: float,
) -> tuple[np.ndarray, list[dict]]:
    """Continuous crop rows with gaps and stunted stretches.

    Returns the canopy height above ground and the ground truth describing each
    anomaly, in pixel coordinates so a detector can be scored against it.
    """
    rows, cols = shape
    y, x = np.mgrid[0:rows, 0:cols] * res_m
    theta = math.radians(bearing_deg)

    # Distance across the rows; the sine of it gives the row ridges.
    across = x * math.cos(theta) + y * math.sin(theta)
    along = -x * math.sin(theta) + y * math.cos(theta)

    ridge = np.cos(2 * math.pi * across / spacing_m)
    canopy = height_m * np.clip(ridge, 0, None) ** 0.6
    canopy *= 1.0 + 0.12 * np.sin(2 * math.pi * along / 11.0)      # natural variation

    truth: list[dict] = []
    row_index = (across / spacing_m).astype(int)
    n_rows = int(row_index.max()) + 1
    along_extent = float(along.max() - along.min())

    for anomaly in range(int(n_rows * gap_fraction * 4)):
        target_row = rng.integers(0, max(1, n_rows))
        start = rng.uniform(along.min(), along.max() - 6.0)
        length = rng.uniform(2.0, 6.0)
        patch = (
            (row_index == target_row)
            & (along >= start)
            & (along < start + length)
        )
        if not patch.any():
            continue
        canopy[patch] = 0.0
        truth.append({"kind": "gap", "row": int(target_row),
                      "along_start_m": float(start), "length_m": float(length),
                      "n_pixels": int(patch.sum())})

    for anomaly in range(int(n_rows * stress_fraction * 4)):
        target_row = rng.integers(0, max(1, n_rows))
        start = rng.uniform(along.min(), along.max() - 8.0)
        length = rng.uniform(4.0, 9.0)
        patch = (
            (row_index == target_row)
            & (along >= start)
            & (along < start + length)
            & (canopy > 0)
        )
        if not patch.any():
            continue
        canopy[patch] *= 0.42
        truth.append({"kind": "stunted", "row": int(target_row),
                      "along_start_m": float(start), "length_m": float(length),
                      "n_pixels": int(patch.sum())})

    canopy += rng.normal(0, 0.02, shape)
    return np.clip(canopy, 0, None), truth


def tree_canopy(
    shape: tuple[int, int],
    res_m: float,
    rng,
    *,
    spacing_m: float,
    height_m: float,
    crown_radius_m: float,
    missing_fraction: float,
    stress_fraction: float,
) -> tuple[np.ndarray, list[dict]]:
    """Discrete crowns on a planting grid, with missing and undersized trees."""
    rows, cols = shape
    canopy = np.zeros(shape, dtype=np.float64)
    y, x = np.mgrid[0:rows, 0:cols] * res_m

    truth: list[dict] = []
    step = spacing_m
    for cy in np.arange(step, rows * res_m - step, step):
        for cx in np.arange(step, cols * res_m - step, step):
            roll = rng.random()
            if roll < missing_fraction:
                truth.append({"kind": "missing", "x_m": float(cx), "y_m": float(cy)})
                continue
            stressed = roll < missing_fraction + stress_fraction
            scale = 0.45 if stressed else rng.uniform(0.85, 1.15)
            radius = crown_radius_m * (0.6 if stressed else rng.uniform(0.9, 1.1))

            distance = np.hypot(x - cx, y - cy)
            inside = distance < radius
            if not inside.any():
                continue
            # A dome: full height at the centre, tapering to zero at the edge.
            dome = height_m * scale * np.sqrt(np.clip(1 - (distance / radius) ** 2, 0, 1))
            canopy = np.maximum(canopy, dome)
            truth.append({
                "kind": "stressed" if stressed else "healthy",
                "x_m": float(cx), "y_m": float(cy),
                "height_m": float(height_m * scale),
                "crown_radius_m": float(radius),
            })

    canopy += rng.normal(0, 0.02, shape)
    return np.clip(canopy, 0, None), truth


def punch_holes(surface: np.ndarray, rng, *, count: int, max_radius_px: int) -> np.ndarray:
    """Knock nodata holes into a surface, as a real reconstruction has."""
    rows, cols = surface.shape
    holed = surface.copy()
    for _ in range(count):
        cy, cx = rng.integers(0, rows), rng.integers(0, cols)
        radius = rng.integers(2, max_radius_px)
        y, x = np.mgrid[0:rows, 0:cols]
        holed[np.hypot(y - cy, x - cx) < radius] = np.nan
    return holed


def write_raster(
    path: Path, data: np.ndarray, transform, *, count: int = 1, dtype: str = "float32"
) -> None:
    """Write a georeferenced GeoTIFF in UTM 14N."""
    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.asarray(data)
    if array.ndim == 2:
        array = array[np.newaxis, ...]
    profile = {
        "driver": "GTiff", "height": array.shape[1], "width": array.shape[2],
        "count": count, "dtype": dtype, "crs": f"EPSG:{UTM_EPSG}",
        "transform": transform, "compress": "deflate",
    }
    if dtype == "float32":
        profile["nodata"] = NODATA
        array = np.where(np.isnan(array), NODATA, array)
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(array.astype(dtype))


def make_orthophoto(
    canopy: np.ndarray, shape: tuple[int, int], rng, height_m: float
) -> np.ndarray:
    """An RGB orthophoto whose greenness tracks canopy vigour.

    Bare ground reads brown, healthy canopy green, so the RGB stress indices in
    step 6 have something with a known answer to measure.
    """
    vigour = np.clip(canopy / max(height_m, 1e-6), 0, 1)
    soil = np.array([0.42, 0.32, 0.24])
    leaf = np.array([0.16, 0.46, 0.13])

    rgb = np.empty((3, *shape), dtype=np.float32)
    for band in range(3):
        rgb[band] = soil[band] + (leaf[band] - soil[band]) * vigour
    rgb += rng.normal(0, 0.015, rgb.shape).astype(np.float32)
    return np.clip(rgb, 0, 1) * 255


def main() -> None:
    """Write a synthetic ODM product set plus its ground truth."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flight_id")
    parser.add_argument("--field", default="rgv-002")
    parser.add_argument("--pattern", choices=["rows", "trees"], default="rows")
    parser.add_argument("--extent", type=float, default=60.0, help="block size in metres")
    parser.add_argument("--dem-resolution", type=float, default=5.0, help="cm/px")
    parser.add_argument("--ortho-resolution", type=float, default=2.0, help="cm/px")
    parser.add_argument("--row-spacing", type=float, default=0.76, help="metres")
    parser.add_argument("--row-bearing", type=float, default=12.0, help="degrees")
    parser.add_argument("--tree-spacing", type=float, default=5.0, help="metres")
    parser.add_argument("--crown-radius", type=float, default=1.8, help="metres")
    parser.add_argument("--canopy-height", type=float, default=2.4, help="metres")
    parser.add_argument("--gap-fraction", type=float, default=0.10)
    parser.add_argument("--stress-fraction", type=float, default=0.12)
    parser.add_argument("--holes", type=int, default=14)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--fields-geojson", type=Path,
                        default=Path(__file__).resolve().parents[2]
                        / "dosojos_sat" / "fields.geojson")
    parser.add_argument("--odm-dir", type=Path, default=None)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    project = args.odm_dir or (
        Path(__file__).resolve().parents[1] / "data" / "odm" / args.flight_id
    )

    dem_res = args.dem_resolution / 100.0
    size = int(round(args.extent / dem_res))
    shape = (size, size)

    east, north = field_origin(args.fields_geojson, args.field)
    dem_transform = from_origin(east, north + args.extent, dem_res, dem_res)

    terrain = make_terrain(shape, dem_res, rng)
    if args.pattern == "rows":
        canopy, truth = row_canopy(
            shape, dem_res, rng,
            spacing_m=args.row_spacing, bearing_deg=args.row_bearing,
            height_m=args.canopy_height, gap_fraction=args.gap_fraction,
            stress_fraction=args.stress_fraction,
        )
    else:
        canopy, truth = tree_canopy(
            shape, dem_res, rng,
            spacing_m=args.tree_spacing, height_m=args.canopy_height,
            crown_radius_m=args.crown_radius,
            missing_fraction=args.gap_fraction,
            stress_fraction=args.stress_fraction,
        )

    dsm = punch_holes(terrain + canopy, rng, count=args.holes, max_radius_px=9)
    dtm = punch_holes(terrain, rng, count=max(1, args.holes // 3), max_radius_px=6)

    write_raster(project / "odm_dem" / "dsm.tif", dsm, dem_transform)
    write_raster(project / "odm_dem" / "dtm.tif", dtm, dem_transform)
    # The true canopy, so a computed CHM can be scored against an exact answer
    # rather than compared with its own summary statistics.
    write_raster(project / "truth_canopy.tif", canopy, dem_transform)

    ortho_res = args.ortho_resolution / 100.0
    ortho_size = int(round(args.extent / ortho_res))
    scale = ortho_size / size
    ortho_canopy = np.repeat(np.repeat(canopy, math.ceil(scale), 0),
                             math.ceil(scale), 1)[:ortho_size, :ortho_size]
    rgb = make_orthophoto(ortho_canopy, (ortho_size, ortho_size), rng, args.canopy_height)
    write_raster(
        project / "odm_orthophoto" / "odm_orthophoto.tif", rgb,
        from_origin(east, north + args.extent, ortho_res, ortho_res),
        count=3, dtype="uint8",
    )

    (project / "odm_georeferencing").mkdir(parents=True, exist_ok=True)
    (project / "odm_georeferencing" / "odm_georeferenced_model.laz").write_bytes(
        b"synthetic placeholder, not a real point cloud"
    )

    truth_path = project / "ground_truth.json"
    truth_path.write_text(json.dumps({
        "flight_id": args.flight_id, "field_id": args.field, "pattern": args.pattern,
        "epsg": UTM_EPSG, "extent_m": args.extent,
        "dem_resolution_m": dem_res, "canopy_height_m": args.canopy_height,
        "row_spacing_m": args.row_spacing, "row_bearing_deg": args.row_bearing,
        "tree_spacing_m": args.tree_spacing,
        "mean_canopy_height_m": float(np.nanmean(canopy)),
        "max_canopy_height_m": float(np.nanmax(canopy)),
        "anomalies": truth,
    }, indent=2), encoding="utf-8")

    print(f"wrote synthetic {args.pattern} field to {project}")
    print(f"  dem {size}x{size} at {args.dem_resolution:g} cm/px")
    print(f"  ortho {ortho_size}x{ortho_size} at {args.ortho_resolution:g} cm/px")
    print(f"  canopy mean {np.nanmean(canopy):.3f} m, max {np.nanmax(canopy):.3f} m")
    print(f"  {len(truth)} ground-truth anomalies -> {truth_path.name}")


if __name__ == "__main__":
    main()
