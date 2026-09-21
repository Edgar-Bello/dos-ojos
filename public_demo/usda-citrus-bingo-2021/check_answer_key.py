"""Score this flight against the USDA team's own tape measure.

FREE PUBLIC DATA, NOT OURS. The team measured all 206 trees by hand in April 2021,
weeks before the flight (Ag Data Commons doi:10.15482/USDA.ADC/26946823), so this is
the one place in Dos Ojos where we can say how wrong we are in centimetres instead of
saying a result looks about right.

Two questions, kept apart, because they fail differently:

1. **Does `detect` find one crown per tree?** Counted per row against the key.
2. **Does the canopy model get a tree's height right?** Read at each tree's own place
   in the planting lattice, so the answer does not depend on how the crowns were cut.

The lattice is registered on the crown positions alone - slide and spacing chosen so
the crowns we found sit on planting spots. The key's heights are never used to place
a tree, only to score it.

Run it from any folder, after run_demo.sh:

    dosojos_drone/.venv/Scripts/python.exe public_demo/usda-citrus-bingo-2021/check_answer_key.py
"""

from __future__ import annotations

import statistics as st
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio

HERE = Path(__file__).resolve().parent
OUT = HERE / "dosojos_drone/out/PUBLIC-usda-bingo-20210512"
DEM = HERE / "dosojos_drone/data/odm/PUBLIC-usda-bingo-20210512/odm_dem"
KEY = HERE / "download/Bingo ground data_Rows 1-4_2021.xlsx"
FIGURE = OUT / "answer_key.png"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: A tree is read within this much of its place in the lattice.
SAMPLE_R_M = 0.9
#: Rows with all 63 planting positions; row 4 stops at 27.
FULL_ROWS = (1, 2, 3)
ROW_POSITIONS = {1: 63, 2: 63, 3: 63, 4: 27}
#: The mosaic covers eight rows of citrus; only four of them are the trial.
TRIAL_ROWS = 4
#: Fewer crowns than this in a line is a stray bush, not a row of the grove.
MIN_ROW_CROWNS = 10
BANNER = "FREE PUBLIC DATA - USDA-ARS Fort Pierce, not a Dos Ojos field"


class CheckError(RuntimeError):
    """Something the check needs is missing or not shaped as expected."""


def answer_key() -> list[dict]:
    """Every tree the team measured: row, position, height in metres, health 0-5."""
    if not KEY.exists():
        raise CheckError(f"the answer key is not here: {KEY}")
    zf = zipfile.ZipFile(KEY)
    shared = ["".join(t.text or "" for t in si.iter(NS + "t"))
              for si in ET.fromstring(zf.read("xl/sharedStrings.xml"))]
    sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
    trees = []
    for row in sheet.iter(NS + "row"):
        cells = {}
        for cell in row.iter(NS + "c"):
            column = "".join(ch for ch in cell.get("r") if ch.isalpha())
            value = cell.find(NS + "v")
            if value is not None:
                cells[column] = (shared[int(value.text)] if cell.get("t") == "s"
                                 else value.text)
        if cells.get("B", "").isdigit():
            trees.append({
                "row": int(cells["C"]), "pos": int(cells["D"]),
                "height_m": float(cells["H"]) / 100 if cells.get("H") else None,
                "health": int(cells["K"]) if cells.get("K") else None,
            })
    if not trees:
        raise CheckError("the answer key has no trees in it")
    return trees


def rotate(x, y, degrees):
    """The same points with the rows running up the page instead of askew."""
    angle = np.radians(degrees)
    return (x * np.cos(angle) + y * np.sin(angle),
            -x * np.sin(angle) + y * np.cos(angle))


def peak(raster, band, x, y, radius=SAMPLE_R_M):
    """The tallest reading within ``radius`` of a place; None where there is none."""
    row, column = raster.index(x, y)
    half = int(round(radius / abs(raster.transform.a)))
    window = band[max(row - half, 0):row + half + 1, max(column - half, 0):column + half + 1]
    return None if window.count() == 0 else float(window.max())


def floor_level(raster, band, x, y, radius=0.6):
    """The usual ground around a place. A maximum would pick up the grass."""
    row, column = raster.index(x, y)
    half = int(round(radius / abs(raster.transform.a)))
    window = band[max(row - half, 0):row + half + 1, max(column - half, 0):column + half + 1]
    return None if window.count() == 0 else float(np.ma.median(window))


