"""Canopy height model: how tall the crop is, as distinct from how high the ground is.

CHM = DSM - DTM. The arithmetic is trivial; everything else here is about the
ways a real reconstruction is not trivial. ODM's two surfaces can land on
different grids, carry nodata as -9999 or as NaN, contain holes where matching
failed, and hold spikes where it matched something that was not ground.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

log = logging.getLogger(__name__)

#: Holes smaller than this are filled from their neighbours; larger voids are
#: left as nodata rather than invented. Expressed as ground area, not pixels, so
#: the behaviour does not change when --dem-resolution does. A quarter of a
#: square metre is smaller than any plant worth measuring.
DEFAULT_FILL_HOLES_M2 = 0.25

#: Gaussian smoothing radius on the ground, in metres. Also deliberately not in
#: pixels: at 5 cm a one-pixel sigma smooths 5 cm, at 2 cm it smooths 2 cm, and
#: the same setting would then mean different things between runs.
#:
#: Measured against a known canopy, smoothing is the only meaningful source of
#: error in the model. On 0.76 m rows: 0 m gives 0.004 m RMSE, 0.03 m gives
#: 0.06 m, 0.05 m gives 0.18 m, and 0.10 m gives 0.39 m. The default is light
#: because later steps measure the row structure that smoothing destroys.
DEFAULT_SMOOTH_M = 0.03

#: Canopy taller than this is a reconstruction artifact for any field crop.
#: Sorghum tops out near 3 m and sugarcane near 5 m.
DEFAULT_MAX_HEIGHT_M = 8.0


class ChmError(RuntimeError):
    """Raised when a canopy model cannot be built from the given surfaces."""


@dataclass(frozen=True)
class Surface:
    """One raster loaded into memory, with nodata already turned into NaN."""

    data: np.ndarray
    transform: object
    crs: object
    path: Path

    @property
    def shape(self) -> tuple[int, int]:
        """Raster shape as (rows, columns)."""
        return self.data.shape

    @property
    def resolution_m(self) -> tuple[float, float]:
        """Pixel size in metres, as (x, y)."""
        return abs(self.transform.a), abs(self.transform.e)


@dataclass(frozen=True)
class ChmStats:
    """Summary of a canopy model, for reporting and sanity checking."""

    shape: tuple[int, int]
    resolution_m: float
    coverage: float
    mean_m: float
    median_m: float
    p95_m: float
    max_m: float
    n_filled: int
    n_clipped_negative: int
    n_clipped_tall: int

    @property
    def pixel_area_m2(self) -> float:
        """Ground area of one pixel, the unit canopy volume integrates over."""
        return self.resolution_m**2


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_surface(path: Path) -> Surface:
    """Read a DEM, converting its nodata to NaN.

    ODM writes -9999 for nodata, but a raster carrying NaN directly is also
    common, so both are handled rather than assumed.

    Raises:
        ChmError: if the file is unreadable or carries no CRS.
    """
    path = Path(path)
    try:
        with rasterio.open(path) as dataset:
            data = dataset.read(1, masked=True).astype(np.float64)
            crs, transform, nodata = dataset.crs, dataset.transform, dataset.nodata
    except Exception as exc:  # noqa: BLE001 - any read failure is fatal here
        raise ChmError(f"could not read {path}: {exc}") from exc

    if crs is None:
        raise ChmError(
            f"{path.name} carries no CRS. An ungeoreferenced surface cannot be "
            "tied to a field; check that the images had GPS."
        )

    array = np.ma.filled(data, np.nan)
    if nodata is not None and not np.isnan(nodata):
        array[array == nodata] = np.nan
    # ODM occasionally writes very large sentinels rather than its declared nodata.
    array[np.abs(array) > 1e6] = np.nan
    return Surface(data=array, transform=transform, crs=crs, path=path)


def align_to(source: Surface, reference: Surface) -> Surface:
    """Resample one surface onto another's grid, if it is not already there.

    ODM normally emits DSM and DTM on the same grid, but a differing
    ``--dem-resolution`` between runs, or a resumed run, can leave them
    mismatched. Subtracting mismatched arrays either raises or, worse, silently
    broadcasts.
    """
    same_grid = (
        source.shape == reference.shape
        and source.crs == reference.crs
        and np.allclose(
            [source.transform.a, source.transform.e, source.transform.c, source.transform.f],
            [reference.transform.a, reference.transform.e,
             reference.transform.c, reference.transform.f],
            rtol=1e-9, atol=1e-6,
        )
    )
    if same_grid:
        return source

    log.warning(
        "%s is on a different grid from %s (%s vs %s); resampling to match",
        source.path.name, reference.path.name, source.shape, reference.shape,
    )
    destination = np.full(reference.shape, np.nan, dtype=np.float64)
    reproject(
        source=source.data,
        destination=destination,
        src_transform=source.transform, src_crs=source.crs,
        dst_transform=reference.transform, dst_crs=reference.crs,
        src_nodata=np.nan, dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return Surface(
        data=destination, transform=reference.transform,
        crs=reference.crs, path=source.path,
    )


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #


def fill_small_holes(array: np.ndarray, max_px: float) -> tuple[np.ndarray, int]:
    """Fill nodata patches smaller than ``max_px`` from their nearest neighbours.

    Large voids are deliberately left alone. Interpolating across a hole the size
    of a tree would invent canopy that was never observed, and every downstream
    volume would inherit it.
    """
    from scipy import ndimage

    holes = np.isnan(array)
    if max_px <= 0 or not holes.any():
        return array, 0

    labels, count = ndimage.label(holes)
    if count == 0:
        return array, 0

    sizes = np.bincount(labels.ravel())
    small_labels = np.flatnonzero((sizes <= max_px))
    small_labels = small_labels[small_labels != 0]
    if small_labels.size == 0:
        return array, 0

    fillable = np.isin(labels, small_labels)
    _, indices = ndimage.distance_transform_edt(
        holes, return_distances=True, return_indices=True
    )
    filled = array.copy()
    filled[fillable] = array[tuple(indices[:, fillable])]
    n_filled = int(fillable.sum())
    log.info("filled %d pixel(s) in %d small hole(s)", n_filled, small_labels.size)
    return filled, n_filled


def smooth_preserving_holes(array: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian-smooth while ignoring NaN, and leave the NaN where it was.

    A plain Gaussian filter spreads a single NaN across its whole kernel, so a
    handful of holes would eat the surface around them.
    """
    from scipy import ndimage

    if sigma <= 0:
        return array

    valid = np.isfinite(array)
    filled = np.where(valid, array, 0.0)
    weighted = ndimage.gaussian_filter(filled, sigma, mode="nearest")
    weights = ndimage.gaussian_filter(valid.astype(np.float64), sigma, mode="nearest")

    with np.errstate(invalid="ignore", divide="ignore"):
        smoothed = weighted / weights
    smoothed[~valid] = np.nan
    return smoothed


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


