"""Flags: turn per-unit measurements into a verdict a grower can act on.

Every unit is judged against its own field's distribution, not an absolute
threshold, because what counts as a small sorghum plant depends on the variety,
the planting date and the season.

Four outcomes:

``MISSING``  plants absent. For rows, a segment with almost no canopy; for an
             orchard, an empty position in the inferred planting grid.
``DEAD``     structure present but nothing alive in it, or a crown collapsed
             to almost nothing.
``STRESSED`` smaller or less green than most of the field.
``HEALTHY``  everything else.

The distinction between missing and dead matters to the grower: a missing plant
is a stand problem, fixed by replanting; a dead standing plant points at disease
or drought.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiPoint, Point

log = logging.getLogger(__name__)

FLAGS = ("HEALTHY", "STRESSED", "DEAD", "MISSING")

#: Per the spec: a unit in the bottom 15% of its field's volume or greenness.
DEFAULT_STRESSED_QUANTILE = 0.15

#: A percentile rule alone always flags that share of the field, a perfect one
#: included, because percentiles are relative by construction. A z-score guard
#: does not fix this: any z threshold also flags a fixed share of a normal
#: distribution, so a perfectly healthy field still lost 11% of its plants to
#: STRESSED. What is needed is an effect size. A unit must also sit this far
#: below the field's median, as a fraction of that median, which does not depend
#: on how uniform the field happens to be. Set to None for the percentile rule
#: on its own.
DEFAULT_STRESSED_MIN_SHORTFALL = 0.20

#: The shortfall guard fails the opposite way on a field that varies a lot:
#: healthy orchard volume scales with height times radius squared, so the
#: smallest healthy tree carries 69% of nominal, a 31% shortfall. A plant must
#: therefore also sit outside this field's own normal spread. Each guard covers
#: the other's blind spot; a genuinely stressed plant fails both.
DEFAULT_STRESSED_MIN_Z = -1.5

#: Greenness this many robust standard deviations under the median is dead...
DEFAULT_DEAD_Z = -3.0

#: ...but only if it has also lost most of the field's typical greenness. A
#: z-score alone scales with how uniform the field is: on a very even orchard a
#: tree at 43% of normal greenness sits far past -3 sd and would be called dead
#: while clearly alive. Requiring the loss of three quarters of the median ExG
#: makes the verdict independent of how uniform the rest of the field happens to be.
DEFAULT_DEAD_COLOUR_FRACTION = 0.25

#: A crown holding less than this share of the median volume has collapsed.
DEFAULT_DEAD_VOLUME_FRACTION = 0.10

#: A row segment with less than this share of the field's median canopy cover is
#: missing plants. Relative, not absolute: a full-width segment always includes
#: the furrow either side, so even a perfect row only reaches about 57% cover,
#: and an absolute 10% threshold caught 1 of 74 true gaps. Measured on the
#: synthetic field, gap segments carry 52% of median cover and stunted ones 88%;
#: 70% sits between them and catches 60 of 74 gaps while calling 0.5% of healthy
#: segments missing.
DEFAULT_MISSING_COVER_FRACTION = 0.70

#: A row segment holding less than this share of the typical segment area was
#: clipped by the edge of the field or the reconstruction, and is reported as
#: EDGE rather than judged. Clipped across the row it keeps the bare furrow and
#: loses the crop ridge, so it reads as missing plants: on the synthetic field
#: partial segments were flagged at 24% against 10% for whole ones, and nine
#: false MISSING flags lined the east edge, one per row.
DEFAULT_EDGE_AREA_FRACTION = 0.75

#: Above this share of the inferred planting grid coming up empty, the grid is
#: wrong and no missing-tree count is reported.
#:
#: The grid's spacing comes from the distance between neighbouring crowns, so it
#: is only as good as the detection, and two shapes of planting break it.
#:
#: Trees grown into each other: the detector cuts one crown for two, the gaps it
#: measures are doubled, and the lattice has twice the positions the orchard has
#: trees. On the USDA citrus trial that gave 4.32 m spacing where the trees stand
#: 2.13 m apart, and 272 missing trees in a grove that is nearly fully planted.
#:
#: Rows much further apart than the trees within them: both spacings collapse onto
#: the smaller one, and the lattice fills the alleys with trees nobody planted. A
#: flawless 7.7 m by 2.1 m orchard with every tree standing reports two thirds of
#: itself missing.
#:
#: An orchard really does lose trees, in ones and twos, and we would rather report
#: a gappy orchard than hide one. But a grid whose own positions are more than half
#: empty has not found the planting pattern, and saying so is worth more than a
#: number nobody should act on.
MAX_EMPTY_GRID_SHARE = 0.5

#: Verdicts that are not judgements of the crop, and so never count as problems.
NOT_ASSESSED = ("EDGE", "NO_DATA")

#: Converts an interquartile range into a standard-deviation equivalent.
IQR_TO_SIGMA = 1.349


class FlagError(RuntimeError):
    """Raised when flags cannot be assigned from the given metrics."""


def structure_column(method: str) -> str:
    """Which measure of size to compare units on, for a detection method.

    Row segments are fixed-size tiles, so any difference in their volume is
    either real stunting or an artifact of where the field boundary clipped
    them. Volume scales with area, so a segment clipped to half its length holds
    half the volume and reads as stunted while its canopy is perfectly healthy.
    Measured on the synthetic field, that artifact alone caused 109 of 214 false
    alarms. Mean canopy height is intensive, so clipping cannot move it.

    Crowns are the opposite case: a stressed tree grows a smaller crown, so its
    area is the signal and raw volume is the right comparison.
    """
    return "height_mean_m" if method == "rows" else "volume_m3"


@dataclass(frozen=True)
class FlagRules:
    """Thresholds behind each verdict, recorded alongside the output."""

    stressed_quantile: float = DEFAULT_STRESSED_QUANTILE
    stressed_min_shortfall: float | None = DEFAULT_STRESSED_MIN_SHORTFALL
    stressed_min_z: float | None = DEFAULT_STRESSED_MIN_Z
    dead_z: float = DEFAULT_DEAD_Z
    dead_colour_fraction: float = DEFAULT_DEAD_COLOUR_FRACTION
    dead_volume_fraction: float = DEFAULT_DEAD_VOLUME_FRACTION
    missing_cover_fraction: float = DEFAULT_MISSING_COVER_FRACTION
    edge_area_fraction: float = DEFAULT_EDGE_AREA_FRACTION


# --------------------------------------------------------------------------- #
# Robust statistics
# --------------------------------------------------------------------------- #


def robust_z(values: pd.Series) -> pd.Series:
    """Distance from the median in interquartile units.

    A few dead plants would drag an ordinary mean and standard deviation toward
    themselves and so hide their own anomaly; median and IQR ignore them.
    """
    finite = values.dropna()
    if finite.empty:
        return pd.Series(np.nan, index=values.index)
    median = finite.median()
    spread = (finite.quantile(0.75) - finite.quantile(0.25)) / IQR_TO_SIGMA
    if spread <= 0:
        return pd.Series(0.0, index=values.index)
    return (values - median) / spread


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def classify_units(
    metrics: gpd.GeoDataFrame, *, method: str, rules: FlagRules = FlagRules(),
    group_column: str | None = None,
) -> gpd.GeoDataFrame:
    """Assign one flag per unit, with the reason that decided it.

    Checks run from the most severe down, so a unit is reported for its worst
    problem. Units with no data at all are flagged as such rather than guessed.

    With ``group_column`` every statistic is taken within each group rather than
    across the flight, because a field holding two varieties or two planting
    dates would otherwise see the shorter one called stressed for being itself.
    """
    if metrics.empty:
        raise FlagError("no metrics to classify; run 'metrics' first")
    if group_column is not None and group_column not in metrics:
        raise FlagError(f"no column {group_column!r} to judge units within")

    if group_column is None:
        frame = _classify_group(metrics.copy(), method=method, rules=rules, scope="field")
    else:
        parts = [
            _classify_group(part.copy(), method=method, rules=rules, scope="block")
            for _, part in metrics.groupby(group_column, sort=False, dropna=False)
        ]
        frame = gpd.GeoDataFrame(pd.concat(parts).loc[metrics.index],
                                 geometry=metrics.geometry.name, crs=metrics.crs)

    counts = frame["flag"].value_counts().to_dict()
    log.info("flags: %s", ", ".join(f"{k} {counts.get(k, 0)}" for k in FLAGS))
    return frame


#: A block with fewer units than this has no distribution worth comparing to;
#: its units are reported as not assessed rather than judged on noise.
MIN_GROUP_UNITS = 8


def _classify_group(frame: gpd.GeoDataFrame, *, method: str, rules: FlagRules,
                    scope: str) -> gpd.GeoDataFrame:
    """Judge one set of units against its own distribution."""
    if scope != "field" and len(frame) < MIN_GROUP_UNITS:
        frame["size_z"] = np.nan
        frame["exg_z"] = np.nan
        frame["flag"] = "NO_DATA"
        frame["reason"] = f"only {len(frame)} unit(s) in this block, too few to judge"
        return frame

    size = frame[structure_column(method)]
    has_colour = "exg_mean" in frame and frame["exg_mean"].notna().any()

    frame["size_z"] = robust_z(size)
    frame["exg_z"] = robust_z(frame["exg_mean"]) if has_colour else np.nan
    size_cut = size.quantile(rules.stressed_quantile)
    exg_cut = frame["exg_mean"].quantile(rules.stressed_quantile) if has_colour else np.nan
    median_size = size.median()
    median_exg = frame["exg_mean"].median() if has_colour else np.nan
    median_cover = frame["canopy_cover"].median() if "canopy_cover" in frame else np.nan
    median_area = frame["area_m2"].median() if "area_m2" in frame else np.nan

    flags, reasons = [], []
    for row in frame.itertuples():
        flag, reason = _verdict(
            row, method=method, rules=rules, has_colour=has_colour,
            size=getattr(row, structure_column(method)),
            size_cut=size_cut, exg_cut=exg_cut, median_size=median_size,
            median_exg=median_exg, median_cover=median_cover,
            median_area=median_area, scope=scope,
        )
        flags.append(flag)
        reasons.append(reason)

    frame["flag"] = flags
    frame["reason"] = reasons
    return frame


def _verdict(
    row, *, method, rules, has_colour, size, size_cut, exg_cut, median_size,
    median_exg, median_cover, median_area, scope="field",
):
    """Decide one unit's flag, most severe first."""
    noun = "canopy height" if method == "rows" else "volume"
    if getattr(row, "n_pixels", 1) == 0 or not np.isfinite(size):
        return "NO_DATA", "unit covers no measured pixels"

    area = getattr(row, "area_m2", np.nan)
    if (
        method == "rows"
        and np.isfinite(area) and np.isfinite(median_area) and median_area > 0
        and area < rules.edge_area_fraction * median_area
    ):
        return "EDGE", (
            f"clipped to {area / median_area:.0%} of a full segment at the {scope} "
            "edge, not a fair sample of its row"
        )

    if (
        method == "rows"
        and np.isfinite(row.canopy_cover)
        and np.isfinite(median_cover) and median_cover > 0
        and row.canopy_cover < rules.missing_cover_fraction * median_cover
    ):
        return "MISSING", (
            f"{row.canopy_cover:.0%} canopy cover against a {scope} median of "
            f"{median_cover:.0%}, plants absent"
        )

    if median_size > 0 and size < rules.dead_volume_fraction * median_size:
        return "DEAD", (
            f"{noun} is under {rules.dead_volume_fraction:.0%} of the {scope} median"
        )
    if has_colour and _colour_dead(row, rules, median_exg):
        return "DEAD", (
            f"greenness {row.exg_z:+.1f} sd below the {scope} median, under "
            f"{rules.dead_colour_fraction:.0%} of its typical level"
        )

    small = size < size_cut and _material(size, median_size, row.size_z, rules)
    pale = (
        has_colour and np.isfinite(row.exg_mean)
        and row.exg_mean < exg_cut
        and _material(row.exg_mean, median_exg, row.exg_z, rules)
    )
    if small or pale:
        parts = []
        if small:
            parts.append(f"{noun} in the bottom {rules.stressed_quantile:.0%} of the {scope}")
        if pale:
            parts.append(f"greenness in the bottom {rules.stressed_quantile:.0%} of the {scope}")
        return "STRESSED", " and ".join(parts)

    return "HEALTHY", ""


