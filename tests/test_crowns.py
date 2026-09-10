"""Tests for row geometry, segmentation and crown detection against known input."""

from __future__ import annotations

import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from dosojos_drone.crowns import (
    MIN_ROW_STRENGTH,
    DetectionError,
    RowGeometry,
    _across_coordinates,
    build_row_segments,
    clip_units,
    detect_crowns_deepforest,
    detect_crowns_watershed,
    estimate_row_geometry,
    to_geodataframe,
)

UTM = rasterio.crs.CRS.from_string("EPSG:32614")
RES = 0.05


def ridged_field(
    size: int = 400,
    *,
    spacing_m: float = 0.76,
    bearing_deg: float = 12.0,
    height_m: float = 2.4,
    res: float = RES,
) -> np.ndarray:
    """A canopy of parallel rows, built the way the field generator builds them.

    ``bearing_deg`` is the direction of the wave, so the rows themselves run
    ninety degrees from it.
    """
    y, x = np.mgrid[0:size, 0:size] * res
    theta = math.radians(bearing_deg)
    across = x * math.cos(theta) + y * math.sin(theta)
    return height_m * np.clip(np.cos(2 * math.pi * across / spacing_m), 0, None) ** 0.6


def domed_field(
    size: int = 300, *, spacing_m: float = 5.0, radius_m: float = 1.8,
    height_m: float = 3.5, res: float = RES,
) -> tuple[np.ndarray, int]:
    """Discrete crowns on a grid; returns the canopy and how many were planted."""
    y, x = np.mgrid[0:size, 0:size] * res
    canopy = np.zeros((size, size))
    planted = 0
    for cy in np.arange(spacing_m, size * res - spacing_m, spacing_m):
        for cx in np.arange(spacing_m, size * res - spacing_m, spacing_m):
            distance = np.hypot(x - cx, y - cy)
            dome = height_m * np.sqrt(np.clip(1 - (distance / radius_m) ** 2, 0, 1))
            canopy = np.maximum(canopy, dome)
            planted += 1
    return canopy, planted


# --------------------------------------------------------------------------- #
# Row geometry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("spacing", [0.5, 0.76, 1.5])
def test_row_spacing_is_recovered(spacing: float) -> None:
    """Spacing comes from the frequency of the canopy's periodic peak."""
    geometry = estimate_row_geometry(ridged_field(spacing_m=spacing), RES)
    assert geometry.spacing_m == pytest.approx(spacing, rel=0.05)


@pytest.mark.parametrize("bearing", [0.0, 12.0, 45.0, 80.0])
def test_row_direction_is_recovered(bearing: float) -> None:
    """Rows run ninety degrees from the direction the canopy varies in."""
    geometry = estimate_row_geometry(ridged_field(bearing_deg=bearing), RES)
    expected = (bearing + 90.0) % 180.0
    difference = abs(geometry.direction_deg - expected) % 180.0
    assert min(difference, 180.0 - difference) < 3.0


def test_periodic_canopy_reports_a_strong_signal() -> None:
    """Clean rows should be unambiguous."""
    assert estimate_row_geometry(ridged_field(), RES).strength > MIN_ROW_STRENGTH
    assert estimate_row_geometry(ridged_field(), RES).confident


def test_random_canopy_reports_a_weak_signal() -> None:
    """Noise has no planting pattern, and the tool must say so rather than invent one.

    The estimate still returns numbers, since something is always the largest
    peak; the strength is what tells you not to trust them.
    """
    rng = np.random.default_rng(3)
    noise = rng.random((400, 400)) * 2.0
    assert estimate_row_geometry(noise, RES).strength < MIN_ROW_STRENGTH


def test_lumpy_canopy_without_rows_is_not_mistaken_for_rows() -> None:
    """A closed or uneven canopy has structure but no planting pattern.

    This is the case a median-based strength gets wrong: a smooth random field
    scores 340 against the median and would read as confident rows, but only 4.4
    against the 99th percentile.
    """
    rng = np.random.default_rng(3)
    lumpy = np.cumsum(np.cumsum(rng.normal(0, 1, (400, 400)), 0), 1) / 1e3
    geometry = estimate_row_geometry(lumpy, RES)
    assert geometry.strength < MIN_ROW_STRENGTH
    assert not geometry.confident


