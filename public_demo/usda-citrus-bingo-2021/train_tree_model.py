"""Train the tree model on the USDA's hand-measured citrus, and say how good it is.

FREE PUBLIC DATA, NOT OURS: 206 'Bingo' mandarin trees, Fort Pierce FL, measured by
the USDA-ARS team in April 2021 (doi:10.15482/USDA.ADC/26946823, public domain).

Two builds of the same 46 photos are learned from: the high-quality one run_demo.sh
makes, and the medium-quality one the Edgar Field demo made, so the model sees both
kinds of canopy model a farm's flight can come back with.

**Scores are from rows the model never saw.** Each of the four rows is held out in
turn, the networks are trained on the other three (both builds), and the held-out
row is scored against the tape measure: first finding the trees, then each found
tree's height and health. Only then is the final model trained on all four rows and
written to dosojos_drone/src/models/citrus_trees.npz, with those scores inside it.

Where each tree stands comes from the planting lattice registered on the flight's
own watershed crowns (as check_answer_key.py does); the key says which spot holds
which tree. Run from any folder, after run_demo.sh:

    dosojos_drone/.venv/Scripts/python.exe public_demo/usda-citrus-bingo-2021/train_tree_model.py
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_answer_key as key  # noqa: E402

from dosojos_drone import treeml  # noqa: E402

WORKSPACE = HERE / "dosojos_drone"
BUILDS = {"high": "PUBLIC-usda-bingo-20210512", "medium": "PUBLIC-usda-bingo-20210512-medium"}
#: The finder learns "how close to the middle of a tree", as a bump this wide.
BUMP_M = 0.35
#: A found tree within this of a key tree is that tree.
MATCH_M = 0.7
SEEDS = 5
THRESHOLD = 0.4
POOR_AT = 0.6


def lattice(out: Path) -> dict:
    crowns = gpd.read_file(out / "metrics_watershed.geojson")
    c = crowns.geometry.centroid
    tilt = max(np.arange(-10, 10, 0.05),
               key=lambda d: (np.histogram(key.rotate(c.x.values, c.y.values, d)[0],
                                           bins=400)[0] ** 2).sum())
    across, along = key.rotate(c.x.values, c.y.values, tilt)
    bands = key.find_rows(across, along)
    band_of = {row: bands[-row] for row in (1, 2, 3, 4)}
    seen = np.concatenate([along[band_of[r]] for r in key.FULL_ROWS])
    start = float(np.median([along[band_of[r]].min() for r in key.FULL_ROWS]))
    spacing = (float(np.median([along[band_of[r]].max() for r in key.FULL_ROWS])) - start) / 62
    first, spacing, _ = key.register(seen, start, spacing)
    lines = {r: float(across[band_of[r]].mean()) for r in (1, 2, 3, 4)}
    a = np.radians(tilt)
    trees = []
    for t in key.answer_key():
        line, place = lines[t["row"]], first + (t["pos"] - 1) * spacing
        trees.append({**t, "x": line * np.cos(a) - place * np.sin(a),
                      "y": line * np.sin(a) + place * np.cos(a)})
    return {"tilt": tilt, "spacing": spacing, "first": first, "lines": lines, "trees": trees}


def prepare(name: str, flight: str) -> dict:
    grids = treeml.load_grids(WORKSPACE / "out" / flight / "chm.tif",
                              WORKSPACE / "data" / "odm" / flight / "odm_orthophoto"
                              / "odm_orthophoto.tif")
    lat = lattice(WORKSPACE / "out" / flight)
    h, w = grids.chm.shape
    rr, cc = np.mgrid[0:h, 0:w]
    xs, ys = grids.xy(rr, cc)
    a = np.radians(lat["tilt"])
    across = xs * np.cos(a) + ys * np.sin(a)
    along = -xs * np.sin(a) + ys * np.cos(a)
    gap = abs(lat["lines"][1] - lat["lines"][2])
    row_of = np.zeros((h, w), np.int8)
    for r, line in lat["lines"].items():
        row_of[np.abs(across - line) < gap / 2] = r
    row_of[(along < lat["first"] - lat["spacing"])
           | (along > lat["first"] + 63 * lat["spacing"])] = 0
    live = [t for t in lat["trees"] if t["health"] not in (0, None)]
    target = np.zeros((h, w), np.float32)
    reach = int(3 * BUMP_M / treeml.CELL_M)
    for t in live:
        pr, pc = grids.rowcol(t["x"], t["y"])
        r0, c0 = int(pr) - reach, int(pc) - reach
        yy, xx = np.mgrid[r0:r0 + 2 * reach + 1, c0:c0 + 2 * reach + 1]
        d2 = ((yy + 0.5 - pr) ** 2 + (xx + 0.5 - pc) ** 2) * treeml.CELL_M ** 2
        window = target[r0:r0 + 2 * reach + 1, c0:c0 + 2 * reach + 1]
        np.maximum(window, np.exp(-d2 / (2 * BUMP_M ** 2)), out=window)
    trees = [t for t in lat["trees"] if t["health"] is not None]
    features = np.array([treeml.tree_features(grids, t["x"], t["y"]) for t in trees], np.float32)
    print(f"  {name}: {h} x {w} cells of 10 cm, {len(live)} living trees in the key", flush=True)
    return {"grids": grids, "pixels": treeml.pixel_features(grids), "row_of": row_of,
            "target": target, "trees": trees, "features": features, "live": live}


def finder_data(builds: dict, rows: tuple[int, ...], rng) -> tuple[np.ndarray, np.ndarray]:
    """Every cell near a tree, and three times as many others, from these rows."""
    xs, ys = [], []
    for b in builds.values():
        cells = np.flatnonzero(np.isin(b["row_of"].ravel(), rows))
        near = cells[b["target"].ravel()[cells] > 0.05]
        far = np.setdiff1d(cells, near)
        far = rng.choice(far, size=min(len(far), 3 * len(near)), replace=False)
        take = np.r_[near, far]
        xs.append(b["pixels"].reshape(-1, b["pixels"].shape[-1])[take])
        ys.append(b["target"].ravel()[take])
    return np.concatenate(xs), np.concatenate(ys)


def train_finder(builds, rows, seed) -> treeml.Net:
    X, y = finder_data(builds, rows, np.random.default_rng(seed))
    return treeml.Net([X.shape[1], 32, 16, 1], seed=seed).fit(X, y, epochs=40, seed=seed)


def tree_table(builds: dict):
    """Per key tree: build, row, health, height, features against the build's own trees."""
    parts = []
    for name, b in builds.items():
        both = treeml.relative(b["features"], np.array([t["row"] for t in b["trees"]]))
        for t, f in zip(b["trees"], both):
            parts.append((name, t["row"], t["health"], t["height_m"] or np.nan, f))
    return (np.array([p[0] for p in parts]), np.array([p[1] for p in parts]),
            np.array([p[2] for p in parts]), np.array([p[3] for p in parts], np.float64),
            np.array([p[4] for p in parts], np.float32))