def compute_chm(
    dsm: Surface,
    dtm: Surface,
    *,
    clamp_negative: bool = True,
    fill_holes_m2: float = DEFAULT_FILL_HOLES_M2,
    smooth_m: float = DEFAULT_SMOOTH_M,
    max_height_m: float | None = DEFAULT_MAX_HEIGHT_M,
) -> tuple[np.ndarray, ChmStats]:
    """Subtract ground from surface and clean the result.

    Order matters. Negatives are clamped before smoothing, so noise below ground
    cannot pull nearby canopy down; holes are filled before smoothing, so the
    smoother has something to work with; and the tall clamp comes last, after
    smoothing has removed the single-pixel spikes it would otherwise trip on.
    """
    aligned = align_to(dtm, dsm)
    if dsm.shape != aligned.shape:
        raise ChmError(
            f"DSM {dsm.shape} and DTM {aligned.shape} could not be put on a "
            "common grid; check both came from the same ODM run"
        )

    chm = dsm.data - aligned.data
    total = chm.size

    n_negative = int(np.sum(np.isfinite(chm) & (chm < 0)))
    if clamp_negative:
        chm = np.where(np.isfinite(chm) & (chm < 0), 0.0, chm)

    # Both cleaning parameters arrive in ground units and are converted here, so
    # a run at a different --dem-resolution behaves the same way.
    pixel_m = float(dsm.resolution_m[0])
    pixel_area = pixel_m**2
    max_hole_px = fill_holes_m2 / pixel_area if pixel_area > 0 else 0
    sigma_px = smooth_m / pixel_m if pixel_m > 0 else 0.0

    chm, n_filled = fill_small_holes(chm, max_hole_px)
    chm = smooth_preserving_holes(chm, sigma_px)

    n_tall = 0
    if max_height_m is not None:
        tall = np.isfinite(chm) & (chm > max_height_m)
        n_tall = int(tall.sum())
        if n_tall:
            log.warning(
                "%d pixel(s) exceeded %.1f m and were clipped; that is taller than "
                "any field crop and usually means a reconstruction artifact",
                n_tall, max_height_m,
            )
        chm = np.where(tall, max_height_m, chm)

    finite = chm[np.isfinite(chm)]
    if finite.size == 0:
        raise ChmError(
            "the canopy model is empty. The DSM and DTM may not overlap, or the "
            "reconstruction may have produced nothing usable."
        )

    stats = ChmStats(
        shape=chm.shape,
        resolution_m=float(dsm.resolution_m[0]),
        coverage=float(finite.size) / total,
        mean_m=float(finite.mean()),
        median_m=float(np.median(finite)),
        p95_m=float(np.percentile(finite, 95)),
        max_m=float(finite.max()),
        n_filled=n_filled,
        n_clipped_negative=n_negative,
        n_clipped_tall=n_tall,
    )
    log.info(
        "canopy model %dx%d at %.2f m, coverage %.1f%%, mean %.2f m, max %.2f m",
        *chm.shape, stats.resolution_m, 100 * stats.coverage, stats.mean_m, stats.max_m,
    )
    return chm, stats


