"""Where a field is thin or bare, from the colour picture alone.

A flight that comes as photos, stitched into one picture without a height
model, cannot find rows or measure plants. What it can still see is how much
of each square metre is green, and how green: a gap in the stand is soil
colour where the rest of the field has leaves, and a struggling stretch is
thinner or paler than the field around it.

Each square is judged against the rest of THIS field on the same day, never
against a fixed number, so a young crop or a naturally pale variety is not
flagged for being what it is:

- **missing**: under a quarter of the field's usual green cover, the soil
  showing where the rest of the field has plants;
- **stressed** (weak): well under the usual cover, or clearly paler green than
  the field's leaves usually are;
- **healthy**: the rest.

Greenness is the excess-green index on colour shares (2g - r - b, each band as
a share of the three), which a bright or a dull photo reads the same, so the
exposure differences between stitched photos do not move it.

What it cannot do: in an orchard or any crop with bare alleys, the alleys are
soil by design and would all read as gaps, so this is for row crops that close
their rows; and it cannot tell a short plant from a tall one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: The size of each square judged, in metres.
DEFAULT_CELL_M = 1.0
#: The picture is read at about this resolution; finer adds time, not answers.
WORKING_RESOLUTION_M = 0.05
#: A square needs this share of its area in the picture (and in the field) to be judged.
MIN_COVERED = 0.5
#: Under this share of the field's usual green cover a square is missing plants...
MISSING_SHARE = 0.25
#: ...and under this share, weak.
WEAK_SHARE = 0.6
#: Leaves paler than the field's usual by this many spreads (MAD) count as weak.
PALE_SPREADS = 3.0
#: A field whose usual square is less green than this has no crop to judge yet.
MIN_FIELD_COVER = 0.10
#: A pixel whose three bands add up to less than this is no picture, not dark soil.
BLACK_SUM = 30
#: Otsu's split between soil and leaf is kept inside these: never so low that damp
#: soil counts as leaf, never so high that a yellowing leaf counts as soil (a
#: weak plant is not a missing one). Excess green of about 0.1 is where crop
#: imaging usually draws the line.
LEAF_EXG_FLOOR = 0.05
LEAF_EXG_CEILING = 0.15


class ColourError(RuntimeError):
    """Raised when a picture cannot be judged, with why."""


@dataclass
class ColourResult:
    """What the squares came to, for the report and the summary."""

    n_cells: int = 0
    n_judged: int = 0
    field_cover: float | None = None       # the usual square's green share
    leaf_threshold: float | None = None
    notes: list[str] = dc_field(default_factory=list)


def excess_green(rgb: np.ndarray) -> np.ndarray:
    """2g - r - b on band shares; NaN where the pixel is black (no data)."""
    rgb = rgb.astype(np.float64)
    total = rgb.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        shares = rgb / total
    exg = 2 * shares[1] - shares[0] - shares[2]
    exg[total <= 0] = np.nan
    return exg


def leaf_threshold(exg: np.ndarray) -> float:
    """Excess green above which a pixel is leaf: Otsu's split, kept between a floor and a ceiling."""
    from skimage.filters import threshold_otsu

    values = exg[np.isfinite(exg)]
    if values.size < 100:
        raise ColourError("too little of the picture to tell leaf from soil")
    try:
        split = float(threshold_otsu(values))
    except ValueError:
        split = LEAF_EXG_FLOOR
    return min(max(split, LEAF_EXG_FLOOR), LEAF_EXG_CEILING)