def test_rows_survive_heavy_noise() -> None:
    """The signal must stay findable when the canopy model is poor."""
    rng = np.random.default_rng(5)
    noisy = ridged_field() + rng.normal(0, 1.5, (400, 400))
    geometry = estimate_row_geometry(noisy, RES)
    assert geometry.confident
    assert geometry.spacing_m == pytest.approx(0.76, rel=0.05)


def test_flat_canopy_has_no_pattern_to_find() -> None:
    """A field with no canopy variation fails rather than returning nonsense."""
    with pytest.raises(DetectionError, match="flat"):
        estimate_row_geometry(np.zeros((200, 200)), RES)


def test_impossible_spacing_range_is_rejected() -> None:
    """A range no frequency can satisfy should say so, not return the DC peak."""
    with pytest.raises(DetectionError, match="no frequencies"):
        estimate_row_geometry(
            ridged_field(size=100), RES, min_spacing_m=50.0, max_spacing_m=100.0
        )


def test_phase_puts_lines_on_rows_not_in_furrows() -> None:
    """Right spacing and angle with the wrong phase measures the furrow.

    Every segment would then sit between two rows, and every canopy volume
    downstream would describe bare ground.
    """
    canopy = ridged_field()
    geometry = estimate_row_geometry(canopy, RES)
    across = _across_coordinates(canopy.shape, RES, geometry.across_deg)
    offset = np.mod(across - geometry.phase_m, geometry.spacing_m)

    on_ridge = (offset < 0.08) | (offset > geometry.spacing_m - 0.08)
    in_furrow = np.abs(offset - geometry.spacing_m / 2) < 0.08
    assert canopy[on_ridge].mean() > 3 * canopy[in_furrow].mean()


# --------------------------------------------------------------------------- #
# Segmentation
# --------------------------------------------------------------------------- #


def _geometry(**kwargs) -> RowGeometry:
    """A row geometry with sensible defaults."""
    defaults = dict(spacing_m=0.76, direction_deg=102.0, phase_m=0.2, strength=50.0)
    defaults.update(kwargs)
    return RowGeometry(**defaults)


def test_segments_have_the_requested_shape() -> None:
    """Segment area should be length times width, give or take clipping."""
    canopy = ridged_field(size=400)
    segments = build_row_segments(canopy, RES, _geometry(), segment_m=2.0)
    areas = [polygon.area for _, _, polygon in segments]
    assert len(segments) > 100
    assert np.median(areas) == pytest.approx(2.0 * 0.76, rel=0.1)


def test_segment_length_is_configurable() -> None:
    """Shorter segments localise a gap more precisely, at more units."""
    canopy = ridged_field(size=400)
    short = build_row_segments(canopy, RES, _geometry(), segment_m=1.0)
    long = build_row_segments(canopy, RES, _geometry(), segment_m=4.0)
    assert len(short) > len(long)


def test_segment_width_can_be_narrowed() -> None:
    """A narrower strip measures the row without the furrow either side."""
    canopy = ridged_field(size=400)
    segments = build_row_segments(canopy, RES, _geometry(), segment_m=2.0, width_m=0.4)
    assert np.median([p.area for _, _, p in segments]) == pytest.approx(0.8, rel=0.15)


def test_segments_stay_inside_the_raster() -> None:
    """Nothing may extend past the canopy model that produced it."""
    canopy = ridged_field(size=300)
    extent = box(0, 0, 300 * RES, 300 * RES)
    for _, _, polygon in build_row_segments(canopy, RES, _geometry()):
        assert extent.buffer(1e-9).contains(polygon)


def test_segments_cover_most_of_the_field() -> None:
    """Full-width segments should tile the field, not sample it."""
    canopy = ridged_field(size=300)
    segments = build_row_segments(canopy, RES, _geometry(), segment_m=2.0)
    covered = sum(polygon.area for _, _, polygon in segments)
    assert covered > 0.8 * (300 * RES) ** 2


# --------------------------------------------------------------------------- #
# Watershed
# --------------------------------------------------------------------------- #


def test_watershed_finds_every_planted_crown() -> None:
    """One polygon per dome, on a field where the count is known exactly."""
    canopy, planted = domed_field()
    crowns = detect_crowns_watershed(
        canopy, RES, min_height_m=0.6, min_distance_m=2.5
    )
    assert len(crowns) == planted