def _colour_dead(row, rules: FlagRules, median_exg: float) -> bool:
    """True when a unit has lost nearly all of the field's typical greenness."""
    if not (np.isfinite(row.exg_z) and row.exg_z < rules.dead_z):
        return False
    if not np.isfinite(median_exg) or median_exg <= 0:
        # Without a positive typical greenness there is no level to fall from,
        # so the deviation alone has to decide.
        return True
    return bool(row.exg_mean < rules.dead_colour_fraction * median_exg)


def _material(value: float, median: float, z: float, rules: FlagRules) -> bool:
    """True when a unit is both materially smaller and unusual for this field.

    Two guards, because each alone fails in the opposite situation. A shortfall
    against the median ignores how uniform the field is, so an even field does
    not see its ordinary variation flagged; a robust z-score respects the
    field's own spread, so a naturally variable field does not either. A
    genuinely stressed plant fails both. Either guard can be disabled with None.
    """
    shortfall_ok = True
    if rules.stressed_min_shortfall is not None:
        if np.isfinite(value) and np.isfinite(median) and median > 0:
            shortfall_ok = bool(value < (1.0 - rules.stressed_min_shortfall) * median)
    spread_ok = True
    if rules.stressed_min_z is not None:
        spread_ok = bool(np.isfinite(z) and z < rules.stressed_min_z)
    return shortfall_ok and spread_ok