def train_judges(X, height, health, rows_mask):
    """Height and health, both from living trees only: a dead tree is a gap, not a crown."""
    alive = rows_mask & (health > 0)
    live = alive & ~np.isnan(height)
    bad = (health <= 2).astype(np.float32)
    weight = np.where(bad == 1, (bad[alive] == 0).sum() / max(bad[alive].sum(), 1), 1.0)
    heights = [treeml.Net([X.shape[1], 16, 1], l2=1e-3, seed=s).fit(
        X[live], height[live], epochs=300, batch=64, lr=3e-3, seed=s) for s in range(SEEDS)]
    healths = [treeml.Net([X.shape[1], 16, 1], out="sigmoid", l2=1e-3, seed=s).fit(
        X[alive], bad[alive], epochs=300, batch=64, lr=3e-3, weight=weight[alive],
        seed=s) for s in range(SEEDS)]
    return heights, healths


def match(found: np.ndarray, truth: np.ndarray) -> list[tuple[int, int]]:
    if len(found) == 0 or len(truth) == 0:
        return []
    d = np.hypot(found[:, None, 0] - truth[None, :, 0], found[:, None, 1] - truth[None, :, 1])
    pairs, used_f, used_t = [], set(), set()
    for i, j in sorted(zip(*np.nonzero(d < MATCH_M)), key=lambda ij: d[ij]):
        if i not in used_f and j not in used_t:
            used_f.add(i)
            used_t.add(j)
            pairs.append((int(i), int(j)))
    return pairs


