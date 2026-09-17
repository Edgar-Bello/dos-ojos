"""Is the ground level, and does the stress follow it? Terrain and irrigation advice.

Most Valley fields are watered down furrows from a head ditch. Water that runs
downhill along the rows, soaks in as it goes, and has to reach the far end
before the set is changed. Two kinds of ground defeat that: a field that is not
evenly graded, where high spots stay dry and low spots pond, and rows too long
or too flat for the stream, whose tail ends never get their share.

The drone's ground model (the DTM) measures the first directly, to a few
centimetres. The flags measure the crop. Put together, they answer the question
a grower actually has: is my problem the water, and if so, is it how much or
how it is spread? The answer is a short list of findings, each with advice.

Method, all on a 0.5 m grid inside the field outline:

* a robust plane is fitted to the ground: its tilt is the field's grade, and
  what is left over (the relief) is what leveling would remove;
* high and low spots are connected patches standing 4 cm or more off the plane;
* each judged row piece gets its height off the plane and its position from the
  head of the rows to the tail, and Fisher's exact test asks whether the flagged
  pieces bunch anywhere more than chance allows.

Thresholds are rules of thumb from surface irrigation practice, stated as
constants so they can be tuned with local extension advice.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import shapely
from affine import Affine
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.features import geometry_mask, shapes
from scipy import ndimage, stats
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

log = logging.getLogger(__name__)

#: Analysis grid. Leveling and water flow care about metres, not centimetres.
GROUND_CELL_M = 0.5
#: Largest grid handled at GROUND_CELL_M before coarsening, in cells.
MAX_CELLS = 2_000_000
#: Smoothing before spots are traced, so a clod or a furrow ridge is not a spot.
SMOOTH_M = 2.0
#: A high or low spot stands this far off the plane...
SPOT_CM = 4.0
#: ...over at least this much ground, and this share of the field.
MIN_SPOT_M2 = 25.0
MIN_SPOT_SHARE = 0.005
#: A row piece counts as on high or low ground this far off the plane.
UNIT_OFF_PLANE_CM = 3.0
#: Laser-leveled fields sit within about 3 cm (0.1 ft) of their design grade.
LEVEL_TOLERANCE_CM = 3.0
WELL_LEVELED_SD_CM = 2.0
UNEVEN_SD_CM = 3.5
#: Grade along the irrigation direction, percent (m per 100 m).
LEVEL_SLOPE_PCT = 0.05
FURROW_MAX_PCT = 0.5
STEEP_PCT = 1.0
CROSS_SLOPE_PCT = 0.3
#: A bowl or dome this deep across a photogrammetry model may be the model.
DOME_RELIEF_CM = 5.0
#: A place is linked to the stress when flagged pieces are this much commoner
#: there, the test is this sure, and there are enough of them to mean anything.
LINK_RATIO = 1.5
LINK_P = 0.01
LINK_MIN_PROBLEMS = 10

#: Without rows to outline the crop, this much of the field's edge is left out:
#: turn rows, ditch banks and field roads stand off the grade the crop sits on.
HEADLAND_M = 3.0

#: Names of the parts of a field along the flow, with rows and without.
HEAD, TAIL = "head of the rows", "tail of the rows"
NEAR, FAR = "near end of the field", "far end of the field"

PROBLEM_FLAGS = ("STRESSED", "DEAD", "MISSING")
JUDGED_FLAGS = ("HEALTHY",) + PROBLEM_FLAGS
M3_TO_YD3 = 1.30795
SQ_M_PER_ACRE = 4046.8564224
SIDES = {"N": (0.0, 1.0), "S": (0.0, -1.0), "E": (1.0, 0.0), "W": (-1.0, 0.0)}
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
COMPASS_WORDS = {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east",
                 "S": "south", "SW": "south-west", "W": "west", "NW": "north-west"}


class TerrainError(RuntimeError):
    """Raised when the ground model cannot be analysed."""


@dataclass
class Spot:
    """A patch of ground standing off the plane."""

    label: str
    kind: str                  # "high" or "low"
    area_m2: float
    mean_cm: float
    peak_cm: float
    where: str
    n_units: int
    problem_share: float | None
    #: Whether its crop is flagged more than chance allows (the same test as the
    #: row ends), not merely more than the field average.
    linked: bool = False
    geometry: BaseGeometry | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("geometry")
        return payload


@dataclass
class Link:
    """Whether flagged row pieces are commoner in one place than elsewhere."""

    place: str
    n_in: int
    problems_in: int
    share_in: float
    share_out: float
    ratio: float
    p_value: float
    linked: bool


@dataclass
class Advice:
    """One finding about the ground and what to do about it."""

    topic: str
    finding: str
    advice: str
    priority: int              # 1 act on it, 2 worth knowing, 3 fine as it is


@dataclass
class TerrainReport:
    """Everything the terrain step found for one flight."""

    flight_id: str
    field_id: str | None
    ground_source: str
    method: str | None
    cell_m: float
    area_m2: float
    slope_pct: float
    downhill: str
    row_bearing_deg: float | None
    flow: str | None            # "N to S"
    flow_source: str | None     # "given" or "slope"
    along_pct: float | None
    cross_pct: float | None
    cross_low_side: str | None
    sd_cm: float
    relief_cm: float
    within_tolerance: float
    within_5cm: float
    cut_m3: float
    cut_yd3_per_acre: float
    curvature_cm: float
    curvature_kind: str
    spots: list[Spot]
    links: list[Link]
    field_problem_share: float | None
    n_judged: int
    advice: list[Advice]
    notes: list[str]
    #: Share of judged pieces flagged in each part of the field, for the chart:
    #: head, middle and tail of the rows; high, level and low ground.
    shares: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["spots"] = [s.to_dict() for s in self.spots]
        return payload


@dataclass
class Ground:
    """The ground model on the analysis grid, with its fitted plane."""

    z: np.ndarray               # metres, NaN outside the field
    transform: Affine
    crs: object
    cell_m: float
    plane: np.ndarray           # [intercept, d/dx, d/dy] in metres, around x0, y0
    origin: tuple[float, float]
    relief_cm: np.ndarray       # z minus the plane, cm
    smooth_cm: np.ndarray       # relief smoothed for spot tracing

    def xy(self) -> tuple[np.ndarray, np.ndarray]:
        """Map coordinates of every cell centre."""
        rows, cols = np.indices(self.z.shape)
        x = self.transform.c + (cols + 0.5) * self.transform.a
        y = self.transform.f + (rows + 0.5) * self.transform.e
        return x, y

    def sample(self, x: np.ndarray, y: np.ndarray, grid: np.ndarray) -> np.ndarray:
        """Values of ``grid`` under map points, NaN off the grid."""
        cols = np.floor((np.asarray(x) - self.transform.c) / self.transform.a).astype(int)
        rows = np.floor((np.asarray(y) - self.transform.f) / self.transform.e).astype(int)
        inside = (rows >= 0) & (rows < grid.shape[0]) & (cols >= 0) & (cols < grid.shape[1])
        out = np.full(len(cols), np.nan)
        out[inside] = grid[rows[inside], cols[inside]]
        return out


# --------------------------------------------------------------------------- #
# Ground model
# --------------------------------------------------------------------------- #


def load_ground(dtm_path: Path, field_shape: BaseGeometry | None = None,
                *, cell_m: float = GROUND_CELL_M) -> Ground:
    """Read the DTM onto the analysis grid, keep the field, and fit its plane.

    Args:
        field_shape: the field outline in the DTM's CRS; cells outside are dropped.

    Raises:
        TerrainError: if the DTM is unreadable, unprojected, or nearly empty.
    """
    try:
        with rasterio.open(dtm_path) as dataset:
            if dataset.crs is None or not dataset.crs.is_projected:
                raise TerrainError(
                    f"{Path(dtm_path).name} has no projected CRS; slopes need metres"
                )
            res = abs(dataset.transform.a)
            cell_m = max(cell_m, res)
            cells = (dataset.width * res / cell_m) * (dataset.height * res / cell_m)
            if cells > MAX_CELLS:
                cell_m *= math.sqrt(cells / MAX_CELLS)
            out_w = max(1, int(round(dataset.width * res / cell_m)))
            out_h = max(1, int(round(dataset.height * res / cell_m)))
            data = dataset.read(1, out_shape=(out_h, out_w), masked=True,
                                resampling=Resampling.average)
            transform = dataset.transform @ Affine.scale(dataset.width / out_w,
                                                         dataset.height / out_h)
            crs = dataset.crs
    except RasterioIOError as exc:
        raise TerrainError(f"could not read {dtm_path}: {exc}") from exc

    z = np.ma.filled(data.astype(float), np.nan)
    z[np.abs(z) > 1e5] = np.nan
    if field_shape is not None and not field_shape.is_empty:
        outside = geometry_mask([field_shape], out_shape=z.shape, transform=transform)
        z[outside] = np.nan
    if np.isfinite(z).sum() < 200:
        raise TerrainError(
            "fewer than 200 ground cells inside the field; check the DTM covers the "
            "field outline"
        )
    return fit_ground(z, transform, crs, abs(transform.a))


def fit_ground(z: np.ndarray, transform: Affine, crs, cell_m: float) -> Ground:
    """Fit the plane robustly and derive the relief grids."""
    rows, cols = np.indices(z.shape)
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    valid = np.isfinite(z)
    x0, y0 = float(x[valid].mean()), float(y[valid].mean())
    plane = _robust_fit(x[valid] - x0, y[valid] - y0, z[valid], order=1)
    fitted = plane[0] + plane[1] * (x - x0) + plane[2] * (y - y0)
    relief = (z - fitted) * 100.0
    return Ground(z=z, transform=transform, crs=crs, cell_m=cell_m, plane=plane,
                  origin=(x0, y0), relief_cm=relief,
                  smooth_cm=_smooth(relief, SMOOTH_M / cell_m))


def _design(x: np.ndarray, y: np.ndarray, order: int) -> np.ndarray:
    columns = [np.ones_like(x), x, y]
    if order == 2:
        columns += [x * x, x * y, y * y]
    return np.column_stack(columns)


def _robust_fit(x: np.ndarray, y: np.ndarray, z: np.ndarray, *, order: int) -> np.ndarray:
    """Least squares that sets aside ditches, berms and stray points (3 x MAD)."""
    keep = np.ones(len(z), dtype=bool)
    coefficients = np.zeros(3 if order == 1 else 6)
    for _ in range(3):
        coefficients, *_ = np.linalg.lstsq(_design(x[keep], y[keep], order), z[keep],
                                           rcond=None)
        residual = z - _design(x, y, order) @ coefficients
        mad = np.median(np.abs(residual[keep] - np.median(residual[keep])))
        if mad == 0:
            break
        keep = np.abs(residual - np.median(residual[keep])) <= 3.0 * 1.4826 * mad
    return coefficients


def _smooth(grid: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian smoothing that ignores the cells outside the field."""
    valid = np.isfinite(grid)
    num = ndimage.gaussian_filter(np.where(valid, grid, 0.0), sigma)
    den = ndimage.gaussian_filter(valid.astype(float), sigma)
    return np.where(valid, num / np.maximum(den, 1e-6), np.nan)


