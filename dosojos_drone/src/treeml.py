"""A trained model that finds orchard trees and judges each one, from a drone flight.

The watershed in ``crowns`` cuts one crown where two trees touch, so in a young
grove it finds about two trees in three. This module learns what a tree looks
like instead, from 206 citrus trees the USDA measured by hand (Fort Pierce FL,
2021, public domain): where each one stands, how tall it is and how healthy.

Three small neural networks, trained in plain numpy (no outside libraries, no
downloads) by ``public_demo/usda-citrus-bingo-2021/train_tree_model.py``:

- **finder**: for every 10 cm of the field, how close it is to the middle of a
  tree, from the canopy height and the colour around it at several sizes. Its
  peaks are the trees.
- **height**: a tree's real height from what the canopy model and the photo show
  around it. The canopy model reads citrus short (fine leaves against the sky
  reconstruct poorly); the net learned by how much.
- **health**: the chance a living tree is in poor shape (the USDA's 1-2 of 5),
  against the other trees of the same flight. A dead tree has no crown to find;
  it shows up as a gap in its row instead.

What they scored on rows they were not trained on is kept in the model file and
shown with every result. They have only ever seen one grove: another orchard is a
reasonable guess, not a measured one, and every figure says so.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).with_name("models") / "citrus_trees.npz"
#: The grid the finder works on, metres.
CELL_M = 0.10
#: Two peaks closer than this are one tree (the grove's trees stand 2.1 m apart).
MIN_APART_M = 1.3
#: Canopy this tall counts as tree when a tree's crown is measured, metres.
CANOPY_M = 0.5
#: Trees this close to where the 3D model ends are counted but not judged.
EDGE_M = 3.0
#: Longer runs with no tree are a row's end or a road, not trees that died.
MAX_GAP_TREES = 3
#: Radii (in cells) a tree is described over.
TREE_RADII = (6, 10)


class TreeModelError(RuntimeError):
    """The model file or the flight's rasters are missing or unusable."""


# --------------------------------------------------------------------------- #
# The network
# --------------------------------------------------------------------------- #