def main() -> int:
    started = time.time()
    print("Reading both builds of the USDA flight...")
    builds = {name: prepare(name, flight) for name, flight in BUILDS.items()}
    names, rows, health, height, X = tree_table(builds)

    print("\nHolding out one row at a time (both builds), training on the other three:")
    tally = {n: {"live": 0, "found": 0, "extra": 0, "h_raw": [], "h_net": [], "poor": 0,
                 "poor_caught": 0, "flags": 0, "flags_right": 0, "ws_found": 0, "ws_extra": 0,
                 "gaps": 0, "gaps_right": 0, "empty": 0}
             for n in builds}
    for held in (1, 2, 3, 4):
        others = tuple(r for r in (1, 2, 3, 4) if r != held)
        finder = train_finder(builds, others, seed=held)
        heights, healths = train_judges(X, height, health, rows != held)
        for name, b in builds.items():
            model = treeml.TreeModel(finder, heights, healths, THRESHOLD, POOR_AT)
            heat = treeml.heat(model, b["grids"])
            heat[b["row_of"] != held] = 0
            found = treeml.peaks(heat, b["grids"], THRESHOLD)
            truth = [t for t in b["trees"] if t["row"] == held]
            truth_xy = np.array([(t["x"], t["y"]) for t in truth])
            live = np.array([t["health"] > 0 for t in truth])
            pairs = match(found, truth_xy[live])
            t = tally[name]
            t["live"] += int(live.sum())
            t["found"] += len(pairs)
            t["extra"] += len(found) - len(pairs)
            # The trees as a farmer's flight would give them: features at the found
            # places, against this build's other found trees.
            reference = np.array([treeml.tree_features(b["grids"], x, y) for x, y in found],
                                 np.float32)
            if len(found):
                both = treeml.relative(reference)          # all found in this one row
                net_h = treeml.mean_of(heights, both)
                p_poor = treeml.mean_of(healths, both)
                living = [tr for tr, alive in zip(truth, live) if alive]
                for i, j in pairs:
                    if living[j]["height_m"]:
                        t["h_raw"].append(reference[i, 0] - living[j]["height_m"])
                        t["h_net"].append(net_h[i] - living[j]["height_m"])
                flagged = p_poor >= POOR_AT
                t["flags"] += int(flagged.sum())
                matched = {i: living[j] for i, j in pairs}
                t["flags_right"] += sum(1 for i in np.flatnonzero(flagged)
                                        if i in matched and matched[i]["health"] <= 2)
                weak = [j for j, tr in enumerate(living) if tr["health"] <= 2]
                t["poor"] += len(weak)
                t["poor_caught"] += sum(1 for i, j in pairs if j in weak and flagged[i])
            empty = truth_xy[~live]
            if len(found) >= 10:
                _, _, _, gap_xy = treeml.row_gaps(found, min_row=5)
                hit = match(gap_xy, empty) if len(gap_xy) else []
                t["gaps"] += len(gap_xy)
                t["gaps_right"] += len(hit)
            t["empty"] += len(empty)
            crowns = gpd.read_file(WORKSPACE / "out" / BUILDS[name] / "metrics_watershed.geojson")
            cxy = np.c_[crowns.geometry.centroid.x, crowns.geometry.centroid.y]
            cr, cc = b["grids"].rowcol(cxy[:, 0], cxy[:, 1])
            inside = ((cr >= 0) & (cr < b["row_of"].shape[0]) & (cc >= 0)
                      & (cc < b["row_of"].shape[1]))
            keep = np.zeros(len(cxy), bool)
            keep[inside] = b["row_of"][cr[inside].astype(int), cc[inside].astype(int)] == held
            ws_pairs = match(cxy[keep], truth_xy[live])
            t["ws_found"] += len(ws_pairs)
            t["ws_extra"] += int(keep.sum()) - len(ws_pairs)
        print(f"  row {held} done ({time.time() - started:.0f}s)", flush=True)

    scores = {}
    print("\nON ROWS THE MODEL NEVER SAW")
    for name, t in tally.items():
        raw, net = np.array(t["h_raw"]), np.array(t["h_net"])
        scores[name] = s = {
            "trees_found": round(t["found"] / t["live"], 3),
            "found_that_are_trees": round(t["found"] / max(t["found"] + t["extra"], 1), 3),
            "watershed_found": round(t["ws_found"] / t["live"], 3),
            "watershed_found_that_are_trees": round(
                t["ws_found"] / max(t["ws_found"] + t["ws_extra"], 1), 3),
            "height_typical_miss_m": round(float(np.median(np.abs(net))), 2),
            "height_bias_m": round(float(net.mean()), 2),
            "canopy_model_typical_miss_m": round(float(np.median(np.abs(raw))), 2),
            "canopy_model_bias_m": round(float(raw.mean()), 2),
            "poor_living_trees_caught": f"{t['poor_caught']} of {t['poor']}",
            "flags_that_were_poor": f"{t['flags_right']} of {t['flags']}",
            "empty_spots_found_as_gaps": f"{t['gaps_right']} of {t['empty']}",
            "gaps_that_were_empty": f"{t['gaps_right']} of {t['gaps']}",
        }
        print(f"  {name} build ({t['live']} living trees)")
        print(f"    trees found        {s['trees_found']:.0%} (watershed {s['watershed_found']:.0%});"
              f" {s['found_that_are_trees']:.0%} of what it found are trees "
              f"(watershed {s['watershed_found_that_are_trees']:.0%})")
        print(f"    height             typical miss {s['height_typical_miss_m']:.2f} m, "
              f"bias {s['height_bias_m']:+.2f} m (canopy model alone: "
              f"{s['canopy_model_typical_miss_m']:.2f} m, {s['canopy_model_bias_m']:+.2f} m)")
        print(f"    poor living trees  {s['poor_living_trees_caught']} caught; of the trees it "
              f"flagged, {s['flags_that_were_poor']} were poor")
        print(f"    dead or missing    {s['empty_spots_found_as_gaps']} empty spots found as gaps "
              f"in the row; of the gaps, {s['gaps_that_were_empty']} really were empty")

    print("\nTraining the final model on all four rows...")
    finder = train_finder(builds, (1, 2, 3, 4), seed=0)
    heights, healths = train_judges(X, height, health, np.ones(len(rows), bool))
    card = {
        "trained_on": "USDA-ARS 'Bingo' mandarin rootstock trial, Fort Pierce FL, 2021: 206 "
                      "hand-measured trees in 4 rows, two builds of one flight "
                      "(doi:10.15482/USDA.ADC/26946823, public domain)",
        "trained": date.today().isoformat(),
        "networks": "finder 22-32-16-1; height and health 36-16-1, 5 of each averaged",
        "scored_on": "each row held out in turn, never trained on",
        "scores": scores,
        "caution": "One grove, one variety, three-year-old trees. On another orchard the "
                   "counts are a reasonable guess, not a measured one.",
    }
    path = treeml.TreeModel(finder, heights, healths, THRESHOLD, POOR_AT, card).save()
    print(f"  {path} ({path.stat().st_size / 1e3:.0f} kB, {time.time() - started:.0f}s in all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
