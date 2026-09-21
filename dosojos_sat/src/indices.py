"""Vegetation and moisture index math, and SCL-based cloud masking.

Every function here works on plain arrays rather than on a loaded scene, which
keeps the maths testable against synthetic inputs with known answers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping

import numpy as np

from .config import MASK_CLASSES

log = logging.getLogger(__name__)

#: Which two bands feed each index, in numerator order: ``(a - b) / (a + b)``.
INDEX_BANDS: dict[str, tuple[str, str]] = {
    "NDVI": ("B08", "B04"),   # vegetation vigour
    "NDMI": ("B08", "B11"),   # canopy moisture
    "NDWI": ("B03", "B08"),   # open water / saturation
}

#: Denominators nearer zero than this produce meaningless ratios.
_MIN_DENOMINATOR = 1e-6


# --------------------------------------------------------------------------- #
# Masking
# --------------------------------------------------------------------------- #


def scl_valid_mask(
    scl: np.ndarray, mask_classes: Iterable[int] = MASK_CLASSES
) -> np.ndarray:
    """Return True where the scene classification layer says a pixel is usable.

    Args:
        scl: Scene classification band, one class number per pixel.
        mask_classes: Classes to reject. The default drops nodata, saturated,
            cloud shadow, medium and high probability cloud, cirrus and snow.
    """
    rejected = np.asarray(sorted(mask_classes), dtype=np.int16)
    return ~np.isin(np.asarray(scl).astype(np.int16), rejected)


def observation_mask(
    scl: np.ndarray,
    bands: Iterable[np.ndarray],
    poly_mask: np.ndarray,
    mask_classes: Iterable[int] = MASK_CLASSES,
) -> np.ndarray:
    """Pixels inside the field that SCL accepts and every band has real data for.

    A pixel is only usable if all three tests pass, so a band that is NaN because
    of a nodata gap cannot quietly contribute to one index while spoiling another.
    """
    mask = np.asarray(poly_mask, dtype=bool) & scl_valid_mask(scl, mask_classes)
    for values in bands:
        mask &= np.isfinite(values)
    return mask


def normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Return ``(a - b) / (a + b)`` as float32, NaN where the denominator vanishes.

    Inputs are expected to be clamped to physical reflectance already, which bounds
    the result to [-1, 1]; the range check here is a safety net, not the main
    defence. See :func:`compute_index` for why clamping happens there.
    """
    left = np.asarray(a, dtype=np.float64)
    right = np.asarray(b, dtype=np.float64)
    denominator = left + right
    with np.errstate(invalid="ignore", divide="ignore"):
        result = (left - right) / denominator
    result[np.abs(denominator) < _MIN_DENOMINATOR] = np.nan
    out_of_range = np.abs(result) > 1.0
    if out_of_range.any():
        log.debug("%d pixel(s) fell outside [-1, 1] and were dropped", int(out_of_range.sum()))
        result[out_of_range] = np.nan
    return result.astype(np.float32)


def clamp_reflectance(values: np.ndarray) -> np.ndarray:
    """Clamp reflectance to the physically possible [0, 1], preserving NaN.

    Atmospheric correction routinely pushes L2A red and green slightly negative
    over dark, dense canopy. Negative reflectance is a correction artifact, and
    the best estimate of the truth behind it is "reflects essentially nothing",
    which is zero.
    """
    return np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)


def compute_index(bands: Mapping[str, np.ndarray], index_name: str) -> np.ndarray:
    """Compute one named index from a mapping of band name to reflectance array.

    Bands are clamped to [0, 1] first. Without that, a slightly negative red band
    drives NDVI above 1, and discarding those pixels as out of range would throw
    away precisely the greenest parts of a field, biasing the whole observation
    downward. Clamping keeps every pixel and bounds the index naturally.

    Raises:
        ValueError: if ``index_name`` is not one of NDVI, NDMI or NDWI.
    """
    try:
        numerator_band, subtrahend_band = INDEX_BANDS[index_name]
    except KeyError:
        raise ValueError(
            f"unknown index {index_name!r}; expected one of {sorted(INDEX_BANDS)}"
        ) from None
    return normalized_difference(
        clamp_reflectance(bands[numerator_band]),
        clamp_reflectance(bands[subtrahend_band]),
    )


@dataclass(frozen=True)
class IndexStats:
    """Summary of one index over the usable pixels of one field on one day."""

    mean: float
    median: float
    p10: float
    p25: float
    p75: float
    p90: float
    std: float
    valid_fraction: float
    n_valid_px: int
    n_total_px: int


def summarize(
    values: np.ndarray, mask: np.ndarray, *, n_total_px: int
) -> IndexStats | None:
    """Summarise index values across the masked pixels, or None if none survive.

    ``valid_fraction`` is measured against the pixels this index could actually
    use, so an index weakened by a poor B11 read reports a lower figure than its
    neighbours rather than inheriting the observation-wide number.
    """
    sample = np.asarray(values)[np.asarray(mask, dtype=bool)]
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return None

    p10, p25, median, p75, p90 = (
        float(v) for v in np.percentile(sample, [10, 25, 50, 75, 90])
    )
    return IndexStats(
        mean=float(np.mean(sample)),
        median=median,
        p10=p10,
        p25=p25,
        p75=p75,
        p90=p90,
        std=float(np.std(sample)),
        valid_fraction=float(sample.size) / n_total_px if n_total_px else 0.0,
        n_valid_px=int(sample.size),
        n_total_px=int(n_total_px),
    )


def class_fraction(scl: np.ndarray, poly_mask: np.ndarray, class_value: int) -> float:
    """Fraction of the pixels inside the field holding one SCL class.

    Used to tell a cloudy observation apart from one where the tile simply has no
    data over this field, which are worth different responses.
    """
    total = int(np.count_nonzero(poly_mask))
    if total == 0:
        return 0.0
    hits = np.asarray(scl) == class_value
    return float(np.count_nonzero(hits & np.asarray(poly_mask, dtype=bool))) / total


def valid_fraction(valid_mask: np.ndarray, poly_mask: np.ndarray) -> float:
    """Fraction of the pixels inside the field outline that survived masking.

    Returns 0.0 for an empty polygon rather than dividing by zero, so a bad
    outline shows up as an unusable observation instead of a crash.
    """
    total = int(np.count_nonzero(poly_mask))
    if total == 0:
        return 0.0
    return float(np.count_nonzero(valid_mask)) / total