# --------------------------------------------------------------------------- #
# Planting grid
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlantingGrid:
    """A regular planting pattern recovered from where the crowns are."""

    angle_deg: float        # rotation of the grid's first axis
    spacing_a_m: float      # spacing along that axis
    spacing_b_m: float      # spacing along the perpendicular axis
    origin: tuple[float, float]
    n_crowns: int

    def rotate(self, xy: np.ndarray) -> np.ndarray:
        """Map world coordinates into the grid's own axis-aligned frame."""
        theta = math.radians(self.angle_deg)
        rotation = np.array([[math.cos(theta), math.sin(theta)],
                             [-math.sin(theta), math.cos(theta)]])
        return (xy - np.asarray(self.origin)) @ rotation.T

    def unrotate(self, uv: np.ndarray) -> np.ndarray:
        """Map grid-frame coordinates back into the world."""
        theta = math.radians(self.angle_deg)
        rotation = np.array([[math.cos(theta), -math.sin(theta)],
                             [math.sin(theta), math.cos(theta)]])
        return uv @ rotation.T + np.asarray(self.origin)


def infer_planting_grid(centroids: np.ndarray) -> PlantingGrid:
    """Recover grid angle and both spacings from crown positions.

    The displacements from each crown to its nearest neighbours cluster along the
    grid's two axes. Their angles, folded into a quarter turn, give the grid's
    rotation; their lengths along each axis give the spacings.

    Raises:
        FlagError: with fewer than four crowns, since no grid can be inferred.
    """
    from scipy.spatial import cKDTree

    points = np.asarray(centroids, dtype=np.float64)
    if len(points) < 4:
        raise FlagError(
            f"only {len(points)} crown(s); a planting grid needs at least four"
        )

    tree = cKDTree(points)
    k = min(5, len(points))
    distances, indices = tree.query(points, k=k)
    vectors = points[indices[:, 1:]] - points[:, None, :]
    vectors = vectors.reshape(-1, 2)
    lengths = np.hypot(vectors[:, 0], vectors[:, 1])

    # Keep the nearest neighbours only: diagonals and second rings add noise.
    nearest = lengths <= np.percentile(lengths[lengths > 0], 60) * 1.15
    vectors, lengths = vectors[nearest], lengths[nearest]

    angles = np.degrees(np.arctan2(vectors[:, 1], vectors[:, 0])) % 90.0
    histogram, edges = np.histogram(angles, bins=90, range=(0, 90))
    peak = edges[np.argmax(histogram)] + 0.5
    close = np.abs(((angles - peak + 45) % 90) - 45) < 5
    angle = float(np.median(angles[close])) if close.any() else float(peak)

    theta = math.radians(angle)
    axis_a = np.array([math.cos(theta), math.sin(theta)])
    axis_b = np.array([-math.sin(theta), math.cos(theta)])
    along_a = np.abs(vectors @ axis_a)
    along_b = np.abs(vectors @ axis_b)
    spacing_a = float(np.median(along_a[along_a > along_b])) if (along_a > along_b).any() else float(np.median(lengths))
    spacing_b = float(np.median(along_b[along_b > along_a])) if (along_b > along_a).any() else spacing_a

    provisional = PlantingGrid(angle, spacing_a, spacing_b, (0.0, 0.0), len(points))
    uv = provisional.rotate(points)
    phase_u = _circular_phase(uv[:, 0], spacing_a)
    phase_v = _circular_phase(uv[:, 1], spacing_b)
    origin = provisional.unrotate(np.array([[phase_u, phase_v]]))[0]

    grid = PlantingGrid(angle, spacing_a, spacing_b, (float(origin[0]), float(origin[1])), len(points))
    log.info(
        "planting grid: %.2f m x %.2f m at %.1f deg from %d crown(s)",
        spacing_a, spacing_b, angle, len(points),
    )
    return grid


