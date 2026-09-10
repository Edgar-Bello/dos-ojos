"""Tests for per-unit structure and colour metrics against known answers."""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from dosojos_drone.metrics import (
    MetricsError,
    canopy_volume_voxel,
    colour_metrics,
    compute_metrics,
    dome_volume,
    label_raster,
    normalise_within_flight,
    points_from_chm,
    rgb_indices,
    structure_metrics,
    zonal_count,
    zonal_max,
    zonal_mean,
    zonal_sum,
)

UTM = "EPSG:32614"
RES = 0.05
ORIGIN = (600000.0, 2900000.0)
TRANSFORM = from_origin(*ORIGIN, RES, RES)


def _units(*boxes_m: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """Units from boxes given in metres relative to the raster's top-left."""
    x0, y0 = ORIGIN
    polygons = [box(x0 + a, y0 - d, x0 + c, y0 - b) for a, b, c, d in boxes_m]
    return gpd.GeoDataFrame(
        {"unit_id": [f"u{i}" for i in range(len(polygons))]},
        geometry=polygons, crs=UTM,
    )


def _dome(size: int, height: float, radius: float) -> np.ndarray:
    """An ellipsoidal crown centred in a square canopy model."""
    y, x = (np.mgrid[0:size, 0:size] + 0.5) * RES
    centre = size * RES / 2
    distance = np.hypot(x - centre, y - centre)
    return height * np.sqrt(np.clip(1 - (distance / radius) ** 2, 0, 1))


def _ortho(path: Path, rgb: np.ndarray, *, crs: str = UTM, res: float = 0.02) -> Path:
    """Write a three-band orthophoto."""
    with rasterio.open(
        path, "w", driver="GTiff", height=rgb.shape[1], width=rgb.shape[2],
        count=rgb.shape[0], dtype="uint8", crs=crs,
        transform=from_origin(*ORIGIN, res, res),
    ) as dataset:
        dataset.write(rgb.astype("uint8"))
    return path


# --------------------------------------------------------------------------- #
# Zonal machinery
# --------------------------------------------------------------------------- #


def test_zonal_reductions_on_a_known_label_image() -> None:
    """Sums, counts, maxima and means per label, on values chosen by hand."""
    labels = np.array([[1, 1, 2], [2, 2, 0]])
    values = np.array([[1.0, 3.0, 5.0], [7.0, np.nan, 100.0]])
    assert zonal_sum(values, labels, 2).tolist() == [4.0, 12.0]
    assert zonal_count(np.isfinite(values), labels, 2).tolist() == [2, 2]
    assert zonal_max(values, labels, 2).tolist() == [3.0, 7.0]
    assert zonal_mean(values, labels, 2).tolist() == [2.0, 6.0]


def test_unlabelled_pixels_are_ignored() -> None:
    """Label 0 is outside every unit and must never be counted."""
    labels = np.array([[0, 1]])
    values = np.array([[999.0, 2.0]])
    assert zonal_sum(values, labels, 1).tolist() == [2.0]


def test_a_unit_without_valid_pixels_reports_nan() -> None:
    """An empty unit must read as missing, not as zero."""
    labels = np.array([[1, 2]])
    values = np.array([[np.nan, 4.0]])
    means = zonal_mean(values, labels, 2)
    assert np.isnan(means[0]) and means[1] == 4.0
    assert np.isnan(zonal_max(values, labels, 2)[0])


def test_each_pixel_belongs_to_at_most_one_unit() -> None:
    """Adjacent segments must not double-count the pixels along their edge."""
    units = _units((0, 0, 1, 1), (1, 0, 2, 1))
    labels = label_raster(units, (20, 40), TRANSFORM)
    assert (labels == 1).sum() == 400
    assert (labels == 2).sum() == 400


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #


def test_volume_is_height_times_pixel_area() -> None:
    """A uniform 2 m canopy over one square metre holds 2 m3."""
    chm = np.full((20, 20), 2.0)
    table = structure_metrics(_units((0, 0, 1, 1)), chm, TRANSFORM)
    assert table["volume_m3"].iloc[0] == pytest.approx(2.0)
    assert table["height_max_m"].iloc[0] == pytest.approx(2.0)
    assert table["area_m2"].iloc[0] == pytest.approx(1.0)


def test_integrated_volume_matches_the_analytic_dome() -> None:
    """The strong check: integration against an exact answer, not another method.

    An ellipsoidal crown of radius r and height h holds two thirds pi r squared
    h. Comparing two numerical methods only shows they agree with each other.
    """
    chm = _dome(160, height=3.5, radius=1.8)
    table = structure_metrics(_units((0, 0, 8, 8)), chm, TRANSFORM)
    assert table["volume_m3"].iloc[0] == pytest.approx(dome_volume(3.5, 1.8), rel=0.01)


def test_dome_volume_formula() -> None:
    """Two thirds pi r squared h, checked against the hemisphere special case."""
    assert dome_volume(1.0, 1.0) == pytest.approx(2 * math.pi / 3)
    assert dome_volume(2.0, 3.0) == pytest.approx((2 / 3) * math.pi * 9 * 2)


def test_negative_canopy_contributes_no_volume() -> None:
    """Sub-ground noise must not subtract volume from the plant beside it."""
    chm = np.full((20, 20), 1.0)
    chm[:10] = -0.5
    table = structure_metrics(_units((0, 0, 1, 1)), chm, TRANSFORM)
    assert table["volume_m3"].iloc[0] == pytest.approx(0.5)


def test_cover_counts_only_canopy_above_the_threshold() -> None:
    """Soil clods and residue are not canopy."""
    chm = np.zeros((20, 20))
    chm[:5] = 1.0                         # a quarter of the unit is canopy
    table = structure_metrics(_units((0, 0, 1, 1)), chm, TRANSFORM, cover_height_m=0.15)
    assert table["canopy_cover"].iloc[0] == pytest.approx(0.25)


def test_nodata_reduces_coverage_rather_than_volume() -> None:
    """A hole is unknown, not bare ground, and coverage says how much is unknown."""
    chm = np.full((20, 20), 1.0)
    chm[:10] = np.nan
    table = structure_metrics(_units((0, 0, 1, 1)), chm, TRANSFORM)
    assert table["data_coverage"].iloc[0] == pytest.approx(0.5)
    assert table["volume_m3"].iloc[0] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Voxel cross-check
# --------------------------------------------------------------------------- #


def test_voxel_volume_agrees_with_integration_on_a_dome() -> None:
    """Two routes to the same quantity should land close together."""
    chm = _dome(160, height=3.5, radius=1.8)
    polygon = _units((0, 0, 8, 8)).geometry.iloc[0]
    points = points_from_chm(chm, TRANSFORM)
    voxel = canopy_volume_voxel(points, polygon, voxel_m=0.05, ground_z=0.0)
    assert voxel == pytest.approx(dome_volume(3.5, 1.8), rel=0.05)


def test_voxel_fills_columns_rather_than_counting_skin() -> None:
    """Photogrammetry sees surfaces; counting occupied voxels measures area.

    A 2 m tall block one metre square must read as 2 m3, not as the handful of
    voxels its top surface occupies.
    """
    xs, ys = np.meshgrid(np.arange(0, 1, 0.1) + 0.05, np.arange(0, 1, 0.1) + 0.05)
    top = np.column_stack([xs.ravel(), ys.ravel(), np.full(xs.size, 2.0)])
    polygon = box(0, 0, 1, 1)
    assert canopy_volume_voxel(top, polygon, voxel_m=0.1, ground_z=0.0) == pytest.approx(2.0)


def test_voxel_volume_ignores_points_outside_the_unit() -> None:
    """A neighbour's canopy must not leak into this unit's volume."""
    points = np.array([[5.0, 5.0, 3.0], [0.5, 0.5, 1.0]])
    volume = canopy_volume_voxel(points, box(0, 0, 1, 1), voxel_m=0.1, ground_z=0.0)
    assert volume == pytest.approx(0.01)


def test_voxel_volume_of_nothing_is_zero() -> None:
    """No points means no canopy, not an error."""
    assert canopy_volume_voxel(np.empty((0, 3)), box(0, 0, 1, 1)) == 0.0


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #


def _pixel(r: float, g: float, b: float) -> dict[str, float]:
    """Indices for a single RGB pixel."""
    out = rgb_indices(np.array([[r]]), np.array([[g]]), np.array([[b]]))
    return {name: float(values[0, 0]) for name, values in out.items()}


def test_green_leaf_scores_above_brown_soil() -> None:
    """Every index must rank a leaf greener than bare ground."""
    leaf, soil = _pixel(40, 117, 33), _pixel(107, 82, 61)
    for name in ("vari", "exg", "gli"):
        assert leaf[name] > soil[name], name


def test_grey_pixel_scores_zero_greenness() -> None:
    """Neutral grey has no green excess by construction."""
    grey = _pixel(100, 100, 100)
    assert grey["exg"] == pytest.approx(0.0)
    assert grey["gli"] == pytest.approx(0.0)


def test_exg_is_invariant_to_brightness() -> None:
    """A cloud shadow halves every band; it must not register as stress.

    This is why ExG is computed on chromatic coordinates rather than raw values.
    """
    bright = _pixel(80, 160, 60)
    shaded = _pixel(40, 80, 30)
    assert shaded["exg"] == pytest.approx(bright["exg"])


def test_vari_is_nan_where_its_denominator_vanishes() -> None:
    """G + R - B passes through zero over some soils; the ratio means nothing there."""
    assert math.isnan(_pixel(50, 50, 100)["vari"])


def test_black_pixels_have_no_indices() -> None:
    """ODM paints outside the reconstruction black; that is not a plant."""
    black = _pixel(0, 0, 0)
    assert math.isnan(black["exg"]) and math.isnan(black["gli"])


def test_colour_metrics_average_over_each_unit(tmp_path: Path) -> None:
    """A uniformly green unit reports that green's index as its mean."""
    rgb = np.zeros((3, 50, 50))
    rgb[:] = np.array([40, 117, 33])[:, None, None]
    path = _ortho(tmp_path / "ortho.tif", rgb)
    table = colour_metrics(_units((0, 0, 1, 1)), path)
    assert table["exg_mean"].iloc[0] == pytest.approx(_pixel(40, 117, 33)["exg"], abs=1e-6)


def test_colour_ignores_empty_ortho_pixels(tmp_path: Path) -> None:
    """Black fill outside the mosaic must not drag a unit's mean down."""
    rgb = np.zeros((3, 50, 50))
    rgb[:, :, 25:] = np.array([40, 117, 33])[:, None, None]
    table = colour_metrics(_units((0, 0, 1, 1)), _ortho(tmp_path / "o.tif", rgb))
    assert table["exg_mean"].iloc[0] == pytest.approx(_pixel(40, 117, 33)["exg"], abs=1e-6)


def test_units_in_another_crs_are_reprojected(tmp_path: Path) -> None:
    """Metrics must not silently read the wrong pixels when CRSs differ."""
    rgb = np.zeros((3, 50, 50))
    rgb[:] = np.array([40, 117, 33])[:, None, None]
    path = _ortho(tmp_path / "o.tif", rgb)
    units = _units((0, 0, 1, 1)).to_crs("EPSG:4326")
    table = colour_metrics(units, path)
    assert np.isfinite(table["exg_mean"].iloc[0])


def test_single_band_image_is_rejected(tmp_path: Path) -> None:
    """RGB indices need three bands; a DEM passed by mistake should say so."""
    path = _ortho(tmp_path / "o.tif", np.zeros((1, 10, 10)))
    with pytest.raises(MetricsError, match="three"):
        colour_metrics(_units((0, 0, 0.1, 0.1)), path)


# --------------------------------------------------------------------------- #
# Normalisation and assembly
# --------------------------------------------------------------------------- #


def test_within_flight_rank_is_a_percentile() -> None:
    """Ranks run 0 to 100 and preserve order."""
    import pandas as pd

    ranked = normalise_within_flight(
        pd.DataFrame({"exg_mean": [0.1, 0.3, 0.2, 0.4]}), ["exg_mean"]
    )
    assert ranked["exg_mean_pct"].tolist() == [25.0, 75.0, 50.0, 100.0]


def test_rank_is_unchanged_by_a_flight_wide_brightness_shift() -> None:
    """The reason for ranking: a whole flight shot under haze reads the same."""
    import pandas as pd

    clear = pd.DataFrame({"exg_mean": [0.10, 0.25, 0.18]})
    hazy = pd.DataFrame({"exg_mean": [0.05, 0.20, 0.13]})
    assert (
        normalise_within_flight(clear, ["exg_mean"])["exg_mean_pct"].tolist()
        == normalise_within_flight(hazy, ["exg_mean"])["exg_mean_pct"].tolist()
    )


def test_metrics_keep_geometry_and_crs(tmp_path: Path) -> None:
    """The output must stay mappable, so it can be joined and drawn."""
    chm = np.full((40, 40), 1.5)
    rgb = np.zeros((3, 100, 100))
    rgb[:] = np.array([40, 117, 33])[:, None, None]
    result = compute_metrics(
        _units((0, 0, 1, 1), (1, 0, 2, 1)), chm, TRANSFORM,
        ortho_path=_ortho(tmp_path / "o.tif", rgb),
    )
    assert result.crs.to_string() == UTM
    assert {"volume_m3", "exg_mean", "exg_mean_pct", "volume_m3_pct"} <= set(result.columns)


def test_missing_orthophoto_still_yields_structure(tmp_path: Path) -> None:
    """Colour is optional; a run without an orthophoto still measures canopy."""
    result = compute_metrics(
        _units((0, 0, 1, 1)), np.full((20, 20), 1.0), TRANSFORM,
        ortho_path=tmp_path / "absent.tif",
    )
    assert "volume_m3" in result and "exg_mean" not in result


def test_no_units_is_an_error() -> None:
    """Measuring nothing is a sign detection did not run."""
    empty = gpd.GeoDataFrame({"unit_id": []}, geometry=[], crs=UTM)
    with pytest.raises(MetricsError, match="no units"):
        compute_metrics(empty, np.zeros((5, 5)), TRANSFORM)


# --------------------------------------------------------------------------- #
# Test data integrity
# --------------------------------------------------------------------------- #


def test_synthetic_orthophoto_is_coregistered_with_its_dem(tmp_path: Path) -> None:
    """The generator's colour must sit on the same ground as its canopy.

    An earlier version repeated DEM pixels by a rounded scale factor, which
    stretched the orthophoto 1.2x and displaced it by up to 12 m. Colour was then
    sampled from the wrong ground, and bare gaps scored greener than healthy
    canopy. That looked exactly like ExG being a useless signal.
    """
    tool = Path(__file__).resolve().parents[1] / "tools" / "make_synthetic_field.py"
    subprocess.run(
        [sys.executable, str(tool), "coreg", "--pattern", "trees", "--extent", "30",
         "--odm-dir", str(tmp_path), "--holes", "0"],
        check=True, capture_output=True,
    )
    with rasterio.open(tmp_path / "truth_canopy.tif") as dem:
        canopy = dem.read(1, masked=True).filled(np.nan)
        dem_transform = dem.transform
    with rasterio.open(tmp_path / "odm_orthophoto" / "odm_orthophoto.tif") as ortho:
        red, green, blue = (ortho.read(i).astype(float) for i in (1, 2, 3))
        ortho_transform = ortho.transform
        ortho_shape = (ortho.height, ortho.width)

    exg = rgb_indices(red, green, blue)["exg"]
    rows, cols = np.nonzero(np.isfinite(canopy))
    xs, ys = rasterio.transform.xy(dem_transform, rows, cols, offset="center")
    orow, ocol = rasterio.transform.rowcol(ortho_transform, xs, ys)
    orow = np.clip(np.asarray(orow), 0, ortho_shape[0] - 1)
    ocol = np.clip(np.asarray(ocol), 0, ortho_shape[1] - 1)

    correlation = np.corrcoef(canopy[rows, cols], exg[orow, ocol])[0, 1]
    assert correlation > 0.9
