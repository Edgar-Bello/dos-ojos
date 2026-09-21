"""The trained tree model: the network, the rows and gaps, and the shipped model end to end."""

from __future__ import annotations

import json

import numpy as np
import pytest
from affine import Affine

from dosojos_drone import treeml


def test_the_network_learns_a_curve_and_a_class() -> None:
    rng = np.random.default_rng(0)
    X = rng.uniform(-2, 2, (3000, 2)).astype(np.float32)
    y = np.sin(X[:, 0]) + 0.5 * X[:, 1]
    net = treeml.Net([2, 16, 1], seed=1).fit(X, y, epochs=80, batch=128, lr=5e-3)
    assert np.mean(np.abs(net.predict(X) - y)) < 0.1
    label = (X[:, 0] * X[:, 1] > 0).astype(np.float32)
    clf = treeml.Net([2, 16, 1], out="sigmoid", seed=1).fit(X, label, epochs=80, batch=128,
                                                            lr=5e-3)
    assert np.mean((clf.predict(X) > 0.5) == label) > 0.9


def test_a_model_survives_a_save_and_a_load(tmp_path) -> None:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, 4)).astype(np.float32)
    nets = [treeml.Net([4, 8, 1], seed=s).fit(X, X[:, 0], epochs=5) for s in range(2)]
    model = treeml.TreeModel(nets[0], nets, nets, 0.4, 0.6, {"scores": {"x": 1}})
    path = model.save(tmp_path / "m.npz")
    back = treeml.TreeModel.load(path)
    assert back.threshold == 0.4 and back.poor_at == 0.6 and back.card == {"scores": {"x": 1}}
    assert len(back.height) == 2
    assert np.allclose(back.finder.predict(X), model.finder.predict(X))


def test_a_missing_model_says_how_to_make_one(tmp_path) -> None:
    with pytest.raises(treeml.TreeModelError, match="train_tree_model.py"):
        treeml.TreeModel.load(tmp_path / "none.npz")


def _grove(missing=(), rows=4, per_row=30, spacing=2.1, gap=7.7, tilt_deg=20.0):
    a = np.radians(tilt_deg)
    points, holes = [], []
    for r in range(rows):
        for i in range(per_row):
            along, across = i * spacing, r * gap
            xy = (across * np.cos(a) - along * np.sin(a), across * np.sin(a) + along * np.cos(a))
            (holes if (r, i) in missing else points).append(xy)
    return np.array(points) + 500_000, np.array(holes) + 500_000


def test_rows_and_gaps_in_a_tilted_grove() -> None:
    points, holes = _grove(missing={(1, 10), (2, 5), (2, 6)})
    rows, gaps, spacing, where = treeml.row_gaps(points)
    assert rows == 4 and gaps == 3 and spacing == pytest.approx(2.1, abs=0.01)
    for hole in holes:
        assert np.min(np.hypot(*(where - hole).T)) < 0.2


def test_a_long_empty_stretch_is_a_rows_end_not_dead_trees() -> None:
    points, _ = _grove(missing={(0, i) for i in range(8, 20)})
    assert treeml.row_gaps(points)[1] == 0


def test_each_tree_is_held_against_its_own_row() -> None:
    features = np.array([[1.0], [1.0], [0.5], [4.0], [4.0], [2.0]])
    both = treeml.relative(features, np.array([0, 0, 0, 1, 1, 1]))
    assert both[:, 1] == pytest.approx([1.0, 1.0, 0.5, 1.0, 1.0, 0.5], rel=1e-5)


def test_the_shipped_model_finds_the_trees_of_a_drawn_grove(tmp_path) -> None:
    """Round green crowns on bare soil, drawn at 10 cm: the model trained on real
    citrus should find nearly all of them and nothing between them."""
    model = treeml.TreeModel.load()
    h, w = 700, 400
    chm = np.zeros((h, w), np.float32)
    rgb = np.zeros((3, h, w), np.float32)
    rgb[:] = np.array([150, 130, 110])[:, None, None]        # sandy soil
    yy, xx = np.mgrid[0:h, 0:w]
    centres = [(60 + 21 * i, 60 + 77 * r) for r in range(4) for i in range(28)]
    for cy, cx in centres:
        d = np.hypot(yy - cy, xx - cx) * 0.1
        crown = np.clip(1.9 * (1 - (d / 1.0) ** 2), 0, None)
        chm = np.maximum(chm, crown)
        leaf = crown > 0
        rgb[:, leaf] = np.array([60, 110, 50])[:, None]
    grids = treeml.Grids(chm, rgb, Affine(0.1, 0, 500_000, 0, -0.1, 3_000_000), "EPSG:32617",
                         np.ones((h, w), bool))
    found = treeml.find_trees(model, grids)
    truth = np.array([grids.xy(cy, cx) for cy, cx in centres])
    near = np.min(np.hypot(found.xy[:, None, 0] - truth[None, :, 0],
                           found.xy[:, None, 1] - truth[None, :, 1]), axis=0)
    assert np.mean(near < 0.7) > 0.9                 # found
    assert len(found.xy) <= len(centres) * 1.1       # and not doubled
    summary = treeml.write(found, grids, tmp_path, model)
    assert (tmp_path / "trees_ai.png").exists()
    saved = json.loads((tmp_path / "trees_ai.json").read_text())
    assert saved["trees"] == summary["trees"] and saved["model"]["scores"]
