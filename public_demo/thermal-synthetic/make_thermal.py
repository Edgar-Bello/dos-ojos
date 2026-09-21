"""Write a SYNTHETIC thermal mosaic and canopy model over a field outline.

NOT A MEASUREMENT. No camera took these pictures and no drone flew. We have no
thermal camera and no drone licence, so this exists only to run the optional
``dosojos-drone thermal`` step end to end and show what it produces. Every
figure it leads to carries a SYNTHETIC band, because the flight is registered
with ``--source synthetic``.

What it builds, over the outline you give it:

- a canopy height model: an orchard of round crowns on a regular grid, or a row
  crop's ridges on 30-inch rows, with bare ground between either way, which is
  what the thermal step masks with;
- a thermal raster in degrees Celsius: canopy near 31 C with ordinary noise,
  bare ground 18 C hotter as real sunlit soil is, and one round patch of trees
  running a few degrees warm, the thing the step is meant to find.

Usage (from this folder, in the drone half's Python)::

    python make_thermal.py --fields <fields.geojson> --id <feature> --out data --pattern rows
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import shape
from shapely.ops import transform as shapely_transform

RESOLUTION_M = 0.25
#: Citrus on a square planting, about what the Valley groves use.
TREE_SPACING_M = 6.0
CROWN_RADIUS_M = 2.2
CROWN_HEIGHT_M = 3.0
#: A row crop on 30-inch rows, canopy over most of the row and bare furrow between.
ROW_SPACING_M = 0.76
ROW_CANOPY_SHARE = 0.65
ROW_HEIGHT_M = 2.0
#: A sunlit row middle in May really does sit this far above the leaves.
GROUND_ABOVE_CANOPY_C = 18.0
CANOPY_C = 31.0
NOISE_C = 0.35
#: Real thermal noise is not per-pixel static: leaf angle, wind and the sensor's
#: own drift all vary over metres, so the noise is smoothed to about this far.
NOISE_SMOOTH_M = 1.0


def utm_for(geometry) -> int:
    """The UTM zone the outline sits in, as an EPSG code."""
    lon, lat = geometry.centroid.x, geometry.centroid.y
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def outline_of(path: Path, feature_id: str | None):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    features = payload.get("features", [payload])
    for feature in features:
        if feature_id is None or str((feature.get("properties") or {}).get("id")) == feature_id:
            return shape(feature["geometry"])
    raise SystemExit(f"no feature {feature_id!r} in {path}")


def canopy_of(pattern: str, x, y, west, north):
    """Canopy height: round crowns on a grid, or ridges along 30-inch rows."""
    if pattern == "rows":
        across = ((x - west) % ROW_SPACING_M) / ROW_SPACING_M
        return np.where(np.abs(across - 0.5) <= ROW_CANOPY_SHARE / 2, ROW_HEIGHT_M, 0.0)
    du = ((x - west) % TREE_SPACING_M) - TREE_SPACING_M / 2
    dv = ((north - y) % TREE_SPACING_M) - TREE_SPACING_M / 2
    to_trunk = np.hypot(du, dv)
    return np.where(to_trunk <= CROWN_RADIUS_M,
                    CROWN_HEIGHT_M * np.sqrt(np.clip(
                        1 - (to_trunk / CROWN_RADIUS_M) ** 2, 0, 1)), 0.0)


def grids(geometry, epsg: int, *, warm_c: float, seed: int, pattern: str = "orchard"):
    """Canopy height and canopy temperature over the outline's bounding box."""
    from pyproj import Transformer

    project = Transformer.from_crs(4326, epsg, always_xy=True).transform
    field = shapely_transform(project, geometry)
    west, south, east, north = field.bounds
    width = int(math.ceil((east - west) / RESOLUTION_M))
    height = int(math.ceil((north - south) / RESOLUTION_M))
    transform = from_origin(west, north, RESOLUTION_M, RESOLUTION_M)

    rows, cols = np.indices((height, width))
    x = west + (cols + 0.5) * RESOLUTION_M
    y = north - (rows + 0.5) * RESOLUTION_M

    canopy = canopy_of(pattern, x, y, west, north)

    from shapely import contains_xy

    inside = contains_xy(field, x, y)
    canopy = np.where(inside, canopy, np.nan)

    rng = np.random.default_rng(seed)
    heat = np.where(canopy > 0.3, CANOPY_C, CANOPY_C + GROUND_ABOVE_CANOPY_C)
    # The patch: a group of plants in the north-east quarter running warm.
    centre_x = west + 0.75 * (east - west)
    centre_y = south + 0.75 * (north - south)
    radius = max(12.0, 0.10 * min(east - west, north - south))
    patch = (x - centre_x) ** 2 + (y - centre_y) ** 2 <= radius ** 2
    heat = np.where(patch & (canopy > 0.3), heat + warm_c, heat)
    from scipy import ndimage

    noise = ndimage.gaussian_filter(rng.normal(0, NOISE_C, heat.shape),
                                    NOISE_SMOOTH_M / RESOLUTION_M)
    noise *= NOISE_C / max(float(noise.std()), 1e-9)      # smoothing shrinks it; put it back
    heat = np.where(np.isfinite(canopy), heat + noise, np.nan)
    return canopy, heat, transform, (centre_x, centre_y, radius)


def write(path: Path, data: np.ndarray, transform, epsg: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
                       count=1, dtype="float32", crs=rasterio.crs.CRS.from_epsg(epsg),
                       transform=transform, nodata=-9999.0, compress="deflate") as dataset:
        dataset.write(np.where(np.isfinite(data), data, -9999.0).astype(np.float32), 1)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fields", required=True, type=Path, help="GeoJSON with the outline.")
    parser.add_argument("--id", dest="feature_id", default=None, help="Which feature to use.")
    parser.add_argument("--out", required=True, type=Path, help="Where to write the rasters.")
    parser.add_argument("--warm", type=float, default=3.5,
                        help="How many degrees warmer the patch runs (default 3.5).")
    parser.add_argument("--pattern", choices=("orchard", "rows"), default="orchard",
                        help="How the crop is planted (default orchard).")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    geometry = outline_of(args.fields, args.feature_id)
    epsg = utm_for(geometry)
    canopy, heat, transform, (cx, cy, radius) = grids(
        geometry, epsg, warm_c=args.warm, seed=args.seed, pattern=args.pattern)
    chm = write(args.out / "chm.tif", canopy, transform, epsg)
    thermal = write(args.out / "thermal.tif", heat, transform, epsg)
    print("SYNTHETIC. No camera took these; they were generated by make_thermal.py.")
    print(f"  canopy   {chm}")
    print(f"  thermal  {thermal}")
    print(f"  the warm patch is {args.warm:g} C above the rest of the canopy, "
          f"{radius:.0f} m across, in the north-east quarter")


if __name__ == "__main__":
    main()
