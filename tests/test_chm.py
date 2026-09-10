"""Tests for the canopy height model against surfaces with known answers."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from dosojos_drone.chm import (
    ChmError,
    Surface,
    align_to,
    compute_chm,
    fill_small_holes,
    load_surface,
    save_chm,
    smooth_preserving_holes,
)

UTM = "EPSG:32614"


def _write(
    path: Path,
    data: np.ndarray,
    *,
    res: float = 0.05,
    crs: str | None = UTM,
    nodata: float | None = -9999.0,
    origin: tuple[float, float] = (600000.0, 2900000.0),
) -> Path:
    """Write a small DEM, with nodata written as the given sentinel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff", "height": data.shape[0], "width": data.shape[1],
        "count": 1, "dtype": "float32",
        "transform": from_origin(origin[0], origin[1], res, res),
    }
    if crs:
        profile["crs"] = crs
    if nodata is not None:
        profile["nodata"] = nodata
    array = np.where(np.isnan(data), nodata if nodata is not None else np.nan, data)
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(array.astype("float32"), 1)
    return path


def _surface(data: np.ndarray, *, res: float = 0.05, origin=(600000.0, 2900000.0)) -> Surface:
    """An in-memory surface without touching disk."""
    return Surface(
        data=np.asarray(data, dtype=np.float64),
        transform=from_origin(origin[0], origin[1], res, res),
        crs=rasterio.crs.CRS.from_string(UTM),
        path=Path("memory.tif"),
    )


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_odm_nodata_sentinel_becomes_nan(tmp_path: Path) -> None:
    """ODM writes -9999; left as a number it would read as ground 9999 m down."""
    data = np.array([[1.0, np.nan], [3.0, 4.0]])
    surface = load_surface(_write(tmp_path / "dsm.tif", data))
    assert np.isnan(surface.data[0, 1])
    assert surface.data[0, 0] == pytest.approx(1.0)


def test_extreme_sentinels_are_also_treated_as_nodata(tmp_path: Path) -> None:
    """Some runs write a large sentinel rather than the declared nodata value."""
    data = np.array([[1.0, 1e30], [3.0, -1e30]])
    surface = load_surface(_write(tmp_path / "dsm.tif", data, nodata=None))
    assert np.isnan(surface.data[0, 1]) and np.isnan(surface.data[1, 1])


def test_surface_without_crs_is_rejected(tmp_path: Path) -> None:
    """An ungeoreferenced surface cannot be tied to a field, so it fails early."""
    path = _write(tmp_path / "dsm.tif", np.ones((3, 3)), crs=None)
    with pytest.raises(ChmError, match="no CRS"):
        load_surface(path)


def test_unreadable_file_names_itself(tmp_path: Path) -> None:
    """The error should carry the path, not a bare rasterio exception."""
    broken = tmp_path / "broken.tif"
    broken.write_bytes(b"not a geotiff")
    with pytest.raises(ChmError, match="could not read"):
        load_surface(broken)


# --------------------------------------------------------------------------- #
# Grid alignment
# --------------------------------------------------------------------------- #


def test_matching_grids_are_returned_untouched() -> None:
    """No resampling should happen in the normal case."""
    reference = _surface(np.ones((8, 8)))
    source = _surface(np.full((8, 8), 2.0))
    assert align_to(source, reference).data is source.data


def test_mismatched_grids_are_resampled(caplog: pytest.LogCaptureFixture) -> None:
    """A DTM at a different resolution must be brought onto the DSM's grid.

    Subtracting mismatched arrays would either raise or, worse, broadcast into
    something silently wrong.
    """
    reference = _surface(np.ones((8, 8)), res=0.05)
    source = _surface(np.full((4, 4), 2.0), res=0.10)
    with caplog.at_level("WARNING"):
        aligned = align_to(source, reference)
    assert aligned.data.shape == (8, 8)
    assert "resampling to match" in caplog.text