class Net:
    """Dense layers with ReLU, trained with Adam and stopped early on held-back data."""

    def __init__(self, sizes, *, out: str = "linear", l2: float = 1e-4, seed: int = 0):
        rng = np.random.default_rng(seed)
        self.W = [rng.normal(0, np.sqrt(2 / a), (a, b)).astype(np.float32)
                  for a, b in zip(sizes[:-1], sizes[1:])]
        self.b = [np.zeros(b, np.float32) for b in sizes[1:]]
        self.out, self.l2 = out, l2
        self.mu = np.zeros(sizes[0], np.float32)
        self.sd = np.ones(sizes[0], np.float32)

    def _forward(self, X):
        acts = [X]
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            z = acts[-1] @ W + b
            if i < len(self.W) - 1:
                z = np.maximum(z, 0)
            acts.append(z)
        return acts

    def predict(self, X) -> np.ndarray:
        X = ((np.asarray(X, np.float32) - self.mu) / self.sd).astype(np.float32)
        z = self._forward(X)[-1][:, 0]
        return 1 / (1 + np.exp(-z)) if self.out == "sigmoid" else z

    def fit(self, X, y, *, epochs=60, batch=512, lr=2e-3, weight=None, valid=0.15,
            seed=0, patience=8) -> "Net":
        rng = np.random.default_rng(seed)
        X = np.asarray(X, np.float32)
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-6
        X = ((X - self.mu) / self.sd).astype(np.float32)
        y = np.asarray(y, np.float32)
        w = np.ones(len(y), np.float32) if weight is None else np.asarray(weight, np.float32)
        order = rng.permutation(len(y))
        cut = int(len(y) * valid)
        held, train = order[:cut], order[cut:]
        params = self.W + self.b
        m1 = [np.zeros_like(p) for p in params]
        m2 = [np.zeros_like(p) for p in params]
        best, kept, step, waited = np.inf, None, 0, 0
        for _ in range(epochs):
            rng.shuffle(train)
            for start in range(0, len(train), batch):
                idx = train[start:start + batch]
                acts = self._forward(X[idx])
                z = acts[-1][:, 0]
                g = ((1 / (1 + np.exp(-z)) - y[idx]) if self.out == "sigmoid"
                     else 2 * (z - y[idx])) * w[idx]
                g = (g / len(idx))[:, None]
                grads_w, grads_b = [], []
                for i in range(len(self.W) - 1, -1, -1):
                    grads_w.insert(0, acts[i].T @ g + self.l2 * self.W[i])
                    grads_b.insert(0, g.sum(0))
                    if i:
                        g = (g @ self.W[i].T) * (acts[i] > 0)
                step += 1
                for j, (p, gr) in enumerate(zip(params, grads_w + grads_b)):
                    m1[j] = 0.9 * m1[j] + 0.1 * gr
                    m2[j] = 0.999 * m2[j] + 0.001 * gr * gr
                    p -= lr * (m1[j] / (1 - 0.9 ** step)) / (
                        np.sqrt(m2[j] / (1 - 0.999 ** step)) + 1e-8)
            if not cut:
                continue
            z = self._forward(X[held])[-1][:, 0]
            if self.out == "sigmoid":
                p = np.clip(1 / (1 + np.exp(-z)), 1e-6, 1 - 1e-6)
                loss = -np.mean(w[held] * (y[held] * np.log(p) + (1 - y[held]) * np.log(1 - p)))
            else:
                loss = float(np.mean(w[held] * (z - y[held]) ** 2))
            if loss < best - 1e-6:
                best, waited, kept = loss, 0, [p.copy() for p in params]
            else:
                waited += 1
                if waited >= patience:
                    break
        if kept is not None:
            n = len(self.W)
            self.W, self.b = kept[:n], kept[n:]
        return self

    def state(self, prefix: str) -> dict[str, np.ndarray]:
        out = {f"{prefix}.mu": self.mu, f"{prefix}.sd": self.sd,
               f"{prefix}.out": np.array(self.out)}
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            out[f"{prefix}.W{i}"], out[f"{prefix}.b{i}"] = W, b
        return out

    @classmethod
    def from_state(cls, saved, prefix: str) -> "Net":
        count = sum(1 for k in saved.files if k.startswith(prefix + ".W"))
        net = cls.__new__(cls)
        net.W = [saved[f"{prefix}.W{i}"] for i in range(count)]
        net.b = [saved[f"{prefix}.b{i}"] for i in range(count)]
        net.mu, net.sd = saved[f"{prefix}.mu"], saved[f"{prefix}.sd"]
        net.out, net.l2 = str(saved[f"{prefix}.out"]), 0.0
        return net


def mean_of(nets: list[Net], X) -> np.ndarray:
    return np.mean([n.predict(X) for n in nets], axis=0)


# --------------------------------------------------------------------------- #
# What the networks see
# --------------------------------------------------------------------------- #


@dataclass
class Grids:
    """The canopy model and the photo on one 10 cm grid."""

    chm: np.ndarray            # metres over the ground
    rgb: np.ndarray            # 3 x H x W, 0-255
    transform: object
    crs: object
    valid: np.ndarray | None = None   # where the flight actually saw the ground

    def xy(self, rows, cols):
        return self.transform * (np.asarray(cols) + 0.5, np.asarray(rows) + 0.5)

    def rowcol(self, x, y):
        col, row = ~self.transform * (x, y)
        return row, col