def _circular_phase(values: np.ndarray, spacing: float) -> float:
    """Offset of a periodic set of positions, robust to the wrap-around."""
    angles = 2 * math.pi * (values % spacing) / spacing
    mean = math.atan2(np.sin(angles).mean(), np.cos(angles).mean())
    return (mean % (2 * math.pi)) / (2 * math.pi) * spacing


def find_missing_positions(
    grid: PlantingGrid,
    centroids: np.ndarray,
    *,
    tolerance: float = 0.45,
    area=None,
    max_empty_share: float = MAX_EMPTY_GRID_SHARE,
) -> np.ndarray:
    """Grid positions with no crown within ``tolerance`` of a spacing.

    Only positions inside ``area`` are considered, which defaults to
    :func:`planted_area`. Without a bound the grid would extend past the orchard
    edge and report every empty headland position as a missing tree.

    Raises:
        FlagError: when more than ``max_empty_share`` of the grid is empty, which
            means the spacing is wrong rather than the orchard being bare. See
            :data:`MAX_EMPTY_GRID_SHARE`.
    """
    from scipy.spatial import cKDTree

    points = np.asarray(centroids, dtype=np.float64)
    uv = grid.rotate(points)
    lo = uv.min(axis=0) - np.array([grid.spacing_a_m, grid.spacing_b_m])
    hi = uv.max(axis=0) + np.array([grid.spacing_a_m, grid.spacing_b_m])

    us = np.arange(
        math.floor(lo[0] / grid.spacing_a_m), math.ceil(hi[0] / grid.spacing_a_m) + 1
    ) * grid.spacing_a_m
    vs = np.arange(
        math.floor(lo[1] / grid.spacing_b_m), math.ceil(hi[1] / grid.spacing_b_m) + 1
    ) * grid.spacing_b_m
    lattice_uv = np.array([(u, v) for u in us for v in vs])
    lattice = grid.unrotate(lattice_uv)

    if area is None:
        area = planted_area(grid, points)

    from shapely import contains_xy

    inside = contains_xy(area, lattice[:, 0], lattice[:, 1])
    lattice = lattice[inside]

    nearest, _ = cKDTree(points).query(lattice, k=1)
    limit = tolerance * min(grid.spacing_a_m, grid.spacing_b_m)
    missing = lattice[nearest > limit]
    log.info("found %d empty grid position(s) of %d expected", len(missing), len(lattice))

    empty_share = len(missing) / len(lattice) if len(lattice) else 0.0
    if empty_share > max_empty_share:
        raise FlagError(
            f"the planting grid it worked out ({grid.spacing_a_m:.2f} m x "
            f"{grid.spacing_b_m:.2f} m) leaves {empty_share:.0%} of its own positions "
            f"empty, which is not an orchard, it is the wrong grid. Two things do this: "
            f"trees grown into each other, where the detector cuts one crown for two and "
            f"the spacing comes out about double; and rows much further apart than the "
            f"trees within them, where both spacings collapse onto the smaller one and "
            f"the lattice fills the alleys with trees that were never planted. Either "
            f"way the phantom positions read as missing trees. Counting "
            f"{len(points)} crown(s) is still sound."
        )
    return missing