def judge(ortho_path: Path, field=None, *, cell_m: float = DEFAULT_CELL_M):
    """Every square of the field with its green cover and its flag.

    Args:
        field: the field outline in WGS84 (shapely), or None for the whole picture.

    Returns:
        (GeoDataFrame of squares, ColourResult). Squares carry ``flag``,
        ``cover``, ``exg_mean`` and an empty ``volume_m3`` (there is no height).
    """
    import geopandas as gpd
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.features import geometry_mask
    from shapely.geometry import box

    result = ColourResult()
    with rasterio.open(ortho_path) as dataset:
        step = max(1, int(round(WORKING_RESOLUTION_M / abs(dataset.transform.a))))
        shape = (max(1, dataset.height // step), max(1, dataset.width // step))
        rgb = dataset.read([1, 2, 3], out_shape=(3, *shape), resampling=Resampling.average)
        if dataset.count >= 4:
            covered = dataset.read(4, out_shape=shape, resampling=Resampling.nearest) > 0
        else:
            covered = rgb.sum(axis=0) > 0
        transform = dataset.transform * dataset.transform.scale(
            dataset.width / shape[1], dataset.height / shape[0])
        crs = dataset.crs
    # Black is where no photo reached (a camera never records true black in a field).
    covered &= rgb.astype(np.int32).sum(axis=0) > BLACK_SUM
    if field is not None:
        outline = gpd.GeoSeries([field], crs="EPSG:4326").to_crs(crs).iloc[0]
        covered &= ~geometry_mask([outline], out_shape=shape, transform=transform)
    exg = excess_green(rgb)
    exg[~covered] = np.nan
    threshold = leaf_threshold(exg)
    result.leaf_threshold = round(threshold, 3)
    leaf = (exg > threshold) & covered

    pixel = abs(transform.a)
    per_cell = max(1, int(round(cell_m / pixel)))
    rows, cols = shape[0] // per_cell, shape[1] // per_cell
    if rows == 0 or cols == 0:
        raise ColourError("the picture is smaller than one square")

    def blocks(array: np.ndarray) -> np.ndarray:
        cut = array[:rows * per_cell, :cols * per_cell]
        return cut.reshape(rows, per_cell, cols, per_cell).swapaxes(1, 2).reshape(
            rows, cols, per_cell * per_cell)

    seen = blocks(covered).mean(axis=2)
    leaf_blocks = blocks(leaf)
    cover = np.where(seen > 0, leaf_blocks.sum(axis=2) / np.maximum(
        blocks(covered).sum(axis=2), 1), np.nan)
    exg_blocks = blocks(np.where(leaf, exg, np.nan))
    with np.errstate(invalid="ignore"), _quiet():
        leaf_green = np.nanmean(exg_blocks, axis=2)

    judged = seen >= MIN_COVERED
    result.n_judged = int(judged.sum())
    if not result.n_judged:
        raise ColourError("no square of the field is covered by the picture")
    usual_cover = float(np.median(cover[judged]))
    result.field_cover = round(usual_cover, 3)
    greens = leaf_green[judged & np.isfinite(leaf_green)]
    usual_green = float(np.median(greens)) if greens.size else np.nan
    spread = float(np.median(np.abs(greens - usual_green))) * 1.4826 if greens.size else np.nan

    flags = np.full((rows, cols), "NO_DATA", dtype=object)
    if usual_cover < MIN_FIELD_COVER:
        flags[judged] = "HEALTHY"
        result.notes.append(
            f"The usual square of this field is only {usual_cover:.0%} green: there is no "
            "crop up yet to judge, so nothing is flagged.")
    else:
        flags[judged] = "HEALTHY"
        pale = judged & np.isfinite(leaf_green) & (leaf_green < usual_green - PALE_SPREADS * spread)
        weak = judged & ((cover < WEAK_SHARE * usual_cover) | pale)
        flags[weak] = "STRESSED"
        flags[judged & (cover < MISSING_SHARE * usual_cover)] = "MISSING"

    geometries, records = [], []
    for r in range(rows):
        for c in range(cols):
            if flags[r, c] == "NO_DATA" and seen[r, c] == 0:
                continue
            west, north = transform * (c * per_cell, r * per_cell)
            east, south = transform * ((c + 1) * per_cell, (r + 1) * per_cell)
            geometries.append(box(min(west, east), min(south, north),
                                  max(west, east), max(south, north)))
            records.append({"flag": flags[r, c],
                            "cover": round(float(cover[r, c]), 3)
                            if np.isfinite(cover[r, c]) else None,
                            "exg_mean": round(float(leaf_green[r, c]), 4)
                            if np.isfinite(leaf_green[r, c]) else None,
                            "volume_m3": np.nan})
    frame = gpd.GeoDataFrame(records, geometry=geometries, crs=crs)
    result.n_cells = len(frame)
    return frame, result


class _quiet:
    """Silence numpy's 'mean of empty slice' for squares with no leaf at all."""

    def __enter__(self):
        import warnings

        self._catch = warnings.catch_warnings()
        self._catch.__enter__()
        warnings.simplefilter("ignore", category=RuntimeWarning)

    def __exit__(self, *exc):
        return self._catch.__exit__(*exc)
