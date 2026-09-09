"""Tests for index arithmetic and summary statistics against known answers."""

from __future__ import annotations

import numpy as np
import pytest

from dosojos_sat.indices import (
    clamp_reflectance,
    compute_index,
    normalized_difference,
    summarize,
)


def _bands(**values: float) -> dict[str, np.ndarray]:
    """Build single-pixel band arrays from scalar reflectances."""
    return {name: np.array([[v]], dtype=np.float32) for name, v in values.items()}


# --------------------------------------------------------------------------- #
# Normalised difference
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (0.5, 0.1, 0.4 / 0.6),      # typical vegetation
        (3.0, 1.0, 0.5),            # exact half
        (0.2, 0.2, 0.0),            # equal bands
        (0.0, 0.4, -1.0),           # lower bound
        (0.4, 0.0, 1.0),            # upper bound
    ],
)
def test_normalized_difference_known_values(a: float, b: float, expected: float) -> None:
    """The ratio matches hand-computed answers, including both bounds."""
    result = normalized_difference(np.array([a]), np.array([b]))
    assert result[0] == pytest.approx(expected, abs=1e-6)


def test_zero_denominator_is_nan_not_infinity() -> None:
    """Two dark bands give 0/0, which must not propagate as inf."""
    result = normalized_difference(np.array([0.0, 0.3]), np.array([0.0, 0.1]))
    assert np.isnan(result[0])
    assert np.isfinite(result[1])


def test_nan_input_stays_nan() -> None:
    """A nodata pixel in either band leaves the index undefined."""
    result = normalized_difference(np.array([np.nan, 0.5]), np.array([0.1, np.nan]))
    assert np.isnan(result).all()


# --------------------------------------------------------------------------- #
# The three indices
# --------------------------------------------------------------------------- #


def test_ndvi_uses_nir_and_red() -> None:
    """NDVI = (B08 - B04) / (B08 + B04)."""
    got = compute_index(_bands(B08=0.5, B04=0.1, B11=0.9, B03=0.9), "NDVI")
    assert got[0, 0] == pytest.approx(0.4 / 0.6, abs=1e-6)


def test_ndmi_uses_nir_and_swir() -> None:
    """NDMI = (B08 - B11) / (B08 + B11)."""
    got = compute_index(_bands(B08=0.4, B11=0.2, B04=0.9, B03=0.9), "NDMI")
    assert got[0, 0] == pytest.approx(0.2 / 0.6, abs=1e-6)


def test_ndwi_uses_green_and_nir() -> None:
    """NDWI = (B03 - B08) / (B03 + B08), negative over vegetation."""
    got = compute_index(_bands(B03=0.1, B08=0.5, B04=0.9, B11=0.9), "NDWI")
    assert got[0, 0] == pytest.approx(-0.4 / 0.6, abs=1e-6)


def test_unknown_index_is_rejected() -> None:
    """A typo in an index name fails loudly rather than returning nonsense."""
    with pytest.raises(ValueError, match="unknown index"):
        compute_index(_bands(B08=0.4, B04=0.1), "EVI")


# --------------------------------------------------------------------------- #
# Reflectance clamping
# --------------------------------------------------------------------------- #


def test_clamp_bounds_reflectance_and_keeps_nan() -> None:
    """Negative and above-one reflectance are clamped; nodata stays nodata."""
    out = clamp_reflectance(np.array([-0.05, 0.3, 1.4, np.nan]))
    assert out[0] == 0.0
    assert out[1] == pytest.approx(0.3)
    assert out[2] == 1.0
    assert np.isnan(out[3])


def test_negative_red_saturates_ndvi_instead_of_discarding_the_pixel() -> None:
    """A dark-red canopy pixel must survive as NDVI 1.0, not vanish as NaN.

    Atmospheric correction pushes red slightly negative over dense canopy. Those
    are the greenest pixels in a field, so dropping them would bias the whole
    observation downward; this is the regression that guards against it.
    """
    got = compute_index(_bands(B08=0.30, B04=-0.05, B11=0.2, B03=0.1), "NDVI")
    assert np.isfinite(got[0, 0])
    assert got[0, 0] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Summary statistics
# --------------------------------------------------------------------------- #


def test_summary_statistics_match_known_distribution() -> None:
    """Percentiles of 0.00..1.00 in even steps are exactly the round numbers."""
    values = (np.arange(101) / 100.0).reshape(101, 1)
    mask = np.ones_like(values, dtype=bool)

    stats = summarize(values, mask, n_total_px=101)
    assert stats is not None
    assert stats.median == pytest.approx(0.50)
    assert stats.p10 == pytest.approx(0.10)
    assert stats.p25 == pytest.approx(0.25)
    assert stats.p75 == pytest.approx(0.75)
    assert stats.p90 == pytest.approx(0.90)
    assert stats.mean == pytest.approx(0.50)
    assert stats.std == pytest.approx(np.sqrt(850) / 100, abs=1e-9)
    assert stats.n_valid_px == 101
    assert stats.valid_fraction == pytest.approx(1.0)


def test_summary_honours_the_mask() -> None:
    """Only masked-in pixels contribute, so the median follows the subset."""
    values = np.array([[0.0, 1.0], [0.4, 0.6]])
    mask = np.array([[False, False], [True, True]])
    stats = summarize(values, mask, n_total_px=4)
    assert stats is not None
    assert stats.median == pytest.approx(0.5)
    assert stats.n_valid_px == 2
    assert stats.valid_fraction == pytest.approx(0.5)


def test_summary_ignores_nan_pixels() -> None:
    """NaN inside the mask reduces the count rather than poisoning the mean."""
    values = np.array([[0.2, np.nan, 0.4]])
    stats = summarize(values, np.ones((1, 3), dtype=bool), n_total_px=3)
    assert stats is not None
    assert stats.n_valid_px == 2
    assert stats.mean == pytest.approx(0.3)
    assert stats.valid_fraction == pytest.approx(2 / 3)


def test_summary_of_nothing_is_none() -> None:
    """A fully masked observation yields no row rather than NaN statistics."""
    values = np.array([[np.nan, np.nan]])
    assert summarize(values, np.ones((1, 2), dtype=bool), n_total_px=2) is None
    assert summarize(np.array([[0.5]]), np.zeros((1, 1), dtype=bool), n_total_px=1) is None