def curvature(ground: Ground) -> tuple[float, str]:
    """Depth of any bowl or dome across the field, beyond the plane, in cm."""
    x, y = ground.xy()
    valid = np.isfinite(ground.z)
    x0, y0 = ground.origin
    dx, dy = x[valid] - x0, y[valid] - y0
    quad = _robust_fit(dx, dy, ground.z[valid], order=2)
    extra = (_design(dx, dy, 2) @ quad - _design(dx, dy, 1) @ ground.plane) * 100.0
    depth = float(np.percentile(extra, 98) - np.percentile(extra, 2))
    kind = "dome" if quad[3] + quad[5] < 0 else "bowl"
    return depth, kind


# --------------------------------------------------------------------------- #
# Directions
# --------------------------------------------------------------------------- #


def bearing(dx: float, dy: float) -> float:
    """Compass bearing of a map vector, degrees clockwise from north."""
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def compass(dx: float, dy: float) -> str:
    """Nearest of the eight compass points for a map vector."""
    return COMPASS[int(((bearing(dx, dy) + 22.5) % 360) // 45)]


def row_axis(units: gpd.GeoDataFrame) -> np.ndarray | None:
    """Unit vector along the crop rows, from how each row's pieces line up.

    Pieces of one row sit in a line, so the main axis of their centres is the
    row direction whatever shape the pieces are cut to. Directions are axial
    (a row has no front), so they are averaged as doubled angles.
    """
    if units is None or units.empty or "row" not in units:
        return None
    cx, cy = units.geometry.centroid.x.to_numpy(), units.geometry.centroid.y.to_numpy()
    sums = np.zeros(2)
    for _, index in units.groupby("row").indices.items():
        if len(index) < 5:
            continue
        points = np.column_stack([cx[index] - cx[index].mean(), cy[index] - cy[index].mean()])
        _, _, vt = np.linalg.svd(points, full_matrices=False)
        angle = math.atan2(vt[0, 1], vt[0, 0])
        sums += len(index) * np.array([math.cos(2 * angle), math.sin(2 * angle)])
    if not sums.any():
        return None
    angle = 0.5 * math.atan2(sums[1], sums[0])
    return np.array([math.cos(angle), math.sin(angle)])


def flow_direction(axis: np.ndarray | None, gradient: np.ndarray, water_enters: str | None
                   ) -> tuple[np.ndarray | None, str | None, list[str]]:
    """Which way the irrigation water runs across the field.

    Along the rows, away from the side it enters from when that is known;
    otherwise downhill along the rows. Without rows (an orchard), straight from
    the entry side or downhill. Returns the unit vector, how it was decided, and
    notes for anything contradictory.
    """
    notes: list[str] = []
    fall = float(np.hypot(*gradient)) * 100.0
    if axis is None:
        if water_enters:
            side = np.array(SIDES[water_enters])
            return -side, "given", notes
        if fall >= LEVEL_SLOPE_PCT:
            return -gradient / np.hypot(*gradient), "slope", notes
        return None, None, notes

    if water_enters:
        side = np.array(SIDES[water_enters])
        if abs(float(axis @ side)) >= 0.5:
            return (axis if axis @ side < 0 else -axis), "given", notes
        notes.append(f"water_enters is {water_enters} but the rows run "
                     f"{compass(*axis)}-{compass(*-axis)}; judged from the slope instead")
    along = float(gradient @ axis)
    if abs(along) * 100.0 >= LEVEL_SLOPE_PCT / 2:
        return (-axis if along > 0 else axis), "slope", notes
    return None, None, notes


# --------------------------------------------------------------------------- #
# Spots and links
# --------------------------------------------------------------------------- #


def cropped_area(outline: BaseGeometry, units: gpd.GeoDataFrame | None
                 ) -> tuple[BaseGeometry, str]:
    """The ground the crop stands on, which is what the water has to cover.

    The rows' own footprint where there are detected units, so borders, alleys
    and headlands outside the planting do not pass for high or low spots;
    otherwise the outline less a headland. Returns the area and how it was chosen.
    """
    if units is not None and not units.empty:
        crop = units.geometry.union_all().convex_hull.buffer(1.0)
        area = outline.intersection(crop)
        if not area.is_empty and area.area >= 100.0:
            return area, "the rows' footprint"
    inner = outline.buffer(-HEADLAND_M)
    if not inner.is_empty and inner.area > 0.5 * outline.area:
        return inner, f"the field less a {HEADLAND_M:g} m headland"
    return outline, "the whole outline"


def where(point: BaseGeometry, frame: BaseGeometry) -> str:
    """'north-east corner', 'south edge' or 'middle', within the field's extent."""
    minx, miny, maxx, maxy = frame.bounds
    u = (point.x - minx) / max(maxx - minx, 1e-9)
    v = (point.y - miny) / max(maxy - miny, 1e-9)
    north_south = "north" if v > 2 / 3 else ("south" if v < 1 / 3 else "")
    east_west = "east" if u > 2 / 3 else ("west" if u < 1 / 3 else "")
    if north_south and east_west:
        return f"{north_south}-{east_west} corner"
    if north_south or east_west:
        return f"{north_south or east_west} side"
    return "middle"


def find_spots(ground: Ground, extent: BaseGeometry) -> list[Spot]:
    """Trace connected high and low patches of the smoothed relief."""
    valid = np.isfinite(ground.smooth_cm)
    area_cell = ground.cell_m ** 2
    field_area = valid.sum() * area_cell
    min_area = max(MIN_SPOT_M2, MIN_SPOT_SHARE * field_area)
    spots: list[Spot] = []
    for kind, mask in (("high", ground.smooth_cm >= SPOT_CM),
                       ("low", ground.smooth_cm <= -SPOT_CM)):
        labels, count = ndimage.label(mask & valid)
        for label in range(1, count + 1):
            cells = labels == label
            area = cells.sum() * area_cell
            if area < min_area:
                continue
            values = ground.smooth_cm[cells]
            polygons = [shape(geom) for geom, value in
                        shapes(cells.astype(np.uint8), mask=cells, transform=ground.transform)
                        if value == 1]
            geometry = unary_union(polygons)
            peak = float(values.max() if kind == "high" else values.min())
            spots.append(Spot(label="", kind=kind, area_m2=round(float(area), 1),
                              mean_cm=round(float(values.mean()), 1), peak_cm=round(peak, 1),
                              where=where(geometry.centroid, extent), n_units=0,
                              problem_share=None, geometry=geometry))
    spots.sort(key=lambda s: (s.kind != "high", -s.area_m2))
    counters = {"high": 0, "low": 0}
    for spot in spots:
        counters[spot.kind] += 1
        spot.label = f"{'H' if spot.kind == 'high' else 'L'}{counters[spot.kind]}"
    return spots


def link(problem: np.ndarray, inside: np.ndarray, place: str) -> Link:
    """Fisher's exact test: are flagged pieces commoner ``inside`` than outside?"""
    a = int((problem & inside).sum())
    b = int((~problem & inside).sum())
    c = int((problem & ~inside).sum())
    d = int((~problem & ~inside).sum())
    share_in = a / (a + b) if a + b else 0.0
    share_out = c / (c + d) if c + d else 0.0
    ratio = share_in / share_out if share_out else (math.inf if share_in else 1.0)
    p_value = float(stats.fisher_exact([[a, b], [c, d]], alternative="greater")[1]) \
        if (a + b) and (c + d) else 1.0
    linked = ratio >= LINK_RATIO and p_value < LINK_P and a >= LINK_MIN_PROBLEMS
    return Link(place=place, n_in=a + b, problems_in=a, share_in=round(share_in, 4),
                share_out=round(share_out, 4),
                ratio=round(ratio, 2) if math.isfinite(ratio) else 99.0,
                p_value=round(p_value, 6), linked=bool(linked))


def position_along(points_x: np.ndarray, points_y: np.ndarray, flow: np.ndarray,
                   extent: BaseGeometry) -> np.ndarray:
    """0 at the head of the field, 1 at the tail, along the flow."""
    ring = np.asarray(extent.convex_hull.exterior.coords) if extent.geom_type != "Point" \
        else np.array([[extent.x, extent.y]])
    s_ring = ring @ flow
    s = np.column_stack([points_x, points_y]) @ flow
    return np.clip((s - s_ring.min()) / max(s_ring.max() - s_ring.min(), 1e-9), 0, 1)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def analyse(
    ground: Ground,
    *,
    flight_id: str,
    field_id: str | None,
    extent: BaseGeometry,
    flags: gpd.GeoDataFrame | None,
    method: str | None,
    water_enters: str | None,
    ground_source: str,
    canopy_median_m: float | None = None,
    intake: str | None = None,
) -> TerrainReport:
    """Measure the ground, relate the flags to it, and write the advice.

    Args:
        extent: the field outline (or the flight's footprint) in the DTM's CRS.
        flags: the flag step's output, or None to judge the ground alone.
        method: irrigation method from the field settings, if known.
        water_enters: side the irrigation water comes in from (N, S, E, W).
        ground_source: ``lidar``, ``imported`` or ``photogrammetry``.
        canopy_median_m: median crop height, to warn when the ground under a
            standing crop was only interpolated.
        intake: how fast the soil takes water (from the soil survey), if known.
    """
    notes: list[str] = []
    gradient = np.array([ground.plane[1], ground.plane[2]])     # rises this way, m/m
    slope_pct = float(np.hypot(*gradient)) * 100.0
    downhill = compass(*-gradient) if slope_pct > 0 else "-"

    judged = None
    if flags is not None and not flags.empty:
        judged = flags[flags["flag"].isin(JUDGED_FLAGS)].copy()
    axis = row_axis(judged if judged is not None and not judged.empty else flags)
    flow, flow_source, flow_notes = flow_direction(axis, gradient, water_enters)
    notes.extend(flow_notes)

    along = cross = None
    cross_low = None
    if axis is not None:
        along = abs(float(gradient @ axis)) * 100.0
        perp = np.array([-axis[1], axis[0]])
        cross_rise = float(gradient @ perp)
        cross = abs(cross_rise) * 100.0
        cross_low = compass(*(-perp if cross_rise > 0 else perp))
    elif flow is not None:
        along = abs(float(gradient @ flow)) * 100.0

    valid = np.isfinite(ground.relief_cm)
    relief = ground.relief_cm[valid]
    area_cell = ground.cell_m ** 2
    area = float(valid.sum() * area_cell)
    cut = float(np.clip(relief, 0, None).sum() / 100.0 * area_cell)
    depth, kind = curvature(ground)
    spots = find_spots(ground, extent)

    links: list[Link] = []
    shares: dict[str, dict] = {}
    field_share = None
    n_judged = 0

    def tally(name: str, problem: np.ndarray, where: np.ndarray, label: str) -> None:
        n = int(where.sum())
        shares[name] = {"n": n, "share": round(float(problem[where].mean()), 4) if n else None,
                        "label": label}

    # Rows have a head and a tail; a block watered without rows has a near and far end.
    near, far = (HEAD, TAIL) if axis is not None else (NEAR, FAR)
    if judged is not None and not judged.empty:
        cx = judged.geometry.centroid.x.to_numpy()
        cy = judged.geometry.centroid.y.to_numpy()
        problem = judged["flag"].isin(PROBLEM_FLAGS).to_numpy()
        n_judged = len(judged)
        field_share = round(float(problem.mean()), 4)
        if flow is not None:
            t = position_along(cx, cy, flow, extent)
            links.append(link(problem, t >= 2 / 3, far))
            links.append(link(problem, t < 1 / 3, near))
            tally("head", problem, t < 1 / 3, near.replace(" the", ""))
            tally("middle", problem, (t >= 1 / 3) & (t < 2 / 3), "middle")
            tally("tail", problem, t >= 2 / 3, far.replace(" the", ""))
        off_plane = ground.sample(cx, cy, ground.smooth_cm)
        on_grid = np.isfinite(off_plane)
        if on_grid.sum() >= 30:
            p, h = problem[on_grid], off_plane[on_grid]
            links.append(link(p, h >= UNIT_OFF_PLANE_CM, "high ground"))
            links.append(link(p, h <= -UNIT_OFF_PLANE_CM, "low ground"))
            tally("high ground", p, h >= UNIT_OFF_PLANE_CM, "high ground")
            tally("level ground", p, np.abs(h) < UNIT_OFF_PLANE_CM, "level ground")
            tally("low ground", p, h <= -UNIT_OFF_PLANE_CM, "low ground")
        for spot in spots:
            inside = shapely.contains_xy(spot.geometry, cx, cy)
            spot.n_units = int(inside.sum())
            spot.problem_share = round(float(problem[inside].mean()), 4) if inside.any() else None
            spot.linked = link(problem, inside, spot.label).linked if inside.any() else False
    else:
        notes.append("no crop flags to compare with the ground; a drone flight of the crop, "
                     "run through 'flag', shows whether the stress follows it")

    if flow is None and axis is not None:
        notes.append("the rows are practically level and the water's entry side is not "
                     "set; add water_enters (N, S, E or W) to the field to check the row ends")
    if ground_source == "photogrammetry":
        if depth >= DOME_RELIEF_CM:
            notes.append(f"the ground model curves into a {depth:.0f} cm {kind} across the "
                         "field, which photogrammetry without ground control can produce "
                         "on its own; confirm with GCPs or an RTK drone before leveling")
        if canopy_median_m is not None and canopy_median_m > 0.5:
            notes.append(f"the crop stood {canopy_median_m:.1f} m tall, so the ground under "
                         "it was interpolated; for leveling decisions fly the field bare")

    report = TerrainReport(
        flight_id=flight_id, field_id=field_id, ground_source=ground_source, method=method,
        cell_m=round(ground.cell_m, 3), area_m2=round(area, 1),
        slope_pct=round(slope_pct, 3), downhill=downhill,
        row_bearing_deg=round(bearing(*axis) % 180.0, 1) if axis is not None else None,
        flow=f"{compass(*-flow)} to {compass(*flow)}" if flow is not None else None,
        flow_source=flow_source,
        along_pct=round(along, 3) if along is not None else None,
        cross_pct=round(cross, 3) if cross is not None else None, cross_low_side=cross_low,
        sd_cm=round(float(np.std(relief)), 2),
        relief_cm=round(float(np.percentile(relief, 98) - np.percentile(relief, 2)), 1),
        within_tolerance=round(float((np.abs(relief) <= LEVEL_TOLERANCE_CM).mean()), 3),
        within_5cm=round(float((np.abs(relief) <= 5.0).mean()), 3),
        cut_m3=round(cut, 1),
        cut_yd3_per_acre=round(cut * M3_TO_YD3 / max(area / SQ_M_PER_ACRE, 1e-9), 1),
        curvature_cm=round(depth, 1), curvature_kind=kind,
        spots=spots, links=links, field_problem_share=field_share, n_judged=n_judged,
        advice=[], notes=notes, shares=shares,
    )
    report.advice = advise(report, intake=intake)
    return report


# --------------------------------------------------------------------------- #
# Advice
# --------------------------------------------------------------------------- #

SURFACE_METHODS = (None, "furrow", "flood", "border", "basin")


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def advise(report: TerrainReport, *, intake: str | None = None) -> list[Advice]:
    """Turn the measurements into findings a grower can act on, most urgent first."""
    items: list[Advice] = []
    method = report.method
    surface = method in SURFACE_METHODS
    links = {l.place: l for l in report.links}
    head, tail = (report.flow.split(" to ") if report.flow else (None, None))

    if report.along_pct is not None and surface:
        along = report.along_pct
        has_rows = report.row_bearing_deg is not None
        run = f"from {COMPASS_WORDS.get(head, head)} to {COMPASS_WORDS.get(tail, tail)}" \
            if head else "along the rows"
        if along < LEVEL_SLOPE_PCT:
            items.append(Advice(
                "grade", f"Practically level{' along the rows' if has_rows else ''} "
                f"({along:.2f}%).",
                "Suits level basins or blocked-end furrows as it is. Open-ended furrows "
                "advance slowly on a level field: shorter runs or a bigger stream per furrow.",
                3 if method == "basin" else 2))
        elif along <= FURROW_MAX_PCT:
            items.append(Advice(
                "grade", f"Falls {along:.2f}% {run}.",
                "A good grade for furrow and border irrigation." if method != "basin" else
                "Too much fall for a level basin: water piles up at the low end. Level it, "
                "or run furrows instead.", 3 if method != "basin" else 1))
        elif along <= STEEP_PCT:
            items.append(Advice(
                "grade", f"Falls {along:.2f}% {run}, steeper than ideal for furrows.",
                "Use smaller streams so the heads of the rows do not erode, and catch the "
                "runoff at the tail.", 2))
        else:
            items.append(Advice(
                "grade", f"Falls {along:.2f}% {run}, too steep for surface irrigation "
                "without erosion.", "Drip or sprinkler, or rows on the contour.", 1))

    if report.cross_pct is not None and report.cross_pct >= CROSS_SLOPE_PCT and surface:
        items.append(Advice(
            "cross slope",
            f"Tilts {report.cross_pct:.2f}% across the rows toward the "
            f"{COMPASS_WORDS.get(report.cross_low_side, report.cross_low_side)}.",
            "Water can break over the beds toward the low side; keep the beds high on that "
            "side, or level across the rows at the next land preparation.", 2))

    if report.sd_cm <= WELL_LEVELED_SD_CM:
        items.append(Advice(
            "leveling", f"Evenly graded: {_pct(report.within_tolerance)} of the field lies "
            f"within 3 cm (about 1 in) of a smooth plane.", "No leveling needed.", 3))
    elif report.sd_cm <= UNEVEN_SD_CM:
        items.append(Advice(
            "leveling", f"Mostly even: {_pct(report.within_tolerance)} within 3 cm of a "
            f"smooth plane (spread {report.sd_cm:.1f} cm).",
            "A touch-up with a land plane at the next preparation would even out the water.",
            3 if not surface else 2))
    else:
        items.append(Advice(
            "leveling", f"Uneven: only {_pct(report.within_tolerance)} of the field within "
            f"3 cm of a smooth plane (spread {report.sd_cm:.1f} cm).",
            f"Laser or GPS land leveling would even out the water: about "
            f"{report.cut_m3:,.0f} m3 of soil to move, {report.cut_yd3_per_acre:,.0f} cubic "
            "yards per acre.", 1 if surface else 2))

    share = report.field_problem_share
    for spot in [s for s in report.spots if s.kind == "high"][:3]:
        finding = (f"High spot {spot.label} in the {spot.where}: {spot.area_m2:,.0f} m2 "
                   f"standing up to {spot.peak_cm:.0f} cm above the plane")
        if spot.linked:
            items.append(Advice(
                "high spot", finding + f", and its crop is struggling ({_pct(spot.problem_share)} "
                f"of row pieces flagged against {_pct(share)} across the field).",
                "Water runs around it. Cut it down when leveling, or give it a longer set "
                "until then.", 1))
        else:
            items.append(Advice("high spot", finding + ".",
                                "Water may skirt it; watch it at the next irrigation.", 3))
    for spot in [s for s in report.spots if s.kind == "low"][:3]:
        wet = spot.linked
        items.append(Advice(
            "low spot", f"Low spot {spot.label} in the {spot.where}: {spot.area_m2:,.0f} m2 "
            f"down to {abs(spot.peak_cm):.0f} cm below the plane"
            + (f", with {_pct(spot.problem_share)} of its row pieces flagged." if wet else "."),
            "Water stands here: risk of waterlogging, scald and salt build-up. Fill it when "
            "leveling, or open a drain.", 1 if wet else 2))

    tail_link = links.get(TAIL) or links.get(FAR)
    head_link = links.get(HEAD) or links.get(NEAR)
    rows = tail_link is not None and tail_link.place == TAIL
    if tail_link and tail_link.linked:
        soak = {"fast": " On this fast-soaking soil, shorter runs matter most.",
                "slow": " On this slow-soaking clay, give the far end longer to soak rather "
                        "than a bigger stream, which only runs off."}.get(intake or "", "")
        fix = ("Shorter runs (split the field into two sets), a bigger stream per furrow to "
               "start and then cut back, or surge valves. Rule of thumb: water should reach "
               "the end in the first quarter to half of the set." if rows else
               "Smaller checks or borders, or more water at the inlet, so the far end is "
               "covered before the set is changed.")
        items.append(Advice(
            "row ends" if rows else "far end",
            f"Stress bunches at the {tail_link.place}: {_pct(tail_link.share_in)} of pieces "
            f"flagged in the last third against {_pct(tail_link.share_out)} elsewhere. The "
            "water is not reaching the end.", fix + soak, 1))
    if head_link and head_link.linked:
        items.append(Advice(
            "row ends" if rows else "near end",
            f"Stress bunches at the {head_link.place}: {_pct(head_link.share_in)} of pieces "
            f"flagged in the first third against {_pct(head_link.share_out)} elsewhere.",
            "The inlet end sits wet longest. Cut the stream back once water reaches the end, "
            "and check it for waterlogging or erosion.", 1))
    high_link, low_link = links.get("high ground"), links.get("low ground")
    spot_advised = any(a.topic == "high spot" and a.priority == 1 for a in items)
    if high_link and high_link.linked and not spot_advised:
        items.append(Advice(
            "high ground", f"Flagged pieces sit on high ground: {_pct(high_link.share_in)} "
            f"flagged 3 cm or more above the plane against {_pct(high_link.share_out)} elsewhere.",
            "The water is not climbing onto the high ground; leveling is the lasting fix, "
            "longer sets the stopgap.", 1))
    if low_link and low_link.linked:
        items.append(Advice(
            "low ground", f"Flagged pieces sit in low ground: {_pct(low_link.share_in)} "
            f"flagged 3 cm or more below the plane against {_pct(low_link.share_out)} elsewhere.",
            "Water stands there: shorten the sets or improve drainage, and check for salt.", 1))

    tested = [l for l in report.links]
    if tested and not any(l.linked for l in tested):
        parts = [f"{_pct(l.share_in)} on the {l.place} against {_pct(l.share_out)} elsewhere"
                 for l in tested if l.place in (TAIL, FAR, "high ground")]
        items.append(Advice(
            "cause", "The flagged spots follow neither the ground nor the row ends ("
            + "; ".join(parts) + ").",
            ("Uneven watering is an unlikely cause." if surface else
             "The ground is an unlikely cause.")
            + " Look at pests, disease, nutrients, salt or the stand itself.", 2))
    if not surface:
        items = [_for_unirrigated(item, method) for item in items
                 if not (item.topic == "high spot" and item.priority == 3)]
        items.append(Advice(
            "method",
            "Rainfed: no irrigation water to spread, so the ground matters for drainage only."
            if method == "none" else
            f"Watered by {method}: the grade matters less than for surface irrigation.",
            "Leveling findings above are about drainage and any future furrow use.", 3))
    return sorted(items, key=lambda a: a.priority)


def _for_unirrigated(item: Advice, method: str | None) -> Advice:
    """Reword ground advice for a field the water does not run across."""
    if item.topic == "leveling":
        if item.finding.startswith("Evenly"):
            return item
        return Advice(item.topic, item.finding,
                      "Not a watering problem here, but low ground can pond after heavy rain; "
                      "smoothing it at the next preparation helps drainage.",
                      2 if item.finding.startswith("Uneven") else 3)
    if item.topic == "high spot":
        return Advice(item.topic, item.finding,
                      "High ground dries out first in a dry spell; smoothing it at the next "
                      "preparation evens out the crop.", item.priority)
    if item.topic == "low spot":
        return Advice(item.topic, item.finding,
                      "Water can stand here after heavy rain: waterlogging and salt build-up. "
                      "Fill it or open a drain.", item.priority)
    return item


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


#: The same four, in words, for anything a person reads.
GROUND_WORDS = {
    "3dep": "the public 3DEP laser survey",
    "lidar": "this flight's laser",
    "imported": "imported maps",
    "photogrammetry": "this flight's photographs",
}


def ground_source(project_dir: Path) -> str:
    """Where the ground under this flight came from.

    ``3dep`` for the government's airborne laser, which we fetch ourselves and
    nobody flew for us; ``lidar`` for a point cloud the flight brought with it,
    which somebody did; ``imported`` for other maps made elsewhere;
    ``photogrammetry`` for ODM's own. The first two are both lasers and are
    still worth telling apart: a grower reading the page should know whether
    the ground map is of their own flight or of a public survey from years ago.
    """
    provenance = Path(project_dir) / "imported.json"
    if not provenance.exists():
        return "photogrammetry"
    try:
        record = json.loads(provenance.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "imported"
    if record.get("lidar"):
        return "3dep"
    dtm = next((p for p in record.get("products", []) if p.get("name") == "dtm"), {})
    return "lidar" if "points" in str(dtm.get("how", "")) else "imported"


def write_relief(ground: Ground, path: Path) -> Path:
    """The ground above or below its plane, in cm, as a GeoTIFF for a leveling contractor."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=ground.z.shape[0], width=ground.z.shape[1],
        count=1, dtype="float32", crs=ground.crs, transform=ground.transform,
        nodata=np.nan, compress="deflate",
    ) as dataset:
        dataset.write(ground.relief_cm.astype("float32"), 1)
    return path


def write_spots(report: TerrainReport, crs, path: Path) -> Path | None:
    """High and low spots as polygons, for any GIS."""
    if not report.spots:
        return None
    frame = gpd.GeoDataFrame(
        [s.to_dict() for s in report.spots],
        geometry=[s.geometry for s in report.spots], crs=crs,
    )
    frame.to_file(path, driver="GeoJSON")
    return Path(path)