def test_watershed_crown_area_matches_the_dome() -> None:
    """Crown polygons should approximate the disc they were grown from."""
    canopy, _ = domed_field(radius_m=1.8)
    crowns = detect_crowns_watershed(canopy, RES, min_height_m=0.6, min_distance_m=2.5)
    areas = [polygon.area for _, polygon in crowns]
    assert np.median(areas) == pytest.approx(math.pi * 1.8**2, rel=0.25)


def test_watershed_needs_something_above_the_ground_threshold() -> None:
    """A bare field has no crowns, and the message should name the knob."""
    with pytest.raises(DetectionError, match="min-height"):
        detect_crowns_watershed(np.zeros((100, 100)), RES, min_height_m=0.5)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _transform():
    """A north-up transform at the test resolution."""
    return from_origin(608480.0, 2900140.0, RES, RES)


def test_units_become_a_georeferenced_frame() -> None:
    """Detections are in grid space; everything downstream needs world space."""
    canopy = ridged_field(size=200)
    segments = build_row_segments(canopy, RES, _geometry())
    frame = to_geodataframe(segments, _transform(), UTM, method="rows")

    assert frame.crs == UTM
    assert frame.geometry.iloc[0].bounds[0] > 600000      # eastings, not pixels
    assert len(frame) == len(segments)


def test_unit_ids_are_stable_and_sorted() -> None:
    """The same ground must get the same id on every run."""
    canopy = ridged_field(size=200)
    segments = build_row_segments(canopy, RES, _geometry())
    first = to_geodataframe(segments, _transform(), UTM, method="rows")
    second = to_geodataframe(segments, _transform(), UTM, method="rows")

    assert list(first["unit_id"]) == list(second["unit_id"])
    assert list(first["unit_id"]) == sorted(first["unit_id"])
    assert first["unit_id"].is_unique


def test_row_and_segment_indices_are_carried() -> None:
    """Gap analysis needs to know which row a segment belongs to."""
    canopy = ridged_field(size=200)
    frame = to_geodataframe(
        build_row_segments(canopy, RES, _geometry()), _transform(), UTM, method="rows"
    )
    assert {"row", "segment"} <= set(frame.columns)
    # Row indices are geometric, not dense: a row line clipped entirely away
    # leaves a gap, which is exactly what step 7 needs to spot a missing row.
    assert frame["row"].min() >= 0
    assert frame["row"].is_monotonic_increasing


def test_row_geometry_travels_with_the_frame() -> None:
    """Later steps need the spacing to reason about gaps between rows."""
    canopy = ridged_field(size=200)
    geometry = _geometry()
    frame = to_geodataframe(
        build_row_segments(canopy, RES, geometry), _transform(), UTM,
        method="rows", geometry=geometry,
    )
    assert frame.attrs["row_spacing_m"] == pytest.approx(0.76)


def test_slivers_at_the_field_edge_are_dropped() -> None:
    """A segment clipped to a sliver reports the boundary, not the crop."""
    import geopandas as gpd
    from shapely.geometry import Polygon

    inside = box(608490, 2900100, 608492, 2900102)
    straddling = box(608479, 2900100, 608481, 2900102)
    frame = gpd.GeoDataFrame(
        {"unit_id": ["a", "b"]}, geometry=[inside, straddling], crs=UTM
    )
    # A field polygon in WGS84 covering only the eastern part.
    from pyproj import Transformer

    to_wgs = Transformer.from_crs(UTM, "EPSG:4326", always_xy=True).transform
    field = Polygon([to_wgs(x, y) for x, y in
                     box(608485, 2900090, 608540, 2900140).exterior.coords])

    kept = clip_units(frame, field, min_fraction=0.5)
    assert list(kept["unit_id"]) == ["a"]


# --------------------------------------------------------------------------- #
# deepforest
# --------------------------------------------------------------------------- #


def test_deepforest_absence_explains_itself() -> None:
    """It is optional, so the error must say how to get it and why it is not default."""
    pytest.importorskip  # noqa: B018 - readability
    try:
        import deepforest  # noqa: F401
    except ImportError:
        with pytest.raises(DetectionError, match="pip install deepforest"):
            detect_crowns_deepforest("nonexistent.tif")
    else:
        pytest.skip("deepforest is installed, so the guidance path cannot run")
