"""Tests for stitching a thermal scan: leaves by contrast, the camera's own drift, one map.

The synthetic scan these are built on is a crop of cold stripes on hot soil,
photographed a frame at a time with overlap, with a wandering camera reading
added on top. Everything here asks the same question in different ways: does the
map come back showing the crop rather than the camera?
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from dosojos_drone import mosaic

EPSG = 32612
PIXEL_M = 0.01                              # 1 cm camera pixels
FRAME = (60, 48)                            # rows, columns: 0.60 by 0.48 m
X0, Y0 = 409000.0, 3660100.0                # somewhere in Arizona, in metres
SOIL_C, LEAF_C = 40.0, 30.0
ROW_SPACING_M, LEAF_WIDTH_M = 0.30, 0.08
START = datetime(2018, 5, 20, 9, 22, 56)


def _truth(width_m: float = 3.0, height_m: float = 2.4, *, warm=None,
           seed: int = 5) -> np.ndarray:
    """The field as it really is: hot soil with cold leaf stripes down it.

    ``warm`` is (x_m, y_m, radius_m, degrees): leaves in that circle run hot,
    which is the thing a thermal step exists to find.
    """
    rng = np.random.default_rng(seed)
    rows, cols = int(height_m / PIXEL_M), int(width_m / PIXEL_M)
    x = (np.arange(cols) + 0.5) * PIXEL_M
    y = (np.arange(rows) + 0.5) * PIXEL_M
    leaf = np.broadcast_to((x % ROW_SPACING_M) < LEAF_WIDTH_M, (rows, cols))
    field = np.where(leaf, LEAF_C, SOIL_C).astype(float)
    if warm:
        cx, cy, radius, degrees = warm
        inside = ((x[None, :] - cx) ** 2 + (y[:, None] - cy) ** 2) <= radius ** 2
        field = np.where(inside & leaf, field + degrees, field)
    return field + rng.normal(0, 0.15, field.shape)


def _cut(truth: np.ndarray, folder: Path, *, offsets=None, times=True,
         crs: int | None = EPSG, rotated: bool = False) -> list[Path]:
    """Photograph ``truth`` frame by frame, with overlap and a wandering reading."""
    folder.mkdir(parents=True, exist_ok=True)
    rows, cols = truth.shape
    step_r, step_c = FRAME[0] // 2, FRAME[1] // 2
    starts = [(r, c) for r in range(0, rows - FRAME[0] + 1, step_r)
              for c in range(0, cols - FRAME[1] + 1, step_c)]
    written = []
    for number, (r, c) in enumerate(starts):
        cut = truth[r:r + FRAME[0], c:c + FRAME[1]].copy()
        if offsets is not None:
            cut = cut + offsets(number)
        transform = from_origin(X0 + c * PIXEL_M, Y0 - r * PIXEL_M, PIXEL_M, PIXEL_M)
        if rotated:
            transform = rasterio.Affine(PIXEL_M, 0.02, X0, 0.02, -PIXEL_M, Y0)
        path = folder / f"frame_{number:03d}.tif"
        with rasterio.open(path, "w", driver="GTiff", height=FRAME[0], width=FRAME[1],
                           count=1, dtype="float32", crs=f"EPSG:{crs}" if crs else None,
                           transform=transform) as dataset:
            dataset.write((cut + 273.15).astype(np.float32), 1)   # cameras write Kelvin
            if times:
                dataset.update_tags(datetime=(START + timedelta(seconds=3 * number)).isoformat())
        written.append(path)
    return written


def _map(path: Path) -> tuple[np.ndarray, rasterio.Affine]:
    with rasterio.open(path) as dataset:
        return dataset.read(1), dataset.transform


# --------------------------------------------------------------------------- #
# Finding and reading frames
# --------------------------------------------------------------------------- #


def test_frames_are_found_however_deep_the_camera_buried_them(tmp_path: Path):
    for name in ("a/b/one.tif", "a/two.TIF", "three.tiff", "notes.txt"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    # Sorted by path, which is capture order for every "one folder per frame"
    # naming a camera has used so far, since those folders are timestamps.
    assert [p.name for p in mosaic.find_frames(tmp_path)] == ["one.tif", "two.TIF", "three.tiff"]


def test_a_folder_with_no_frames_says_what_it_wanted(tmp_path: Path):
    (tmp_path / "holiday.jpg").write_bytes(b"x")
    with pytest.raises(mosaic.MosaicError, match="georeferenced frames"):
        mosaic.find_frames(tmp_path)


@pytest.mark.parametrize("tags, name, expected", [
    ({"datetime": "2018-05-20T09:22:56-07:00"}, "x.tif", datetime(2018, 5, 20, 9, 22, 56)),
    ({"TIFFTAG_DATETIME": "2018:05:20 09:22:56"}, "x.tif", datetime(2018, 5, 20, 9, 22, 56)),
    ({}, "ir_geotiff_L1_ua-mac_2018-05-20__09-22-56-936.tif", datetime(2018, 5, 20, 9, 22, 56)),
    ({}, "frame_12.tif", None),
])
def test_when_a_frame_was_taken_comes_from_the_tags_or_the_name(tags, name, expected):
    found = mosaic.frame_time(Path(name), tags)
    assert (found.replace(tzinfo=None) if found else None) == expected


# --------------------------------------------------------------------------- #
# Leaves, told from soil by contrast
# --------------------------------------------------------------------------- #


def test_leaves_are_found_without_any_canopy_model():
    truth = _truth()
    leaf = mosaic.leaves_of(truth, pixel_m=PIXEL_M)
    really_leaf = truth < (SOIL_C + LEAF_C) / 2
    agree = (leaf == really_leaf).mean()
    assert agree > 0.95, f"only {agree:.0%} of pixels classified as the field really is"


def test_ground_in_the_shade_is_not_mistaken_for_a_leaf():
    """Shade is a degree or two below the sun; a drinking leaf is many more."""
    truth = _truth()
    shaded = truth.copy()
    shaded[:, 120:160] -= 1.5                     # a 40 cm band of shadow across soil and all
    leaf = mosaic.leaves_of(shaded, pixel_m=PIXEL_M)
    soil_in_shade = leaf[:, 125:155][shaded[:, 125:155] > (SOIL_C + LEAF_C) / 2]
    assert soil_in_shade.mean() < 0.02


def test_a_field_with_nothing_growing_yet_finds_no_leaves():
    bare = np.full((80, 80), 42.0) + np.random.default_rng(1).normal(0, 0.2, (80, 80))
    assert mosaic.leaves_of(bare, pixel_m=PIXEL_M).mean() < 0.001


def test_the_ground_around_a_plant_is_measured_as_ground():
    soil = np.full((60, 60), 45.0)
    soil[25:35, 25:35] = 28.0                     # one plant in the middle of the window
    around = mosaic.ground_around(soil, block_px=20)
    assert around[30, 30] > 40.0


# --------------------------------------------------------------------------- #
# The camera wandering while it worked
# --------------------------------------------------------------------------- #


def test_the_trend_follows_a_warming_morning_and_ignores_one_wild_frame():
    seconds = np.arange(0, 3600, 30.0)
    warming = 28.0 + seconds / 3600.0 * 6.0
    warming[40] += 12.0                            # one frame the shutter caught mid-cycle
    trend = mosaic.drift_trend(seconds, warming, half_window_s=300.0)
    assert abs(trend[40] - 28.0 - seconds[40] / 3600.0 * 6.0) < 0.6
    assert trend[-1] - trend[0] == pytest.approx(6.0, abs=1.0)


def test_a_wandering_camera_reading_does_not_reach_the_map(tmp_path: Path):
    """The map has to show the crop, not the camera: this is the whole point."""
    truth = _truth()
    wander = lambda n: 2.5 * np.sin(n / 3.0) + (1.5 if n % 7 == 0 else 0.0)   # noqa: E731
    scan, thermal_path, leaves_path = mosaic.build(
        _cut(truth, tmp_path / "frames", offsets=wander), tmp_path / "out", cell_m=0.05)

    heat, _ = _map(thermal_path)
    measured = heat[np.isfinite(heat)]
    assert measured.size > 100
    assert float(np.median(measured)) == pytest.approx(LEAF_C, abs=0.5)
    # ...and no part of the map is left holding one frame's error.
    assert float(np.std(measured)) < 1.0
    assert scan.levelled_c and scan.levelled_c > 0.3


def test_a_patch_of_hot_crop_survives_all_that_correcting(tmp_path: Path):
    """Taking out the camera must not take out the thing we are looking for."""
    truth = _truth(warm=(1.5, 1.2, 0.35, 4.0))
    wander = lambda n: 2.0 * np.sin(n / 4.0)                                  # noqa: E731
    scan, thermal_path, _ = mosaic.build(
        _cut(truth, tmp_path / "frames", offsets=wander), tmp_path / "out", cell_m=0.05)

    heat, transform = _map(thermal_path)
    rows, cols = np.indices(heat.shape)
    x = X0 + (cols + 0.5) * 0.05
    y = Y0 - (rows + 0.5) * 0.05
    inside = ((x - (X0 + 1.5)) ** 2 + (y - (Y0 - 1.2)) ** 2) <= 0.3 ** 2
    warm = heat[inside & np.isfinite(heat)]
    elsewhere = heat[~inside & np.isfinite(heat)]
    assert float(np.median(warm)) - float(np.median(elsewhere)) > 3.0


def test_frames_that_never_overlap_are_still_held_to_the_warming_trend(tmp_path: Path):
    """Two strips with a gap between them: nothing ties one to the other but time."""
    truth = _truth(height_m=1.2)
    north = _cut(truth, tmp_path / "north")
    far = _truth(height_m=1.2, seed=9)
    folder = tmp_path / "south"
    folder.mkdir()
    south = []
    for number, path in enumerate(_cut(far, tmp_path / "south_raw")):
        with rasterio.open(path) as dataset:
            data, transform = dataset.read(1), dataset.transform
        moved = rasterio.Affine(transform.a, transform.b, transform.c,
                                transform.d, transform.e, transform.f - 5.0)
        out = folder / path.name
        with rasterio.open(out, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
                           count=1, dtype="float32", crs=f"EPSG:{EPSG}", transform=moved) as ds:
            ds.write(data + 2.0, 1)           # the camera read 2 C high down there
            ds.update_tags(datetime=(START + timedelta(seconds=600 + 3 * number)).isoformat())
        south.append(out)

    scan, thermal_path, _ = mosaic.build(north + south, tmp_path / "out", cell_m=0.05)
    heat, _ = _map(thermal_path)
    top = heat[:int(1.2 / 0.05)]
    bottom = heat[int(5.0 / 0.05):]
    gap = abs(float(np.nanmedian(top)) - float(np.nanmedian(bottom)))
    assert gap < 1.5, f"the two strips came out {gap:.1f} C apart"


# --------------------------------------------------------------------------- #
# What the map is, and what it says about itself
# --------------------------------------------------------------------------- #


def test_the_map_is_metric_even_when_the_frames_are_in_degrees(tmp_path: Path):
    """A camera writing lon/lat has to end up on ground anyone can measure in metres."""
    truth = _truth(width_m=1.5, height_m=1.2)
    folder = tmp_path / "frames"
    folder.mkdir()
    paths = []
    degrees = 0.01 / 111_320.0
    for number, path in enumerate(_cut(truth, tmp_path / "metric")):
        with rasterio.open(path) as dataset:
            data, transform = dataset.read(1), dataset.transform
        lon = -111.975 + (transform.c - X0) * degrees / 0.01 * 0.01
        lat = 33.076 + (transform.f - Y0) * degrees / 0.01 * 0.01
        out = folder / path.name
        with rasterio.open(out, "w", driver="GTiff", height=data.shape[0], width=data.shape[1],
                           count=1, dtype="float32", crs="EPSG:4326",
                           transform=from_origin(lon, lat, degrees, degrees)) as ds:
            ds.write(data, 1)
            ds.update_tags(datetime=(START + timedelta(seconds=3 * number)).isoformat())
        paths.append(out)

    scan, thermal_path, _ = mosaic.build(paths, tmp_path / "out", cell_m=0.05)
    with rasterio.open(thermal_path) as dataset:
        assert dataset.crs.to_epsg() == 32612
        assert dataset.res[0] == pytest.approx(0.05)


def test_the_scan_reports_what_it_did_and_what_it_cost(tmp_path: Path):
    truth = _truth()
    paths = _cut(truth, tmp_path / "frames", offsets=lambda n: n * 0.02)   # a steady warming
    # A short scan needs a short window, or the trend covers the whole of it and
    # reads no warming at all; the default is sized for a scan of an hour or two.
    scan, thermal_path, leaves_path = mosaic.build(
        paths, tmp_path / "out", cell_m=0.05, half_window_s=45.0)

    assert scan.frames == len(paths) and scan.frames_skipped == 0
    assert scan.unit == "kelvin"
    assert scan.minutes == pytest.approx(3 * (len(paths) - 1) / 60, abs=0.1)
    assert scan.drift_c is not None and scan.drift_c > 0.5
    assert scan.leaf_m2 > 0 and scan.leaf_m2 < scan.ground_m2
    assert any("lining every frame up" in note for note in scan.notes)
    assert [point["minute"] for point in scan.warming] == sorted(
        point["minute"] for point in scan.warming)

    fraction, _ = _map(leaves_path)
    seen = fraction[np.isfinite(fraction)]
    assert float(np.mean(seen)) == pytest.approx(LEAF_WIDTH_M / ROW_SPACING_M, abs=0.08)


def test_an_unreadable_frame_costs_that_frame_and_nothing_else(tmp_path: Path):
    paths = _cut(_truth(), tmp_path / "frames")
    paths[3].write_bytes(b"not a tiff at all")
    scan, thermal_path, _ = mosaic.build(paths, tmp_path / "out", cell_m=0.05)
    assert scan.frames_skipped == 1
    assert scan.frames == len(paths) - 1


def test_a_frame_with_no_coordinates_is_refused(tmp_path: Path):
    paths = _cut(_truth(width_m=1.0, height_m=0.8), tmp_path / "frames", crs=None)
    with pytest.raises(mosaic.MosaicError, match="carries no CRS"):
        mosaic.build(paths, tmp_path / "out")


def test_a_rotated_frame_is_refused_rather_than_placed_wrongly(tmp_path: Path):
    paths = _cut(_truth(width_m=1.0, height_m=0.8), tmp_path / "frames", rotated=True)
    with pytest.raises(mosaic.MosaicError, match="rotated"):
        mosaic.build(paths, tmp_path / "out")


def test_a_grid_too_fine_for_the_ground_is_refused(tmp_path: Path):
    paths = _cut(_truth(width_m=1.0, height_m=0.8), tmp_path / "frames")
    with pytest.raises(mosaic.MosaicError, match="far too fine"):
        mosaic.build(paths, tmp_path / "out", cell_m=0.00002)


def test_a_picture_that_is_not_temperatures_is_refused(tmp_path: Path):
    """A grey JPEG turned into a GeoTIFF carries pixel values, not degrees."""
    folder = tmp_path / "frames"
    folder.mkdir()
    grey = np.linspace(0, 255, 60 * 48).reshape(60, 48)
    for number in range(4):
        with rasterio.open(folder / f"grey_{number}.tif", "w", driver="GTiff", height=60,
                           width=48, count=1, dtype="float32", crs=f"EPSG:{EPSG}",
                           transform=from_origin(X0 + number * 0.2, Y0, PIXEL_M, PIXEL_M)) as ds:
            ds.write(grey.astype(np.float32), 1)
    with pytest.raises(mosaic.MosaicError, match="no frame could be placed|no cell holds"):
        mosaic.build(mosaic.find_frames(folder), tmp_path / "out")


def test_empty_pixels_do_not_spread_into_the_ground_around_them() -> None:
    """A frame placed from a photo has empty corners; the ground next to them still counts."""
    celsius = np.full((60, 60), 40.0)
    leaf = np.zeros((60, 60), bool)
    leaf[::6, ::6] = leaf[1::6, 1::6] = True     # small leaves, 7 degrees cooler than soil
    celsius[leaf] = 33.0
    celsius[:10, :10] = np.nan                   # a corner no photo reached
    celsius[25, 45] = np.nan
    leaves = mosaic.leaves_of(celsius, pixel_m=0.01, window_m=0.1)
    seen = leaf & np.isfinite(celsius)
    assert leaves[seen].mean() > 0.95            # right up to the empty corner
    assert not leaves[~leaf].any()
