"""Per-unit metrics: how big each crown or row segment is, and how green.

Structure comes from the canopy model, colour from the orthophoto. The two sit on
different grids, usually 5 cm and 2 cm, so every unit is rasterised onto each
grid separately rather than resampling one raster onto the other.

Everything is computed zonally in one pass per raster: the units are burned into
a label image, and ``bincount`` sums every pixel into its unit at once. That is
the difference between seconds and minutes on a few thousand row segments.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize

log = logging.getLogger(__name__)

#: Canopy below this height counts as ground when measuring cover. Low enough to
#: keep a stunted plant, high enough to ignore soil clods and residue.
DEFAULT_COVER_HEIGHT_M = 0.15

#: Indices normalised within the flight. Absolute RGB is not comparable between
#: flights, or even within one as the sun moves, so later steps compare these.
INDEX_NAMES = ("vari", "exg", "gli")

#: Smallest denominator allowed in a ratio index. VARI's G + R - B passes
#: through zero over some soils and water, where the ratio is meaningless.
_MIN_DENOMINATOR = 1e-6


class MetricsError(RuntimeError):
    """Raised when metrics cannot be computed for the given inputs."""


# --------------------------------------------------------------------------- #
# Zonal machinery
# --------------------------------------------------------------------------- #


def label_raster(units: gpd.GeoDataFrame, shape: tuple[int, int], transform) -> np.ndarray:
    """Burn each unit's position into an integer image, 1-based; 0 is outside.

    Pixels are assigned by centre, so a pixel belongs to at most one unit and
    none is counted twice where two segments meet.
    """
    shapes = ((geometry, index + 1) for index, geometry in enumerate(units.geometry))
    return rasterize(
        shapes, out_shape=shape, transform=transform,
        fill=0, dtype="int32", all_touched=False,
    )


def zonal_sum(values: np.ndarray, labels: np.ndarray, n_units: int) -> np.ndarray:
    """Sum of finite values per unit, as an array indexed by unit position."""
    valid = np.isfinite(values) & (labels > 0)
    sums = np.bincount(labels[valid], weights=values[valid], minlength=n_units + 1)
    return sums[1:]


def zonal_count(mask: np.ndarray, labels: np.ndarray, n_units: int) -> np.ndarray:
    """Number of pixels per unit where ``mask`` holds."""
    counts = np.bincount(labels[mask & (labels > 0)], minlength=n_units + 1)
    return counts[1:]


def zonal_max(values: np.ndarray, labels: np.ndarray, n_units: int) -> np.ndarray:
    """Maximum finite value per unit, NaN for a unit with no valid pixels."""
    result = np.full(n_units + 1, -np.inf)
    valid = np.isfinite(values) & (labels > 0)
    np.maximum.at(result, labels[valid], values[valid])
    result[~np.isfinite(result)] = np.nan
    return result[1:]


def zonal_mean(values: np.ndarray, labels: np.ndarray, n_units: int) -> np.ndarray:
    """Mean of finite values per unit, NaN where a unit has none."""
    sums = zonal_sum(values, labels, n_units)
    counts = zonal_count(np.isfinite(values), labels, n_units)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #


def structure_metrics(
    units: gpd.GeoDataFrame,
    chm: np.ndarray,
    transform,
    *,
    cover_height_m: float = DEFAULT_COVER_HEIGHT_M,
) -> pd.DataFrame:
    """Area, height, cover and volume for every unit.

    Volume integrates canopy height over the unit's footprint: the sum over its
    pixels of height times pixel area. That is exact for a surface model, which
    is what photogrammetry produces; a second, independent estimate from the
    point cloud is available through :func:`canopy_volume_voxel`.
    """
    n_units = len(units)
    pixel_area = abs(transform.a * transform.e)
    labels = label_raster(units, chm.shape, transform)

    heights = np.where(np.isfinite(chm), np.maximum(chm, 0.0), np.nan)
    n_pixels = zonal_count(np.ones_like(labels, dtype=bool), labels, n_units)
    n_valid = zonal_count(np.isfinite(heights), labels, n_units)
    n_canopy = zonal_count(np.isfinite(heights) & (heights >= cover_height_m), labels, n_units)

    volume = zonal_sum(heights, labels, n_units) * pixel_area
    with np.errstate(invalid="ignore", divide="ignore"):
        coverage = np.where(n_pixels > 0, n_valid / np.maximum(n_pixels, 1), 0.0)
        cover = np.where(n_valid > 0, n_canopy / np.maximum(n_valid, 1), np.nan)

    frame = pd.DataFrame({
        "unit_id": units["unit_id"].to_numpy(),
        "area_m2": units.geometry.area.to_numpy(),
        "height_max_m": zonal_max(heights, labels, n_units),
        "height_mean_m": zonal_mean(heights, labels, n_units),
        "canopy_cover": cover,
        "volume_m3": volume,
        "n_pixels": n_pixels,
        "data_coverage": coverage,
    })

    tiny = frame["n_pixels"] == 0
    if tiny.any():
        log.warning(
            "%d unit(s) contain no pixel centre, usually slivers smaller than one "
            "pixel; their metrics are empty", int(tiny.sum()),
        )
    return frame


def canopy_volume_voxel(
    points: np.ndarray,
    polygon,
    *,
    voxel_m: float = 0.10,
    ground_z: float | None = None,
) -> float:
    """Canopy volume from a point cloud, by filled-column voxelisation.

    Photogrammetry sees surfaces, not interiors, so counting occupied voxels
    would measure skin rather than volume. Each ground column is instead filled
    from the ground to its highest point, which is the same assumption the canopy
    model makes but reached independently, from the raw points rather than the
    interpolated raster.

    Args:
        points: ``(n, 3)`` array of x, y, z in the polygon's CRS.
        ground_z: Ground elevation under the unit. Defaults to the lowest point,
            which suits a unit that includes some bare ground around the crown.
    """
    from shapely import contains_xy

    points = np.asarray(points, dtype=np.float64)
    if points.size == 0:
        return 0.0
    inside = contains_xy(polygon, points[:, 0], points[:, 1])
    selected = points[inside]
    if selected.size == 0:
        return 0.0

    floor = float(selected[:, 2].min()) if ground_z is None else float(ground_z)
    columns = np.floor(selected[:, :2] / voxel_m).astype(np.int64)
    keys, inverse = np.unique(columns, axis=0, return_inverse=True)
    tops = np.full(len(keys), -np.inf)
    np.maximum.at(tops, inverse.ravel(), selected[:, 2])

    heights = np.clip(tops - floor, 0.0, None)
    voxels = np.ceil(heights / voxel_m)
    return float(voxels.sum() * voxel_m**3)


def points_from_chm(chm: np.ndarray, transform, ground: float = 0.0) -> np.ndarray:
    """Turn a canopy model into x, y, z points, for cross-checking the voxel path."""
    rows, cols = np.nonzero(np.isfinite(chm))
    xs, ys = rasterio.transform.xy(transform, rows, cols, offset="center")
    return np.column_stack([np.asarray(xs), np.asarray(ys), ground + chm[rows, cols]])


def read_point_cloud(path: Path) -> np.ndarray:
    """Read an ODM LAZ point cloud into an ``(n, 3)`` array.

    Raises:
        MetricsError: if the file is a placeholder or unreadable.
    """
    import laspy

    try:
        with laspy.open(path) as reader:
            cloud = reader.read()
    except Exception as exc:  # noqa: BLE001 - any failure makes the check unavailable
        raise MetricsError(f"could not read point cloud {path}: {exc}") from exc
    return np.column_stack([cloud.x, cloud.y, cloud.z])


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #


def rgb_indices(red: np.ndarray, green: np.ndarray, blue: np.ndarray) -> dict[str, np.ndarray]:
    """VARI, ExG and GLI per pixel.

    ExG is computed on chromatic coordinates, r = R / (R + G + B) and so on,
    rather than raw digital numbers. That is the formulation it was defined with
    and the one that removes overall brightness, so a cloud shadow drifting over
    part of the field does not register as stress.
    """
    r = red.astype(np.float64)
    g = green.astype(np.float64)
    b = blue.astype(np.float64)

    total = r + g + b
    with np.errstate(invalid="ignore", divide="ignore"):
        rn = np.where(total > 0, r / total, np.nan)
        gn = np.where(total > 0, g / total, np.nan)
        bn = np.where(total > 0, b / total, np.nan)

        vari_denominator = g + r - b
        vari = np.where(
            np.abs(vari_denominator) > _MIN_DENOMINATOR,
            (g - r) / vari_denominator, np.nan,
        )
        gli_denominator = 2 * g + r + b
        gli = np.where(
            np.abs(gli_denominator) > _MIN_DENOMINATOR,
            (2 * g - r - b) / gli_denominator, np.nan,
        )

    # VARI is unbounded as its denominator approaches zero; beyond this range the
    # value describes the arithmetic, not the plant.
    vari = np.where(np.abs(vari) <= 1.5, vari, np.nan)
    return {"vari": vari, "exg": 2 * gn - rn - bn, "gli": gli}


def colour_metrics(
    units: gpd.GeoDataFrame, ortho_path: Path
) -> pd.DataFrame:
    """Mean VARI, ExG and GLI per unit from the orthophoto.

    Raises:
        MetricsError: if the orthophoto has fewer than three bands or a
            different CRS from the units.
    """
    with rasterio.open(ortho_path) as dataset:
        if dataset.count < 3:
            raise MetricsError(
                f"{Path(ortho_path).name} has {dataset.count} band(s); RGB indices "
                "need three"
            )
        if units.crs is not None and dataset.crs is not None and units.crs != dataset.crs:
            units = units.to_crs(dataset.crs)
        red, green, blue = (dataset.read(i).astype(np.float64) for i in (1, 2, 3))
        alpha = dataset.read(4) if dataset.count >= 4 else None
        transform, shape = dataset.transform, (dataset.height, dataset.width)

    # ODM marks areas outside the reconstruction as fully transparent or black.
    empty = (red + green + blue) == 0
    if alpha is not None:
        empty |= alpha == 0
    for band in (red, green, blue):
        band[empty] = np.nan

    labels = label_raster(units, shape, transform)
    indices = rgb_indices(red, green, blue)
    n_units = len(units)
    return pd.DataFrame({
        "unit_id": units["unit_id"].to_numpy(),
        **{f"{name}_mean": zonal_mean(values, labels, n_units)
           for name, values in indices.items()},
    })


def normalise_within_flight(frame: pd.DataFrame, columns) -> pd.DataFrame:
    """Add a within-flight percentile rank for each column, 0 to 100.

    Absolute RGB depends on the camera, the exposure, the sun angle and the haze,
    so an ExG of 0.12 means nothing on its own. Its rank among the other units
    flown in the same minutes under the same light does.
    """
    ranked = frame.copy()
    for column in columns:
        if column in ranked:
            ranked[f"{column}_pct"] = ranked[column].rank(pct=True) * 100.0
    return ranked


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def compute_metrics(
    units: gpd.GeoDataFrame,
    chm: np.ndarray,
    chm_transform,
    *,
    ortho_path: Path | None = None,
    cover_height_m: float = DEFAULT_COVER_HEIGHT_M,
) -> gpd.GeoDataFrame:
    """Join structure and colour metrics onto the units, keeping their geometry."""
    if units.empty:
        raise MetricsError("no units to measure; run detection first")

    table = structure_metrics(units, chm, chm_transform, cover_height_m=cover_height_m)
    columns = ["height_max_m", "height_mean_m", "volume_m3", "canopy_cover"]

    if ortho_path is not None and Path(ortho_path).exists():
        colour = colour_metrics(units, ortho_path)
        table = table.merge(colour, on="unit_id", how="left")
        columns += [f"{name}_mean" for name in INDEX_NAMES]
    else:
        log.warning("no orthophoto; RGB stress indices will be missing")

    table = normalise_within_flight(table, columns)
    merged = units.merge(table, on="unit_id", how="left")
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=units.crs)


def summarise(metrics: gpd.GeoDataFrame) -> dict[str, float]:
    """Flight-level figures for reporting."""
    def safe(series: pd.Series, fn) -> float:
        finite = series.dropna()
        return float(fn(finite)) if len(finite) else float("nan")

    return {
        "n_units": float(len(metrics)),
        "total_volume_m3": safe(metrics["volume_m3"], np.sum),
        "median_volume_m3": safe(metrics["volume_m3"], np.median),
        "median_height_m": safe(metrics["height_mean_m"], np.median),
        "median_cover": safe(metrics["canopy_cover"], np.median),
        "mean_exg": safe(metrics.get("exg_mean", pd.Series(dtype=float)), np.mean),
    }


def dome_volume(height_m: float, radius_m: float) -> float:
    """Exact volume of an ellipsoidal crown: two thirds pi r squared h.

    Used to score the canopy integration against an analytic answer, which is a
    stronger check than comparing two numerical methods with each other.
    """
    return (2.0 / 3.0) * math.pi * radius_m**2 * height_m