def find_rows(across, along):
    """The rows of citrus in the mosaic, west to east.

    A row of sixty trees and a lone bush beyond the farm road both look like a
    cluster in this direction, so anything too short to be a row is dropped. At
    higher point-cloud quality the detector picks up two such strays, and without
    this the trial rows would be counted from the wrong end.
    """
    order = np.argsort(across)
    bands, current = [], [order[0]]
    for left, right in zip(order[:-1], order[1:]):
        if across[right] - across[left] > 3.0:
            bands.append(current)
            current = []
        current.append(right)
    bands.append(current)
    bands = [band for band in bands if len(band) >= MIN_ROW_CROWNS]
    bands.sort(key=lambda ids: across[ids].mean())
    return bands


def register(seen, start, spacing):
    """Slide and stretch the planting lattice onto the crowns we actually found.

    Nothing from the answer key is used here: the crowns alone say where the trees
    are. Returns the first tree's place, the spacing, and how far a crown now sits
    from a planting spot on average.
    """
    def misfit(first, step):
        spots = np.clip(np.round((seen - first) / step), 0, ROW_POSITIONS[1] - 1)
        return float(np.mean(np.abs(seen - (first + spots * step))))

    best = min(((misfit(start + slide, spacing * stretch), slide, stretch)
                for slide in np.arange(-1.5, 1.55, 0.05)
                for stretch in np.arange(0.96, 1.045, 0.005)), key=lambda z: z[0])
    error, slide, stretch = best
    return start + slide, spacing * stretch, error