def load_grids(chm_path: Path, ortho_path: Path) -> Grids:
    """The canopy model (any cell size) and the orthophoto, resampled to 10 cm."""
    import rasterio
    from rasterio.warp import Resampling, reproject

    for path in (chm_path, ortho_path):
        if not Path(path).exists():
            raise TreeModelError(f"{Path(path).name} is missing: {path}")
    with rasterio.open(chm_path) as src:
        step = max(1, int(round(CELL_M / abs(src.transform.a))))
        raw = src.read(1, masked=True)
        chm = raw.filled(0).astype(np.float32)
        seen = ~np.ma.getmaskarray(raw) & np.isfinite(raw.filled(np.nan))
        h, w = chm.shape[0] // step, chm.shape[1] // step
        chm = chm[:h * step, :w * step].reshape(h, step, w, step).max(axis=(1, 3))
        seen = seen[:h * step, :w * step].reshape(h, step, w, step).all(axis=(1, 3))
        transform = src.transform * src.transform.scale(step, step)
        crs = src.crs
    chm = np.clip(np.nan_to_num(chm), 0, 30)
    rgb = np.zeros((3, h, w), np.float32)
    with rasterio.open(ortho_path) as src:
        for band in range(3):
            reproject(src.read(band + 1).astype(np.float32), rgb[band],
                      src_transform=src.transform, src_crs=src.crs, dst_transform=transform,
                      dst_crs=crs, resampling=Resampling.average)
    from scipy import ndimage as ndi

    # A few metres in from where the model ends: crowns there are half built.
    valid = ndi.binary_erosion(seen & (rgb.sum(0) > 0), iterations=int(EDGE_M / CELL_M))
    return Grids(chm, rgb, transform, crs, valid)


def _chromaticity(rgb):
    total = rgb.sum(0) + 1e-3
    r, g, b = rgb / total
    return r, g, b, total


def pixel_features(grids: Grids) -> np.ndarray:
    """H x W x 22: canopy height and greenness, smoothed and peaked at several sizes."""
    from scipy import ndimage as ndi

    chm = grids.chm
    out = [chm]
    for s in (1, 2, 4, 8):
        smooth = ndi.gaussian_filter(chm, s)
        out += [smooth, smooth - ndi.maximum_filter(smooth, size=2 * s + 1)]
    for s in (4, 6, 8, 10):
        out.append(-s * s * ndi.gaussian_laplace(chm, s))     # blobs a tree's size
    canopy = (chm > CANOPY_M).astype(np.float32)
    for size in (5, 11, 21):
        out.append(ndi.uniform_filter(canopy, size))
    r, g, b, total = _chromaticity(grids.rgb)
    exg = 2 * g - r - b
    for s in (2, 4, 8):
        out.append(ndi.gaussian_filter(exg, s))
    out.append(ndi.gaussian_filter(total / 765, 4))
    return np.stack(out, -1).astype(np.float32)


def tree_features(grids: Grids, x: float, y: float) -> list[float]:
    """Eighteen numbers about the tree standing at (x, y)."""
    row, col = (int(v) for v in grids.rowcol(x, y))
    r, g, b, total = _chromaticity(grids.rgb)
    exg = 2 * g - r - b
    out: list[float] = []
    for radius in TREE_RADII:
        rows = slice(max(row - radius, 0), row + radius + 1)
        cols = slice(max(col - radius, 0), col + radius + 1)
        yy, xx = np.mgrid[rows, cols]
        disk = (yy - row) ** 2 + (xx - col) ** 2 <= radius * radius
        height = grids.chm[rows, cols][disk]
        if height.size == 0:
            out += [0.0] * 9
            continue
        canopy = height > CANOPY_M
        some = bool(canopy.any())
        out += [float(height.max()), float(np.percentile(height, 90)), float(height.mean()),
                float(canopy.mean()), float(height[canopy].sum() * CELL_M * CELL_M),
                float(exg[rows, cols][disk][canopy].mean()) if some else 0.0,
                float(exg[rows, cols][disk].mean()),
                float(total[rows, cols][disk][canopy].mean() / 765) if some else 0.0,
                float(r[rows, cols][disk][canopy].mean()) if some else 0.0]
    return out