def clip_to_field(
    chm: np.ndarray, transform, crs, field
) -> tuple[np.ndarray, int]:
    """Mask everything outside the field outline, returning the pixels kept.

    ODM reconstructs whatever the flight saw, which includes headlands, roads and
    a neighbour's crop. Statistics taken over that are not statistics about this
    field.
    """
    from rasterio.features import geometry_mask
    from shapely.ops import transform as shapely_transform
    from pyproj import Transformer

    target = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    field_projected = shapely_transform(target, field)
    inside = geometry_mask(
        [field_projected.__geo_interface__], out_shape=chm.shape,
        transform=transform, invert=True,
    )
    clipped = np.where(inside, chm, np.nan)
    return clipped, int(np.sum(inside & np.isfinite(clipped)))


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def save_chm(
    chm: np.ndarray, reference: Surface, out_tif: Path, *, nodata: float = -9999.0
) -> Path:
    """Write the canopy model as a georeferenced GeoTIFF."""
    out_tif = Path(out_tif)
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff", "height": chm.shape[0], "width": chm.shape[1],
        "count": 1, "dtype": "float32", "crs": reference.crs,
        "transform": reference.transform, "nodata": nodata,
        "compress": "deflate", "predictor": 3,
    }
    with rasterio.open(out_tif, "w", **profile) as dataset:
        dataset.write(np.where(np.isfinite(chm), chm, nodata).astype(np.float32), 1)
        dataset.set_band_description(1, "canopy_height_m")
    log.info("wrote %s", out_tif)
    return out_tif