def planted_area(grid: PlantingGrid, points: np.ndarray):
    """The rectangle the planting occupies, aligned with the planting grid.

    A convex hull of the detected crowns is the obvious choice and the wrong
    one: when a corner tree is missing, the hull cuts that corner off, so the
    missing tree falls outside the search and is never reported. Its row and its
    column both still hold other trees, so a rectangle in the grid's own frame
    keeps it. The trade is an L-shaped orchard, where the rectangle would take in
    the empty notch; pass the field outline as ``area`` for one of those.
    """
    from shapely.geometry import Polygon

    uv = grid.rotate(np.asarray(points, dtype=np.float64))
    margin = 0.25 * min(grid.spacing_a_m, grid.spacing_b_m)
    (u0, v0), (u1, v1) = uv.min(axis=0) - margin, uv.max(axis=0) + margin
    corners = np.array([[u0, v0], [u1, v0], [u1, v1], [u0, v1]])
    return Polygon(grid.unrotate(corners))


# --------------------------------------------------------------------------- #
# Row gaps
# --------------------------------------------------------------------------- #


def gap_runs(flagged: gpd.GeoDataFrame, *, segment_m: float) -> gpd.GeoDataFrame:
    """Merge consecutive missing segments along each row into single gaps.

    A grower replants a stretch of row, not a list of two-metre segments, so a
    four-segment gap is reported once with its length.
    """
    missing = flagged[flagged["flag"] == "MISSING"].sort_values(["row", "segment"])
    records = []
    for row_index, group in missing.groupby("row"):
        segments = group["segment"].to_list()
        geometries = group.geometry.to_list()
        start = 0
        for position in range(1, len(segments) + 1):
            ends_run = position == len(segments) or segments[position] != segments[position - 1] + 1
            if not ends_run:
                continue
            run = geometries[start:position]
            merged = gpd.GeoSeries(run, crs=flagged.crs).union_all()
            records.append({
                "row": int(row_index),
                "first_segment": int(segments[start]),
                "last_segment": int(segments[position - 1]),
                "n_segments": position - start,
                "length_m": (position - start) * segment_m,
                "geometry": merged,
            })
            start = position

    if not records:
        return gpd.GeoDataFrame(
            columns=["row", "first_segment", "last_segment", "n_segments",
                     "length_m", "geometry"],
            geometry="geometry", crs=flagged.crs,
        )
    return gpd.GeoDataFrame(records, geometry="geometry", crs=flagged.crs).sort_values(
        "length_m", ascending=False
    ).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


def summarise_flags(
    flagged: gpd.GeoDataFrame, *, n_missing_positions: int = 0
) -> dict[str, object]:
    """Counts and shares per flag, plus field-level figures for the report."""
    counts = flagged["flag"].value_counts().to_dict()
    total = len(flagged)
    summary: dict[str, object] = {"n_units": total}
    for flag in FLAGS:
        n = int(counts.get(flag, 0))
        if flag == "MISSING":
            n += n_missing_positions
        summary[f"n_{flag.lower()}"] = n
    denominator = total + n_missing_positions
    for flag in FLAGS:
        summary[f"share_{flag.lower()}"] = (
            summary[f"n_{flag.lower()}"] / denominator if denominator else 0.0
        )
    summary["n_no_data"] = int(counts.get("NO_DATA", 0))
    summary["n_edge"] = int(counts.get("EDGE", 0))
    return summary