def relative(features: np.ndarray, groups: np.ndarray | None = None) -> np.ndarray:
    """Each tree against the typical tree of its own row, beside the raw numbers.

    A flight built at lower quality reads every tree short by the same share, and
    a weak tree is weak against its neighbours; the ratio carries both. It is the
    row, not the flight, because one flight can take in an older planting next door.
    Trees in no row (``groups`` -1, or none given) are held against all of them.
    """
    groups = np.full(len(features), -1) if groups is None else np.asarray(groups)
    base = np.repeat(np.median(features, axis=0)[None], len(features), axis=0)
    for g in np.unique(groups[groups >= 0]):
        base[groups == g] = np.median(features[groups == g], axis=0)
    return np.c_[features, features / (np.abs(base) + 1e-6)]


# --------------------------------------------------------------------------- #
# Using the trained model
# --------------------------------------------------------------------------- #


@dataclass
class TreeModel:
    finder: Net
    height: list[Net]
    health: list[Net]
    threshold: float
    poor_at: float
    card: dict = field(default_factory=dict)

    def save(self, path: Path = MODEL_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {"threshold": np.array(self.threshold), "poor_at": np.array(self.poor_at),
                 "card": np.array(json.dumps(self.card))}
        state.update(self.finder.state("finder"))
        for i, net in enumerate(self.height):
            state.update(net.state(f"height{i}"))
        for i, net in enumerate(self.health):
            state.update(net.state(f"health{i}"))
        np.savez_compressed(path, **state)
        return path

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "TreeModel":
        if not path.exists():
            raise TreeModelError(
                f"no trained tree model at {path}; run public_demo/usda-citrus-bingo-2021/"
                "train_tree_model.py")
        saved = np.load(path, allow_pickle=False)
        count = lambda name: len({k.split(".")[0] for k in saved.files if k.startswith(name)})
        return cls(finder=Net.from_state(saved, "finder"),
                   height=[Net.from_state(saved, f"height{i}") for i in range(count("height"))],
                   health=[Net.from_state(saved, f"health{i}") for i in range(count("health"))],
                   threshold=float(saved["threshold"]), poor_at=float(saved["poor_at"]),
                   card=json.loads(str(saved["card"])))


def heat(model: TreeModel, grids: Grids) -> np.ndarray:
    """How close each 10 cm is to the middle of a tree, 0 to 1."""
    from scipy import ndimage as ndi

    features = pixel_features(grids)
    flat = features.reshape(-1, features.shape[-1])
    out = np.empty(len(flat), np.float32)
    for start in range(0, len(flat), 200_000):
        out[start:start + 200_000] = model.finder.predict(flat[start:start + 200_000])
    return ndi.gaussian_filter(out.reshape(grids.chm.shape), 1)


def peaks(heatmap: np.ndarray, grids: Grids, threshold: float) -> np.ndarray:
    """The trees: local tops of the heat, over canopy, as (x, y) in the map's metres."""
    from scipy import ndimage as ndi

    size = int(round(MIN_APART_M / CELL_M)) | 1
    top = (heatmap == ndi.maximum_filter(heatmap, size=size)) & (heatmap > threshold)
    rows, cols = np.nonzero(top)
    x, y = grids.xy(rows, cols)
    return np.c_[x, y]


@dataclass
class Found:
    """One flight's trees, as the model sees them."""

    xy: np.ndarray                 # n x 2, map metres
    height_m: np.ndarray           # learned height
    canopy_m: np.ndarray           # what the canopy model alone reads
    poor: np.ndarray               # chance each tree is poor or dead, 0 to 1
    poor_at: float
    judged: np.ndarray | None = None   # False: at the model's edge or in no row
    in_row: np.ndarray | None = None   # False: a plant standing outside the rows
    rows: int = 0
    gaps: int = 0
    spacing_m: float | None = None
    gap_xy: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))

    @property
    def flagged(self) -> np.ndarray:
        judged = np.ones(len(self.poor), bool) if self.judged is None else self.judged
        return (self.poor >= self.poor_at) & judged


