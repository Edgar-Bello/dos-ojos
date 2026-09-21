"""The 3D model: a known cloud in, the same shape out, small enough for a page."""

from __future__ import annotations

import json

import laspy
import numpy as np
import pytest

from dosojos_drone import model3d


def a_field(path, n_ground: int = 60_000, trees: int = 20):
    """A flat 100 x 40 m field with rows of 2 m trees, as a coloured LAS file."""
    rng = np.random.default_rng(1)
    ground = np.column_stack([rng.uniform(0, 100, n_ground), rng.uniform(0, 40, n_ground),
                              rng.normal(10.0, 0.02, n_ground)])
    crowns = []
    for i in range(trees):
        middle = np.array([5 + i * 5, 20, 10.0])
        spread = rng.normal(0, 0.6, (2_000, 3)) * [1, 1, 0.5] + middle + [0, 0, 1.5]
        crowns.append(spread)
    xyz = np.vstack([ground, *crowns])
    green = np.vstack([np.tile([120, 100, 80], (n_ground, 1)),
                       np.tile([40, 140, 50], (sum(len(c) for c in crowns), 1))])
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.offsets, header.scales = [0, 0, 0], [0.001, 0.001, 0.001]
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.red, las.green, las.blue = (green[:, k] * 256 for k in range(3))   # 16-bit colour
    las.write(path)
    return xyz


def test_thinning_keeps_about_the_target_and_every_tree(tmp_path) -> None:
    xyz = a_field(tmp_path / "field.las")
    kept, cube = model3d.thin(xyz, 10_000)
    assert 8_000 <= len(kept) <= 10_500 and cube > 0
    tall = xyz[kept][:, 2] > 11.0
    trees_seen = {int(round((x - 5) / 5)) for x in xyz[kept][tall][:, 0]}
    assert trees_seen >= set(range(20))


def test_build_writes_a_model_a_page_can_read_back(tmp_path) -> None:
    a_field(tmp_path / "field.las")
    model = model3d.build(tmp_path / "field.las", tmp_path / "out", target=5_000)
    meta = json.loads((tmp_path / "out" / "model3d.json").read_text("utf-8"))
    data = (tmp_path / "out" / "model3d.bin").read_bytes()
    n = meta["points"]
    assert n == model.points and len(data) == n * 10
    ints = np.frombuffer(data[:n * 6], "<i2").reshape(n, 3)
    rgb = np.frombuffer(data[n * 6:n * 9], np.uint8).reshape(n, 3)
    xyz = ints * meta["scale_m"] + meta["middle"]
    assert xyz[:, 0].min() == pytest.approx(0, abs=0.5) and 99.5 < xyz[:, 0].max() < 103   # a crown overhangs
    assert rgb.max() <= 255 and (rgb == [40, 140, 50]).all(axis=1).any()   # 16-bit colour read right
    assert 99 < meta["size_m"][0] < 103
    assert (tmp_path / "out" / "model3d.png").stat().st_size > 10_000
    over = np.frombuffer(data[n * 9:], np.uint8) / 10
    assert over[xyz[:, 2] < 10.1].max() < 0.3                  # the ground sits at 0
    assert 1.5 < meta["plants_top_m"] < 3.0                     # the 2 m trees


def test_height_over_the_ground_follows_a_sloping_field() -> None:
    rng = np.random.default_rng(2)
    x, y = rng.uniform(0, 100, 50_000), rng.uniform(0, 40, 50_000)
    ground = np.column_stack([x, y, 5 + 0.05 * x])            # rises 5 m across the field
    bush = np.column_stack([rng.normal(80, 0.5, 500), rng.normal(20, 0.5, 500),
                            5 + 0.05 * 80 + 1.2 + rng.normal(0, 0.1, 500)])
    heights = model3d.over_ground(np.vstack([ground, bush]))
    assert heights[:50_000].max() < 0.6                         # slope is not height
    assert np.median(heights[50_000:]) == pytest.approx(1.2, abs=0.35)


def test_a_missing_cloud_says_what_to_run(tmp_path) -> None:
    with pytest.raises(model3d.ModelError, match="odm"):
        model3d.build(tmp_path / "nothing.laz", tmp_path / "out")