def draw(measured, truth, path):
    """A scatter of what we measured against what the team measured."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(6.4, 6.4), dpi=140)
    axes.scatter(truth, measured, s=18, alpha=0.6, edgecolor="none", color="#2b7a4b")
    limit = max(truth.max(), measured.max()) * 1.05
    axes.plot([0, limit], [0, limit], color="#888", lw=1, ls="--", label="perfect agreement")
    axes.set_xlim(0, limit)
    axes.set_ylim(0, limit)
    axes.set_xlabel("measured by hand, April 2021 (m)")
    axes.set_ylabel("from the drone's canopy model (m)")
    axes.set_title(f"Tree height: {len(truth)} citrus trees against their answer key")
    axes.annotate(
        BANNER, xy=(0, 1), xycoords="axes fraction", xytext=(0, 34),
        textcoords="offset points", fontsize=9, color="white", fontweight="bold",
        bbox={"boxstyle": "square,pad=0.45", "facecolor": "#b3261e", "edgecolor": "none"})
    axes.legend(loc="lower right", frameon=False)
    axes.grid(alpha=0.25)
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)


def main() -> int:
    for needed in (OUT / "metrics_watershed.geojson", OUT / "chm.tif"):
        if not needed.exists():
            raise CheckError(f"{needed.name} is missing; run run_demo.sh first")

    crowns = gpd.read_file(OUT / "metrics_watershed.geojson")
    centres = crowns.geometry.centroid
    tilt = max(np.arange(-10, 10, 0.05),
               key=lambda d: (np.histogram(
                   rotate(centres.x.values, centres.y.values, d)[0], bins=400)[0] ** 2).sum())
    across, along = rotate(centres.x.values, centres.y.values, tilt)

    bands = find_rows(across, along)
    if len(bands) < TRIAL_ROWS:
        raise CheckError(
            f"found {len(bands)} row(s) of citrus in the mosaic, and the trial needs "
            f"{TRIAL_ROWS}. Either the detection failed or this is not the Bingo flight."
        )
    # Row 1 is the one beside the farm road, on the east. The four older rows further
    # west belong to somebody else's planting and are left out of this.
    band_of = {row: bands[-row] for row in (1, 2, 3, 4)}

    seen = np.concatenate([along[band_of[r]] for r in FULL_ROWS])
    rough_start = float(np.median([along[band_of[r]].min() for r in FULL_ROWS]))
    rough_spacing = (float(np.median([along[band_of[r]].max() for r in FULL_ROWS]))
                     - rough_start) / (ROW_POSITIONS[1] - 1)
    first_tree, spacing, fit_error = register(seen, rough_start, rough_spacing)
    row_gap = float(np.mean(np.diff([across[band_of[r]].mean() for r in (4, 3, 2, 1)])))

    key = answer_key()
    chm = rasterio.open(OUT / "chm.tif")
    chm_band = chm.read(1, masked=True)
    surface = ground = None
    if (DEM / "dsm.tif").exists() and (DEM / "dtm.tif").exists():
        surface, ground = rasterio.open(DEM / "dsm.tif"), rasterio.open(DEM / "dtm.tif")
        surface_band, ground_band = surface.read(1, masked=True), ground.read(1, masked=True)

    print(f"canopy model {chm.width} x {chm.height} at {abs(chm.transform.a) * 100:.0f} cm, "
          f"{chm.crs}")
    print(f"rows {abs(row_gap):.2f} m apart, trees {spacing:.2f} m apart, "
          f"{tilt:.2f} deg off north")
    print(f"lattice registered on the crowns alone: they sit {fit_error * 100:.0f} cm "
          f"from a planting spot on average\n")

    print("FINDING THE TREES - one crown per tree?")
    print("  row    in the key    crowns found    share")
    found_total = key_total = 0
    for row in (1, 2, 3, 4):
        expected = sum(1 for tree in key if tree["row"] == row)
        last = first_tree + (ROW_POSITIONS[row] - 1) * spacing
        inside = ((along[band_of[row]] >= first_tree - spacing / 2)
                  & (along[band_of[row]] <= last + spacing / 2))
        found = int(inside.sum())
        key_total += expected
        found_total += found
        print(f"   {row}        {expected:3d}           {found:3d}           "
              f"{found / expected:.0%}")
    print(f"  all        {key_total:3d}           {found_total:3d}           "
          f"{found_total / key_total:.0%}")
    print("  Trees 2.1 m apart in a row grow into each other, and the watershed cuts")
    print("  one crown where two trees touch. Counts are a floor, not a census.\n")

    print("HEIGHT - the canopy model against the tape measure")
    measured, truth, from_alley = [], [], []
    per_row: dict[int, list] = {}
    angle = np.radians(tilt)
    for tree in key:
        if tree["height_m"] is None:
            continue
        line = float(across[band_of[tree["row"]]].mean())
        place = first_tree + (tree["pos"] - 1) * spacing
        x = line * np.cos(angle) - place * np.sin(angle)
        y = line * np.sin(angle) + place * np.cos(angle)
        top = peak(chm, chm_band, x, y)
        if top is None:
            continue
        measured.append(top)
        truth.append(tree["height_m"])
        per_row.setdefault(tree["row"], []).append((top, tree["height_m"]))
        if surface is not None:
            roof = peak(surface, surface_band, x, y)
            floors = []
            for side in (-1, 1):
                alley = line + side * abs(row_gap) / 2
                fx = alley * np.cos(angle) - place * np.sin(angle)
                fy = alley * np.sin(angle) + place * np.cos(angle)
                got = floor_level(ground, ground_band, fx, fy)
                if got is not None:
                    floors.append(got)
            if roof is not None and floors:
                from_alley.append((roof - st.mean(floors), tree["height_m"]))

    for row in sorted(per_row):
        pairs = per_row[row]
        keys = [p[1] for p in pairs]
        ours = [p[0] for p in pairs]
        print(f"  row {row}: {len(pairs):3d} trees   key {st.mean(keys):.2f} m   "
              f"drone {st.mean(ours):.2f} m   short by {st.mean(keys) - st.mean(ours):.2f} m")

    ours = np.array(measured)
    keys = np.array(truth)
    errors = ours - keys
    print(f"\n  all {len(ours)} trees")
    print(f"    key             {keys.mean():.2f} m mean, {keys.min():.2f} to {keys.max():.2f}")
    print(f"    drone           {ours.mean():.2f} m mean, {ours.min():.2f} to {ours.max():.2f}")
    print(f"    short by        {-errors.mean():.2f} m ({-errors.mean() / keys.mean():.0%} "
          f"of the real height)")
    print(f"    typical miss    {np.median(np.abs(errors)):.2f} m")
    print(f"    within 25 cm    {np.mean(np.abs(errors) <= 0.25):.0%}")
    print(f"    correlation     {np.corrcoef(ours, keys)[0, 1]:.2f}  "
          f"(it ranks the trees right, it just reads them short)")

    if from_alley:
        alley_ours = np.array([p[0] for p in from_alley])
        alley_keys = np.array([p[1] for p in from_alley])
        moved = (alley_ours - alley_keys).mean() - errors.mean()
        print("\n  WHERE THE SHORTFALL COMES FROM")
        print(f"    same treetops, ground taken from the grass alley instead of from")
        print(f"    the ground model under the tree: short by "
              f"{-(alley_ours - alley_keys).mean():.2f} m")
        print(f"    so about {moved:.2f} m of it is the ground model sitting too high")
        print(f"    under the row, and about {-(alley_ours - alley_keys).mean():.2f} m is "
              f"the surface")
        print(f"    model not reaching the top of a citrus crown.")

    draw(ours, keys, FIGURE)
    print(f"\n  {FIGURE}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckError as exc:
        print(f"Cannot check this flight: {exc}", file=sys.stderr)
        sys.exit(1)