def find_trees(model: TreeModel, grids: Grids) -> Found:
    points = peaks(heat(model, grids), grids, model.threshold)
    if len(points) == 0:
        return Found(points, np.zeros(0), np.zeros(0), np.zeros(0), model.poor_at)
    features = np.array([tree_features(grids, x, y) for x, y in points], np.float32)
    labels = row_labels(points)[0]
    both = relative(features, labels)
    judged = labels >= 0
    if grids.valid is not None:
        rows, cols = grids.rowcol(points[:, 0], points[:, 1])
        rows = np.clip(rows.astype(int), 0, grids.chm.shape[0] - 1)
        cols = np.clip(cols.astype(int), 0, grids.chm.shape[1] - 1)
        judged &= grids.valid[rows, cols]
    found = Found(points, mean_of(model.height, both), features[:, 0],
                  mean_of(model.health, both), model.poor_at, judged, labels >= 0)
    found.rows, found.gaps, found.spacing_m, found.gap_xy = row_gaps(points)
    return found


def row_labels(points: np.ndarray, *, min_row: int = 10):
    """Which row each tree stands in (-1: none), and the rows' direction.

    The rows' direction is the one that stacks the trees into the fewest lines.
    Returns labels, the tilt in degrees, and each tree's (across, along) position.
    """
    labels = np.full(len(points), -1)
    if len(points) < min_row:
        return labels, 0.0, np.zeros(len(points)), np.zeros(len(points))
    x, y = points[:, 0] - points[:, 0].mean(), points[:, 1] - points[:, 1].mean()

    def turn(deg):
        a = np.radians(deg)
        return x * np.cos(a) + y * np.sin(a), -x * np.sin(a) + y * np.cos(a)

    tilt = max(np.arange(-90, 90, 0.25),
               key=lambda d: (np.histogram(turn(d)[0], bins=max(20, int(np.ptp(x) + np.ptp(y))))[0]
                              ** 2).sum())
    across, along = turn(tilt)
    order = np.argsort(across)
    groups, current = [], [order[0]]
    for left, right in zip(order[:-1], order[1:]):
        if across[right] - across[left] > 1.5:
            groups.append(current)
            current = []
        current.append(right)
    groups.append(current)
    for number, members in enumerate(g for g in groups if len(g) >= min_row):
        labels[members] = number
    return labels, float(tilt), across, along


def row_gaps(points: np.ndarray, *, min_row: int = 10):
    """Rows, and the places in them where a tree should stand and none was found.

    The spacing is the usual distance to the next tree in the same row.
    """
    labels, tilt, across, along = row_labels(points, min_row=min_row)
    rows = [np.flatnonzero(labels == g) for g in range(labels.max() + 1)]
    steps = np.concatenate([np.diff(np.sort(along[r])) for r in rows]) if rows else np.zeros(0)
    if steps.size == 0:
        return len(rows), 0, None, np.zeros((0, 2))
    spacing = float(np.median(steps))
    gaps, where = 0, []
    a = np.radians(tilt)
    for r in rows:
        line = float(np.median(across[r]))
        stops = np.sort(along[r])
        for left, right in zip(stops[:-1], stops[1:]):
            missing = int(round((right - left) / spacing)) - 1
            if missing > MAX_GAP_TREES:
                continue        # the end of a short row, or a road: not trees gone
            for k in range(1, missing + 1):
                at = left + k * (right - left) / (missing + 1)
                where.append((line * np.cos(a) - at * np.sin(a) + points[:, 0].mean(),
                              line * np.sin(a) + at * np.cos(a) + points[:, 1].mean()))
            gaps += max(missing, 0)
    return len(rows), gaps, spacing, np.array(where).reshape(-1, 2)


# --------------------------------------------------------------------------- #
# Writing it out
# --------------------------------------------------------------------------- #