# --------------------------------------------------------------------------- #
# The subtraction
# --------------------------------------------------------------------------- #


def test_canopy_height_is_surface_minus_ground() -> None:
    """The core arithmetic, on values chosen so the answer is obvious."""
    dsm = _surface(np.full((6, 6), 12.5))
    dtm = _surface(np.full((6, 6), 10.0))
    chm, stats = compute_chm(dsm, dtm, smooth_m=0.0, fill_holes_m2=0.0)
    assert np.allclose(chm, 2.5)
    assert stats.mean_m == pytest.approx(2.5)
    assert stats.coverage == pytest.approx(1.0)


def test_sub_ground_noise_is_clamped_to_zero() -> None:
    """Interpolation noise puts the surface below the ground; that is not canopy."""
    dsm = _surface(np.array([[10.0, 9.8], [11.0, 10.0]]))
    dtm = _surface(np.full((2, 2), 10.0))
    chm, stats = compute_chm(dsm, dtm, smooth_m=0.0, fill_holes_m2=0.0)
    assert chm.min() >= 0
    assert stats.n_clipped_negative == 1


def test_negatives_can_be_kept_for_inspection() -> None:
    """Sometimes you want to see how far below ground the surface went."""
    dsm = _surface(np.array([[9.0, 11.0]]))
    dtm = _surface(np.full((1, 2), 10.0))
    chm, _ = compute_chm(
        dsm, dtm, clamp_negative=False, smooth_m=0.0, fill_holes_m2=0.0
    )
    assert chm[0, 0] == pytest.approx(-1.0)


def test_impossible_heights_are_clipped() -> None:
    """No field crop is 40 m tall; that is a reconstruction artifact."""
    dsm = _surface(np.array([[12.0, 50.0]]))
    dtm = _surface(np.full((1, 2), 10.0))
    chm, stats = compute_chm(
        dsm, dtm, max_height_m=8.0, smooth_m=0.0, fill_holes_m2=0.0
    )
    assert chm.max() == pytest.approx(8.0)
    assert stats.n_clipped_tall == 1


def test_entirely_empty_model_fails_loudly() -> None:
    """An empty result means the surfaces did not overlap; say so."""
    dsm = _surface(np.full((4, 4), np.nan))
    dtm = _surface(np.full((4, 4), 10.0))
    with pytest.raises(ChmError, match="empty"):
        compute_chm(dsm, dtm, smooth_m=0.0, fill_holes_m2=0.0)


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #


def test_small_holes_fill_and_large_ones_do_not() -> None:
    """Interpolating a large void would invent canopy nobody observed."""
    array = np.ones((20, 20))
    array[2, 2] = np.nan                    # 1 pixel
    array[10:16, 10:16] = np.nan            # 36 pixels
    filled, n_filled = fill_small_holes(array, max_px=4)
    assert not np.isnan(filled[2, 2])
    assert np.isnan(filled[12, 12])
    assert n_filled == 1


def test_hole_filling_is_a_no_op_when_disabled() -> None:
    """Zero means leave every hole alone."""
    array = np.ones((5, 5))
    array[1, 1] = np.nan
    filled, n_filled = fill_small_holes(array, max_px=0)
    assert np.isnan(filled[1, 1])
    assert n_filled == 0


def test_smoothing_does_not_spread_holes() -> None:
    """A plain Gaussian filter would smear one NaN across its whole kernel."""
    array = np.ones((11, 11))
    array[5, 5] = np.nan
    smoothed = smooth_preserving_holes(array, sigma=1.5)
    assert np.isnan(smoothed[5, 5])
    assert np.isfinite(smoothed[5, 7])
    assert smoothed[0, 0] == pytest.approx(1.0, abs=1e-6)


def test_smoothing_preserves_a_flat_surface() -> None:
    """Smoothing must not shift the overall level, only remove speckle."""
    array = np.full((15, 15), 3.0)
    assert np.allclose(smooth_preserving_holes(array, sigma=2.0), 3.0)


