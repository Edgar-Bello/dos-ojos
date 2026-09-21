"""Judging a field square by square from its colour picture alone."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from shapely.geometry import box

from dosojos_drone import colour

CRS = "EPSG:32614"
WEST, NORTH = 583_000.0, 2_900_040.0
PER_PX = 0.05


def _ortho(tmp_path, paint) -> tuple:
    """A 40 x 40 m field at 5 cm: rows of leaf on soil, then whatever ``paint`` does."""
    n = int(40 / PER_PX)
    rng = np.random.default_rng(0)
    rgb = np.empty((3, n, n), np.float64)
    rgb[:] = np.array([150, 120, 90])[:, None, None]          # soil
    leaf = np.zeros((n, n), bool)
    for start in range(0, n, 15):                              # rows 0.75 m apart, 0.5 m wide
        leaf[:, start:start + 10] = True
    rgb[:, leaf] = np.array([70, 140, 50])[:, None]
    rgb += rng.normal(0, 6, rgb.shape)
    paint(rgb, leaf)
    path = tmp_path / "ortho.tif"
    with rasterio.open(path, "w", driver="GTiff", width=n, height=n, count=3, dtype="uint8",
                       crs=CRS, transform=Affine(PER_PX, 0, WEST, 0, -PER_PX, NORTH)) as out:
        out.write(np.clip(rgb, 0, 255).astype(np.uint8))
    return path, n


def _square(x0: float, y0: float, size: float) -> tuple:
    """Rows and columns of the ortho covering a square, from its NW corner in metres."""
    return (slice(int(y0 / PER_PX), int((y0 + size) / PER_PX)),
            slice(int(x0 / PER_PX), int((x0 + size) / PER_PX)))


def _flag_at(cells, x: float, y: float) -> str:
    """The flag of the square holding a point, given in metres from the NW corner."""
    from shapely.geometry import Point

    # Nudged off the 1 m grid, so the point sits inside one square, not on a corner.
    hit = cells[cells.contains(Point(WEST + x + 0.3, NORTH - y - 0.3))]
    return hit.iloc[0]["flag"]


def test_a_gap_in_the_stand_is_missing_and_a_thin_stretch_is_weak(tmp_path) -> None:
    def paint(rgb, leaf):
        rows, cols = _square(5, 5, 4)
        rgb[:, rows, cols] = np.array([150, 120, 90])[:, None, None]      # plants gone
        rows, cols = _square(25, 25, 4)
        thin = np.zeros(leaf.shape, bool)
        thin[rows, cols] = True
        thin &= leaf
        thin[:, ::2] = False                                             # half the leaf gone
        rgb[:, thin] = np.array([150, 120, 90])[:, None]

    path, _ = _ortho(tmp_path, paint)
    cells, result = colour.judge(path)
    assert 0.6 <= result.field_cover <= 0.8           # rows of 0.5 m every 0.75 m
    assert _flag_at(cells, 7, 7) == "MISSING"
    assert _flag_at(cells, 27, 27) == "STRESSED"
    assert _flag_at(cells, 15, 15) == "HEALTHY"
    flagged = cells[cells.flag != "HEALTHY"]
    assert 16 <= (flagged.flag == "MISSING").sum() <= 30      # the 4 x 4 m gap, give or take its edge
    assert len(flagged) < 0.05 * len(cells)


def test_pale_leaves_are_weak_though_the_cover_is_full(tmp_path) -> None:
    def paint(rgb, leaf):
        rows, cols = _square(20, 5, 3)
        pale = np.zeros(leaf.shape, bool)
        pale[rows, cols] = True
        rgb[:, pale & leaf] = np.array([120, 145, 80])[:, None]           # yellowing leaves

    path, _ = _ortho(tmp_path, paint)
    cells, _ = colour.judge(path)
    assert _flag_at(cells, 21.5, 6.5) == "STRESSED"


def test_a_field_with_no_crop_up_yet_flags_nothing(tmp_path) -> None:
    def paint(rgb, leaf):
        rgb[:, leaf] = np.array([150, 120, 90])[:, None]                  # bare soil everywhere

    path, _ = _ortho(tmp_path, paint)
    cells, result = colour.judge(path)
    assert set(cells.flag) == {"HEALTHY"}
    assert "no crop up yet" in result.notes[0]


def test_only_the_field_is_judged_and_black_is_no_picture(tmp_path) -> None:
    def paint(rgb, leaf):
        rgb[:, :, :100] = 0                                              # 5 m no photo reached

    path, _ = _ortho(tmp_path, paint)
    to_wgs = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    west, south = to_wgs.transform(WEST + 10, NORTH - 30)
    east, north = to_wgs.transform(WEST + 30, NORTH - 10)
    cells, result = colour.judge(path, box(west, south, east, north))
    assert result.n_judged == pytest.approx(400, abs=45)                 # 20 x 20 squares of 1 m
    whole, _ = colour.judge(path)
    no_picture = box(WEST, NORTH - 40, WEST + 4.9, NORTH)
    judged = whole[whole.flag != "NO_DATA"]
    assert not judged.intersects(no_picture).any()