def write(found: Found, grids: Grids, out: Path, model: TreeModel,
          banner: str | None = None) -> dict:
    """trees_ai.geojson, trees_ai.json and trees_ai.png in the flight's out folder."""
    import geopandas as gpd
    from shapely.geometry import Point

    out.mkdir(parents=True, exist_ok=True)
    frame = gpd.GeoDataFrame({
        "tree": np.arange(1, len(found.xy) + 1),
        "height_m": np.round(found.height_m, 2),
        "canopy_model_m": np.round(found.canopy_m, 2),
        "chance_poor": np.round(found.poor, 2),
        "judged": found.judged if found.judged is not None else np.ones(len(found.xy), bool),
        "needs_a_look": found.flagged,
    }, geometry=[Point(x, y) for x, y in found.xy], crs=grids.crs)
    frame.to_file(out / "trees_ai.geojson", driver="GeoJSON")
    summary = {
        "method": "trained neural networks (numpy), Dos Ojos treeml",
        "trees": int(len(found.xy) if found.in_row is None else found.in_row.sum()),
        "outside_rows": int(0 if found.in_row is None else (~found.in_row).sum()),
        "needs_a_look": int(found.flagged.sum()),
        "not_judged": int(0 if found.judged is None else (~found.judged).sum()),
        "gaps": int(found.gaps),
        "rows": int(found.rows),
        "spacing_m": None if found.spacing_m is None else round(found.spacing_m, 2),
        "height_m": {"median": _round(np.median(found.height_m)),
                     "tallest": _round(np.max(found.height_m)),
                     "shortest": _round(np.min(found.height_m))} if len(found.xy) else None,
        "canopy_model_median_m": _round(np.median(found.canopy_m)) if len(found.xy) else None,
        "worst": [{"tree": int(i + 1), "chance_poor": round(float(found.poor[i]), 2),
                   "height_m": round(float(found.height_m[i]), 2)}
                  for i in np.argsort(-np.where(found.flagged, found.poor, -1))[:5]
                  if found.flagged[i]],
        "model": model.card,
    }
    (out / "trees_ai.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    picture(found, grids, out / "trees_ai.png", banner)
    return summary


def _round(value) -> float:
    return round(float(value), 2)


def picture(found: Found, grids: Grids, path: Path, banner: str | None = None) -> None:
    """The photo with each tree ringed: green fine, orange needs a look, grey X a gap."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rgb = np.clip(np.moveaxis(grids.rgb, 0, -1) / 255, 0, 1)
    h, w = grids.chm.shape
    figure, axes = plt.subplots(figsize=(7, 7 * h / max(w, 1)) if h < w else (7 * w / max(h, 1) + 1.5, 7),
                                dpi=150)
    axes.imshow(rgb)
    if len(found.xy):
        rows, cols = grids.rowcol(found.xy[:, 0], found.xy[:, 1])
        ok = ~found.flagged
        axes.scatter(cols[ok], rows[ok], s=22, facecolors="none", edgecolors="#2ecc40",
                     linewidths=1.0, label=f"tree ({ok.sum()})")
        axes.scatter(cols[~ok], rows[~ok], s=42, facecolors="none", edgecolors="#ff851b",
                     linewidths=1.8, label=f"needs a look ({(~ok).sum()})")
    if len(found.gap_xy):
        rows, cols = grids.rowcol(found.gap_xy[:, 0], found.gap_xy[:, 1])
        axes.scatter(cols, rows, s=26, marker="x", color="#e8e8e8", linewidths=1.6,
                     path_effects=[__import__("matplotlib.patheffects", fromlist=["x"]).withStroke(
                         linewidth=2.6, foreground="#333333")],
                     label=f"gap in the row ({len(found.gap_xy)})")
    axes.set_axis_off()
    axes.legend(loc="lower right", fontsize=7, framealpha=0.85)
    axes.set_title("Trees found by the trained model", fontsize=10)
    if banner:
        axes.annotate(banner, xy=(0, 1), xycoords="axes fraction", xytext=(0, 18),
                      textcoords="offset points", fontsize=7, color="white", fontweight="bold",
                      bbox={"boxstyle": "square,pad=0.4", "facecolor": "#b3261e",
                            "edgecolor": "none"})
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)