def test_smoothing_off_is_exact() -> None:
    """Zero smoothing returns the array untouched."""
    array = np.array([[1.0, 5.0], [2.0, 9.0]])
    assert np.array_equal(smooth_preserving_holes(array, sigma=0.0), array)


# --------------------------------------------------------------------------- #
# Physical units
# --------------------------------------------------------------------------- #


def test_smoothing_is_resolution_independent() -> None:
    """The same --smooth must mean the same ground distance at any resolution.

    With the parameter in pixels, changing --dem-resolution between runs would
    silently change how much canopy structure the cleaning destroys.
    """
    def ridged(size: int, res: float) -> tuple[Surface, Surface]:
        x = np.arange(size) * res
        profile = 1.0 + np.cos(2 * np.pi * x / 0.76)
        canopy = np.tile(profile, (size, 1))
        return (
            _surface(10.0 + canopy, res=res),
            _surface(np.full((size, size), 10.0), res=res),
        )

    coarse, coarse_ground = ridged(80, 0.05)
    fine, fine_ground = ridged(200, 0.02)

    coarse_chm, _ = compute_chm(coarse, coarse_ground, smooth_m=0.06, fill_holes_m2=0)
    fine_chm, _ = compute_chm(fine, fine_ground, smooth_m=0.06, fill_holes_m2=0)

    # Equal ground smoothing must flatten the ridges by a comparable amount.
    coarse_range = coarse_chm.max() - coarse_chm.min()
    fine_range = fine_chm.max() - fine_chm.min()
    assert coarse_range == pytest.approx(fine_range, rel=0.15)


def test_hole_filling_area_is_resolution_independent() -> None:
    """A 0.25 m2 threshold must mean the same patch of ground at any resolution."""
    def with_hole(size: int, res: float, hole_px: int) -> tuple[Surface, Surface]:
        surface = np.full((size, size), 11.0)
        surface[5:5 + hole_px, 5:5 + hole_px] = np.nan
        return _surface(surface, res=res), _surface(np.full((size, size), 10.0), res=res)

    # A 0.2 m x 0.2 m hole: 4x4 px at 5 cm, 10x10 px at 2 cm. Both are 0.04 m2,
    # under the threshold, so both should fill.
    coarse, coarse_ground = with_hole(40, 0.05, 4)
    fine, fine_ground = with_hole(60, 0.02, 10)

    _, coarse_stats = compute_chm(coarse, coarse_ground, smooth_m=0, fill_holes_m2=0.25)
    _, fine_stats = compute_chm(fine, fine_ground, smooth_m=0, fill_holes_m2=0.25)
    assert coarse_stats.n_filled == 16
    assert fine_stats.n_filled == 100


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def test_saved_model_round_trips_with_its_crs(tmp_path: Path) -> None:
    """The written GeoTIFF must be readable and still georeferenced."""
    reference = _surface(np.ones((5, 5)))
    chm = np.full((5, 5), 1.75)
    chm[0, 0] = np.nan

    path = save_chm(chm, reference, tmp_path / "chm.tif")
    with rasterio.open(path) as dataset:
        read_back = dataset.read(1, masked=True).filled(np.nan)
        assert dataset.crs.to_string() == UTM
        assert dataset.descriptions[0] == "canopy_height_m"
    assert read_back[1, 1] == pytest.approx(1.75)
    assert np.isnan(read_back[0, 0])


def test_stats_expose_the_pixel_area_volume_needs() -> None:
    """Canopy volume integrates height over pixel area, so it must be right."""
    dsm = _surface(np.full((4, 4), 11.0), res=0.05)
    dtm = _surface(np.full((4, 4), 10.0), res=0.05)
    _, stats = compute_chm(dsm, dtm, smooth_m=0.0, fill_holes_m2=0.0)
    assert stats.resolution_m == pytest.approx(0.05)
    assert stats.pixel_area_m2 == pytest.approx(0.0025)
