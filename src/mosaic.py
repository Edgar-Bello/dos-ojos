"""Many thermal frames into one map of canopy temperature.

A radiometric thermal camera does not write a map. It writes hundreds or
thousands of small pictures, each a metre or two across, each already carrying
its own corner coordinates from the aircraft or the gantry that held it. Nothing
here has to match one picture to the next the way ``odm`` matches colour photos:
every pixel already knows where it is. The work is deciding which pixels are
leaves, undoing the sun's climb while the camera worked, and averaging what is
left onto one grid.

Both of those, left alone, read as crop stress:

- **Soil.** Sunlit ground runs five to twenty degrees above any leaf, so a map
  that includes it is a map of the furrows. The colour half tells leaves from
  soil with a canopy height model, and where a flight has one this module is not
  needed. Where it has none - a thermal camera flown on its own - a leaf can
  still be told by contrast: a leaf that is drinking sits several degrees below
  the ground immediately around it. Shade alone does not: bare ground in a
  plant's shadow is a degree or two cooler, not five.
- **Time.** A scan takes minutes from a drone and hours from a field scanner,
  and the sun climbs throughout. On the Maricopa sorghum scan this was built
  against, the ground warmed about 9 C and the crop about 6 C between the first
  frame and the last. Uncorrected, whichever half was scanned last is one
  enormous warm patch. So each cell is moved onto the temperature the crop held
  in the middle of the scan, along a trend read from the scan itself: the median
  leaf temperature of the frames taken within a few minutes either side.

What that correction costs is worth saying plainly, because it is the price of a
slow scan and no amount of care removes it: a warm patch that fills most of what
the camera saw within the trend window is partly explained away as "the time of
day". Patches smaller than that survive it; a whole field running hot does not,
and would have to be read from the checkbook instead.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field as dc_field
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform_bounds

from .thermal import ThermalError, to_celsius

log = logging.getLogger(__name__)

#: How much cooler than the ground around it a pixel must be to count as a leaf.
#: Bare ground in a plant's shadow is about 1 to 2 C cooler than ground in the
#: sun; a transpiring leaf is 4 to 12 C cooler. Three sits between the two.
#: The cost of the rule is at the other end: a plant so far gone that it has
#: stopped cooling itself reads like soil and is left out, which is exactly the
#: plant worth finding. That is why a canopy height model wins whenever a flight
#: has one, and why the report says so when it had to fall back to this.
LEAF_COLDER_C = 3.0

#: The ground "around it" is measured over a square this wide. Wide enough to
#: hold soil even where a plant sits in the middle of it, narrow enough that the
#: shade, the wet patch under a dripper and the sunlit furrow are not averaged
#: into one number.
GROUND_WINDOW_M = 0.20

#: ...and taken as this percentile of it, not the mean, so the plants standing
#: in the window do not drag the ground reading down towards themselves.
GROUND_PERCENTILE = 75.0

#: Output cell. Five centimetres is finer than any patch a person walks out to,
#: and coarse enough that a cell holds hundreds of camera pixels to average.
DEFAULT_CELL_M = 0.05

#: Share of each frame's edge thrown away before it is placed, for a camera
#: whose rim is not to be trusted. Off by default, on measurement rather than
#: principle: the outer tenth of the Maricopa frames does read 1.5 to 2.5 C
#: high, but trimming it made the map worse, not better. Frames have to overlap
#: to be lined up against each other, and trimming eats overlap first - there,
#: it broke the scan into more groups with nothing shared between them, which
#: costs more than the rim does. Try it on a camera with a bad edge and a
#: generous flight plan; measure before keeping it.
TRIM_EDGE = 0.0

#: A cell with fewer leaf pixels than this holds no temperature worth writing:
#: a few stray pixels at the edge of a leaf are half soil.
MIN_LEAF_SAMPLES = 25

#: Half the width of the window the time trend is read over. Seven minutes
#: either side covers several passes of a scanner and most of a drone flight.
DRIFT_HALF_WINDOW_S = 420.0

#: The camera does not see its own picture evenly. Part of it is the lens, and
#: part is the machine carrying the camera: a drone's leg or a scanner's beam
#: throws a bar of shade that sits still in the picture while the ground moves
#: underneath, and creeps across it as the sun climbs. On the Maricopa scan this
#: was worth up to 2.5 C between one edge of a frame and the middle, which is
#: more than any patch worth finding. It is measured from the scan itself, over
#: slices of time this long, and taken out.
#:
#: Five minutes, because the bar moves: measured on that scan, as a share of
#: everything that varies north to south, the structure locked to the scanner's
#: own 1.05 m step came to 16% with no correction at all, 10% with one pattern
#: for the whole scan, 4% over twenty-minute slices and 2.4% over five-minute
#: ones. Shorter slices than that start subtracting the field from itself,
#: since each one then averages too few passes over different ground.
FLAT_FIELD_BIN_S = 300.0

#: Frames read to measure it. Each carries tens of thousands of leaf pixels, so
#: a few hundred frames give every row and column of the picture thousands of
#: readings; more would cost time and change nothing.
FLAT_FIELD_FRAMES = 480

#: The measured pattern is smoothed over this many pixels before it is taken
#: out, so that leaf noise is not subtracted from the leaves.
FLAT_FIELD_SMOOTH_PX = 25

#: Rounds of lining the frames up against each other. The move shrinks by about
#: half a round; six is well past where the map stops changing.
LEVEL_ROUNDS = 6

#: How firmly the frames are held to the smooth warming trend while they are
#: lined up. Light: the trend says where the whole set sits, the overlaps say
#: everything else.
LEVEL_ANCHOR = 0.1

#: A grid larger than this is refused rather than allocated: at 5 cm it is 250
#: hectares, far beyond any thermal flight, and it is nearly always a sign that
#: one frame's coordinates are wrong.
MAX_CELLS = 100_000_000

FRAME_SUFFIXES = (".tif", ".tiff")

#: TERRA-REF and most cameras write the capture time in the file name as
#: 2018-05-20__09-22-56-936; GeoTIFF tags are checked first all the same.
_NAME_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[_ T]+(\d{2})[-:](\d{2})[-:](\d{2})")


class MosaicError(RuntimeError):
    """Raised when a set of thermal frames cannot be made into one map."""


# --------------------------------------------------------------------------- #
# The frames
# --------------------------------------------------------------------------- #


def find_frames(folder: Path) -> list[Path]:
    """Every thermal frame under ``folder``, in the order they were taken.

    Cameras and scanners write one folder per capture as often as one flat
    folder, so this looks all the way down. Sorting by name puts them in time
    order for every naming scheme seen so far, and the capture time itself is
    read per frame anyway.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise MosaicError(f"{folder} is not a folder of thermal frames")
    found = sorted(
        path for path in folder.rglob("*")
        if path.suffix.lower() in FRAME_SUFFIXES and path.is_file()
    )
    if not found:
        raise MosaicError(
            f"no {' or '.join(FRAME_SUFFIXES)} frames under {folder}. A thermal scan arrives "
            "as georeferenced frames; a folder of ordinary photos is a job for 'odm'."
        )
    return found


def frame_time(path: Path, tags: dict | None = None) -> datetime | None:
    """When a frame was taken, from its GeoTIFF tags or failing that its name."""
    for key in ("datetime", "DateTime", "TIFFTAG_DATETIME", "ACQUISITIONDATETIME"):
        raw = (tags or {}).get(key)
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                try:                                    # TIFF's own 2018:05:20 09:22:56
                    return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
    match = _NAME_TIME.search(path.name)
    if match:
        year, month, day, hour, minute, second = (int(part) for part in match.groups())
        return datetime(year, month, day, hour, minute, second)
    return None


@dataclass(frozen=True)
class Frame:
    """One thermal picture: where it landed, when it was taken, how it read."""

    path: Path
    when: datetime | None
    #: Bounds in the mosaic's own CRS, west, south, east, north.
    bounds: tuple[float, float, float, float]
    shape: tuple[int, int]
    #: Median temperature of the leaves in it, and how much of it was leaf.
    leaf_median_c: float | None = None
    leaf_share: float = 0.0
    ground_median_c: float | None = None


# --------------------------------------------------------------------------- #
# Leaves, by contrast with the ground around them
# --------------------------------------------------------------------------- #


def ground_around(celsius: np.ndarray, block_px: int,
                  percentile: float = GROUND_PERCENTILE) -> np.ndarray:
    """The temperature of the ground around every pixel.

    A high percentile over a coarse grid of blocks, smoothed back up to full
    size. Coarse on purpose: the point is the soil a plant is sitting on, not
    the plant.
    """
    from scipy import ndimage

    block = max(1, int(block_px))
    height, width = celsius.shape
    pad_y, pad_x = (-height) % block, (-width) % block
    padded = np.pad(celsius, ((0, pad_y), (0, pad_x)), mode="edge")
    rows, columns = padded.shape[0] // block, padded.shape[1] // block
    tiles = padded.reshape(rows, block, columns, block).transpose(0, 2, 1, 3)
    coarse = np.percentile(tiles.reshape(rows, columns, -1), percentile, axis=2)
    if coarse.shape == padded.shape:
        grown = coarse
    else:
        grown = ndimage.zoom(coarse, (padded.shape[0] / rows, padded.shape[1] / columns),
                             order=1, grid_mode=True, mode="nearest")
    return grown[:height, :width]


def leaves_of(celsius: np.ndarray, *, pixel_m: float, colder_c: float = LEAF_COLDER_C,
              window_m: float = GROUND_WINDOW_M) -> np.ndarray:
    """Which pixels are leaves, told from soil by contrast alone.

    Returns a boolean mask. Everything false is soil, shade, plastic, a rail or
    a rock, none of which transpires and all of which would swamp a canopy
    statistic.

    Pixels on the rim of a leaf are part leaf and part the soil behind it, and
    are kept all the same: peeling them off was measured on the Maricopa scan
    and cost a quarter of the leaf area to move the reading by 0.2 C. What that
    bias really is, and what is done about it, is a share-of-the-cell rule one
    step later - see ``thermal.MIN_LEAF_FRACTION``.
    """
    if pixel_m <= 0:
        raise MosaicError("a frame with no pixel size cannot be measured")
    block_px = max(3, int(round(window_m / pixel_m)))
    return (ground_around(celsius, block_px) - celsius) >= colder_c


# --------------------------------------------------------------------------- #
# The sun climbing while the camera worked
# --------------------------------------------------------------------------- #


def drift_trend(seconds: np.ndarray, values: np.ndarray,
                half_window_s: float = DRIFT_HALF_WINDOW_S) -> np.ndarray:
    """The crop's temperature as a smooth function of the time it was measured.

    A running median, not a fitted curve: cloud passing over a scan puts a step
    in the warming, and a median rides that where a polynomial would swing.
    """
    seconds = np.asarray(seconds, dtype=float)
    values = np.asarray(values, dtype=float)
    trend = np.empty_like(values)
    for i, moment in enumerate(seconds):
        near = np.abs(seconds - moment) <= half_window_s
        trend[i] = np.median(values[near]) if near.any() else values[i]
    return trend


# --------------------------------------------------------------------------- #
# The mosaic
# --------------------------------------------------------------------------- #


@dataclass
class Scan:
    """What one set of thermal frames came to, and how it was read."""

    frames: int
    frames_skipped: int
    unit: str
    cell_m: float
    crs: str
    bounds: tuple[float, float, float, float]
    shape: tuple[int, int]
    started: str | None
    ended: str | None
    minutes: float | None
    #: How far the crop's own temperature drifted between the first frame and
    #: the last, and what was taken out of the map because of it.
    drift_c: float | None
    drift_window_s: float
    #: How far the frames had to be moved to agree with each other, typical and
    #: worst, once the warming trend was allowed for.
    levelled_c: float | None
    levelled_worst_c: float | None
    #: The worst of the camera's own uneven view, taken out before any of that.
    camera_pattern_c: float | None
    leaf_rule_c: float
    #: Ground the camera covered, ground that came back with a canopy
    #: temperature on it, and the leaf area inside that: three different things.
    ground_m2: float
    measured_m2: float
    leaf_m2: float
    canopy_median_c: float | None
    #: The crop's temperature minute by minute through the scan, which is what
    #: the correction above was read from. Kept so anyone can see it.
    warming: list[dict] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _utm_epsg(lon: float, lat: float) -> int:
    """The UTM zone a point falls in, as an EPSG code on WGS84."""
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _target_crs(dataset) -> object:
    """The CRS the mosaic is written in: metric, because every later step measures.

    A frame already in a projected CRS keeps it. Frames in degrees - which is how
    most airborne and gantry thermal arrives - are moved to their own UTM zone,
    so that a cell is a square of ground and an area is in square metres.
    """
    from rasterio.crs import CRS

    if dataset.crs is None:
        raise MosaicError(
            f"{Path(dataset.name).name} carries no CRS, so nothing can be said about where "
            "it is. A thermal frame has to arrive georeferenced."
        )
    if not dataset.crs.is_geographic:
        return dataset.crs
    west, south, east, north = dataset.bounds
    return CRS.from_epsg(_utm_epsg((west + east) / 2, (south + north) / 2))


def _trimmed(bounds, shape, trim: float):
    """The bounds a frame keeps once its unreliable rim is thrown away."""
    rows, columns = shape
    cut_r, cut_c = int(rows * trim), int(columns * trim)
    if trim <= 0 or rows - 2 * cut_r < 8 or columns - 2 * cut_c < 8:
        return bounds
    west, south, east, north = bounds
    pixel_x, pixel_y = (east - west) / columns, (north - south) / rows
    return (west + cut_c * pixel_x, south + cut_r * pixel_y,
            east - cut_c * pixel_x, north - cut_r * pixel_y)


def _read(path: Path, target_crs, trim: float = 0.0
          ) -> tuple[np.ndarray, tuple[float, float, float, float], dict]:
    """One frame as Celsius, with its bounds in the mosaic's CRS, edges trimmed."""
    with rasterio.open(path) as dataset:
        if dataset.transform.b or dataset.transform.d:
            raise MosaicError(f"{path.name} is a rotated frame, which this cannot place")
        data = dataset.read(1, masked=True).astype(np.float64)
        tags = dataset.tags()
        bounds = transform_bounds(dataset.crs, target_crs, *dataset.bounds)
    array = np.ma.filled(data, np.nan)
    if trim > 0:
        rows, columns = array.shape
        cut_r, cut_c = int(rows * trim), int(columns * trim)
        if rows - 2 * cut_r >= 8 and columns - 2 * cut_c >= 8:
            west, south, east, north = bounds
            pixel_x, pixel_y = (east - west) / columns, (north - south) / rows
            array = array[cut_r:rows - cut_r, cut_c:columns - cut_c]
            bounds = (west + cut_c * pixel_x, south + cut_r * pixel_y,
                      east - cut_c * pixel_x, north - cut_r * pixel_y)
    celsius, unit = to_celsius(array)
    return celsius, bounds, {"tags": tags, "unit": unit}


@dataclass
class FlatField:
    """How unevenly the camera saw its own picture, and how that changed.

    Held as one profile down the picture and one across it, per slice of time.
    Two profiles rather than a full image because the pattern is a bar of shade
    and a lens falling off at the edges, both of which run one way; and per
    slice of time because the bar moves with the sun.
    """

    #: Middle of each time slice, in seconds from the first frame.
    seconds: np.ndarray
    #: (slices, rows) and (slices, columns), each already centred on zero.
    down: np.ndarray
    across: np.ndarray

    @property
    def worst_c(self) -> float:
        """The largest correction it makes anywhere, for the record."""
        if not self.seconds.size:
            return 0.0
        return float(np.abs(self.down).max() + np.abs(self.across).max())

    def correction(self, seconds: float, shape: tuple[int, int]) -> np.ndarray:
        """The pattern to take off a frame taken at ``seconds``."""
        if not self.seconds.size or self.down.shape[1:] != (shape[0],):
            return np.zeros(shape)
        down = np.array([np.interp(seconds, self.seconds, self.down[:, i])
                         for i in range(shape[0])])
        across = np.array([np.interp(seconds, self.seconds, self.across[:, i])
                           for i in range(shape[1])])
        return down[:, None] + across[None, :]


def _smooth(profile: np.ndarray, window: int) -> np.ndarray:
    """A moving average that keeps the ends where they are."""
    window = max(1, int(window) | 1)
    padded = np.pad(profile, window // 2, mode="edge")
    smoothed = np.convolve(padded, np.ones(window) / window, mode="valid")
    return smoothed - np.median(smoothed)


def flat_field(paths, *, colder_c: float = LEAF_COLDER_C, bin_s: float = FLAT_FIELD_BIN_S,
               frames: int = FLAT_FIELD_FRAMES, smooth_px: int = FLAT_FIELD_SMOOTH_PX,
               window_m: float = GROUND_WINDOW_M) -> FlatField:
    """Measure the camera's uneven view from the scan itself.

    Every frame is compared against its own middle, and the comparisons are
    averaged over all the frames in a slice of time. Real crop cannot survive
    that averaging: a hot patch sits still on the ground while the camera moves
    over it, so it lands on a different part of the picture in every frame that
    sees it. Anything that stays in the same part of every picture is the
    camera, or what is bolted next to it.
    """
    paths = list(paths)
    step = max(1, len(paths) // max(1, frames))
    sample = paths[::step]
    times: list[float] = []
    down: list[np.ndarray] = []
    across: list[np.ndarray] = []
    shape = None
    zero = None
    for path in sample:
        try:
            with rasterio.open(path) as dataset:
                data = np.ma.filled(dataset.read(1, masked=True).astype(np.float64), np.nan)
                when = frame_time(path, dataset.tags())
                pixel_m = (dataset.bounds[3] - dataset.bounds[1]) / dataset.height
                if dataset.crs is not None and dataset.crs.is_geographic:
                    pixel_m *= 111_320.0
            celsius, _ = to_celsius(data)
        except (ThermalError, MosaicError, OSError, ValueError):
            continue
        if when is None:
            continue
        zero = when if zero is None else min(zero, when)
        shape = shape or celsius.shape
        if celsius.shape != shape:
            continue
        leaf = leaves_of(celsius, pixel_m=pixel_m, colder_c=colder_c,
                         window_m=window_m) & np.isfinite(celsius)
        if leaf.sum() < 500:
            continue
        residual = np.where(leaf, celsius - np.median(celsius[leaf]), 0.0)
        times.append(when.timestamp())
        # Rows and columns with no leaf in them contribute nothing rather than
        # NaN: at the edge of a frame there may be no leaf at all.
        for axis, keep in ((1, down), (0, across)):
            total, count = residual.sum(axis=axis), leaf.sum(axis=axis)
            keep.append(np.divide(total, count, out=np.zeros_like(total), where=count > 0))
    if len(times) < 4 or shape is None:
        return FlatField(np.empty(0), np.empty((0, 0)), np.empty((0, 0)))

    seconds = np.array(times) - min(times)
    order = np.argsort(seconds)
    seconds = seconds[order]
    down_a = np.nan_to_num(np.array(down)[order])
    across_a = np.nan_to_num(np.array(across)[order])
    edges = np.arange(0, seconds.max() + bin_s, bin_s)
    middles, profiles_down, profiles_across = [], [], []
    for start in edges:
        inside = (seconds >= start) & (seconds < start + bin_s)
        if inside.sum() < 3:
            continue
        middles.append(float(np.median(seconds[inside])))
        profiles_down.append(_smooth(down_a[inside].mean(axis=0), smooth_px))
        profiles_across.append(_smooth(across_a[inside].mean(axis=0), smooth_px))
    if not middles:
        return FlatField(np.empty(0), np.empty((0, 0)), np.empty((0, 0)))
    field = FlatField(np.array(middles), np.array(profiles_down), np.array(profiles_across))
    log.info("the camera's own pattern: up to %.1f C, measured over %d slices of %.0f s",
             field.worst_c, len(middles), bin_s)
    return field


def _level(shares, prior: np.ndarray, cells: int, rounds: int = LEVEL_ROUNDS,
           anchor: float = LEVEL_ANCHOR) -> np.ndarray:
    """One correction per frame, so that frames agree where they overlap.

    An uncooled thermal camera's reading wanders by a degree or two as it works
    - it re-zeroes itself every few minutes, and the housing warms - and the
    ground underneath warms all morning besides. Both show up the same way: two
    frames over the same square of ground disagree about its temperature.

    This solves for the one number per frame that makes those disagreements as
    small as possible, alternating between the map implied by the current
    corrections and the corrections implied by that map. Only disagreement over
    *the same ground* moves a frame, so a patch of crop that really is hot is
    left alone: every frame that sees it sees it hot, and they agree.

    ``prior`` holds the smooth warming trend, which anchors the answer: without
    it the whole set could slide together, since nothing here measures absolute
    temperature, only differences. It does more than that where a scan does not
    quite join up - a scanner that steps further than its own field of view
    every third pass leaves slivers of ground no two passes share - and the
    groups either side of such a sliver have nothing to line up against. Each
    group is therefore held to the trend on its own; without that they float
    apart and the map comes out in stripes.
    """
    offsets = prior.astype(float).copy()
    weights = np.array([float(counts.sum()) for _, counts, _ in shares])
    if not weights.any():
        return offsets
    groups = _groups(shares)
    if groups.max() > 0:
        log.info("the frames fall into %d groups that never see the same ground; each is "
                 "held to the warming trend on its own", groups.max() + 1)
    for _ in range(rounds):
        total = np.zeros(cells)
        seen = np.zeros(cells)
        for (cell, counts, sums), offset in zip(shares, offsets):
            if cell.size:
                np.add.at(total, cell, sums - offset * counts)
                np.add.at(seen, cell, counts)
        field = np.divide(total, seen, out=np.zeros(cells), where=seen > 0)
        for i, (cell, counts, sums) in enumerate(shares):
            if not cell.size:
                continue
            gap = float(np.sum(sums - counts * field[cell])) / weights[i]
            offsets[i] = (gap + anchor * prior[i]) / (1.0 + anchor)
        # Nothing here knows absolute temperature, so hold each group where the
        # warming trend put it rather than letting it drift as a whole.
        for group in range(groups.max() + 1):
            member = groups == group
            if member.any():
                offsets[member] -= np.average(offsets[member] - prior[member],
                                              weights=np.maximum(weights[member], 1e-9))
    return offsets


def _groups(shares) -> np.ndarray:
    """Which frames are tied to which: sets of frames joined by shared ground.

    Two frames that never cover the same cell say nothing about each other's
    reading, however close together they were taken.
    """
    parent = list(range(len(shares)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells = np.concatenate([c for c, _, _ in shares]) if shares else np.empty(0, dtype=np.int64)
    owner = np.concatenate([np.full(c.size, i, dtype=np.int64)
                            for i, (c, _, _) in enumerate(shares)]) if shares else cells
    order = np.argsort(cells, kind="stable")
    cells, owner = cells[order], owner[order]
    same = np.nonzero(cells[1:] == cells[:-1])[0]
    for i in same:
        a, b = root(int(owner[i])), root(int(owner[i + 1]))
        if a != b:
            parent[a] = b
    labels = np.array([root(i) for i in range(len(shares))])
    _, packed = np.unique(labels, return_inverse=True)
    return packed


def build(paths, out_dir: Path, *, cell_m: float = DEFAULT_CELL_M,
          colder_c: float = LEAF_COLDER_C, half_window_s: float = DRIFT_HALF_WINDOW_S,
          min_samples: int = MIN_LEAF_SAMPLES, trim: float = TRIM_EDGE
          ) -> tuple[Scan, Path, Path]:
    """Average a scan's frames into one canopy-temperature map and a leaf map.

    Writes ``thermal.tif`` - leaves only, in Celsius, moved onto the middle of
    the scan - and ``leaves.tif``, the share of each cell the camera saw leaf
    in, which is what later steps use in place of a canopy height model.

    Raises:
        MosaicError: if the frames disagree about where they are, hold no
            temperatures, or would make an impossible grid.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        raise MosaicError("no thermal frames to build from")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with rasterio.open(paths[0]) as first:
        target_crs = _target_crs(first)

    # Pass one is only the corners: the grid has to exist before anything lands on it.
    frames: list[Frame] = []
    skipped: list[str] = []
    for path in paths:
        try:
            with rasterio.open(path) as dataset:
                if dataset.crs is None:
                    _target_crs(dataset)                # raises, saying which file
                if dataset.transform.b or dataset.transform.d:
                    raise MosaicError(
                        f"{path.name} is a rotated frame. Thermal frames are placed by their "
                        "corners, not matched to each other, so a rotated one cannot be put "
                        "down without resampling it first."
                    )
                bounds = _trimmed(transform_bounds(dataset.crs, target_crs, *dataset.bounds),
                                  (dataset.height, dataset.width), trim)
                frames.append(Frame(path=path, when=frame_time(path, dataset.tags()),
                                    bounds=bounds, shape=(dataset.height, dataset.width)))
        except MosaicError:
            raise
        except Exception as exc:                        # noqa: BLE001 - one bad frame, not the scan
            log.warning("%s could not be read (%s); leaving it out", path.name, exc)
            skipped.append(path.name)
    if not frames:
        raise MosaicError("not one frame could be read")

    west = min(f.bounds[0] for f in frames)
    south = min(f.bounds[1] for f in frames)
    east = max(f.bounds[2] for f in frames)
    north = max(f.bounds[3] for f in frames)
    width = int(np.ceil((east - west) / cell_m))
    height = int(np.ceil((north - south) / cell_m))
    if width < 1 or height < 1:
        raise MosaicError("the frames cover less ground than one cell")
    if width * height > MAX_CELLS:
        raise MosaicError(
            f"a {cell_m:g} m grid over these frames would be {width} by {height} cells. "
            "Either one frame's coordinates are wrong, or --cell is far too fine."
        )
    log.info("%d frames over %.0f by %.0f m, at %g m cells: %d by %d",
             len(frames), east - west, north - south, cell_m, width, height)

    n_all = np.zeros(height * width, dtype=np.int64)
    zero = min((f.when for f in frames if f.when is not None), default=None)
    flat = flat_field([f.path for f in frames], colder_c=colder_c)
    unit = ""
    placed: list[Frame] = []
    #: What each frame contributed, cell by cell: which cells, how many leaf
    #: pixels, and their sum. Kept rather than added up straight away, so the
    #: frames can be lined up against each other below without reading gigabytes
    #: a second time. A frame covers a few hundred cells, so this is megabytes.
    shares: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    for frame in frames:
        try:
            celsius, bounds, meta = _read(frame.path, target_crs, trim)
        except (ThermalError, MosaicError, OSError) as exc:
            log.warning("%s: %s; leaving it out", frame.path.name, exc)
            skipped.append(frame.path.name)
            continue
        unit = unit or meta["unit"]
        rows, columns = celsius.shape
        pixel_m = (bounds[2] - bounds[0]) / columns
        # The mask is taken on what the camera wrote, since it works on contrast
        # between neighbours and the pattern below is far wider than that; the
        # temperatures that go on the map are the corrected ones.
        leaf = leaves_of(celsius, pixel_m=pixel_m, colder_c=colder_c) & np.isfinite(celsius)
        moment = 0.0 if (zero is None or frame.when is None) else (frame.when - zero).total_seconds()
        celsius = celsius - flat.correction(moment, celsius.shape)

        # Where every pixel of this frame lands on the mosaic. Both grids are
        # north-up in the same CRS, so this is arithmetic, not resampling.
        xs = bounds[0] + (np.arange(columns) + 0.5) * (bounds[2] - bounds[0]) / columns
        ys = bounds[3] - (np.arange(rows) + 0.5) * (bounds[3] - bounds[1]) / rows
        cols = np.clip(((xs - west) / cell_m).astype(np.int64), 0, width - 1)
        lines = np.clip(((north - ys) / cell_m).astype(np.int64), 0, height - 1)
        c0, c1 = int(cols.min()), int(cols.max()) + 1
        r0, r1 = int(lines.min()), int(lines.max()) + 1
        local_w, local_h = c1 - c0, r1 - r0
        index = (lines - r0)[:, None] * local_w + (cols - c0)[None, :]
        size = local_h * local_w

        good = np.isfinite(celsius)
        here = np.bincount(index[good], minlength=size)
        seen = np.nonzero(here)[0]
        np.add.at(n_all, (r0 + seen // local_w) * width + (c0 + seen % local_w), here[seen])
        if leaf.any():
            counts = np.bincount(index[leaf], minlength=size)
            sums = np.bincount(index[leaf], weights=celsius[leaf], minlength=size)
            hit = np.nonzero(counts)[0]
            cells = (r0 + hit // local_w) * width + (c0 + hit % local_w)
            shares.append((cells, counts[hit].astype(np.int32), sums[hit]))
            frame = Frame(frame.path, frame.when, bounds, frame.shape,
                          leaf_median_c=float(np.median(celsius[leaf])),
                          leaf_share=float(leaf.mean()),
                          ground_median_c=float(np.median(celsius[good & ~leaf]))
                          if (good & ~leaf).any() else None)
        else:
            shares.append((np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int32),
                           np.empty(0, dtype=float)))
        placed.append(frame)

    if not placed:
        raise MosaicError("no frame could be placed on the mosaic")

    # The sun's climb: the crop's own temperature, read as a smooth function of
    # the moment each frame was taken. This alone is not enough - see below -
    # but it says what the map's temperatures mean, which is the middle of the scan.
    notes: list[str] = []
    drift_c = None
    warming: list[dict] = []
    seconds = np.array([0.0 if (zero is None or f.when is None)
                        else (f.when - zero).total_seconds() for f in placed])
    prior = np.zeros(len(placed))
    timed = np.array([f.when is not None and f.leaf_median_c is not None for f in placed])
    if timed.sum() >= 3 and zero is not None:
        medians = np.array([f.leaf_median_c if f.leaf_median_c is not None else np.nan
                            for f in placed])
        order = np.argsort(seconds[timed])
        moments = seconds[timed][order]
        trend = drift_trend(moments, medians[timed][order], half_window_s)
        middle = float(np.interp(0.5 * (moments[0] + moments[-1]), moments, trend))
        prior = np.interp(seconds, moments, trend) - middle
        drift_c = float(trend.max() - trend.min())
        minute = np.round(moments / 60).astype(int)
        warming = [{"minute": int(m), "canopy_c": round(float(np.median(trend[minute == m])), 2)}
                   for m in np.unique(minute)]
    elif len(placed) > 1:
        notes.append("the frames carry no capture times, so the sun's climb during the scan "
                     "could not be read; a scan of more than a few minutes will read warm "
                     "wherever it finished")

    offsets = _level(shares, prior, height * width)
    levelled = offsets - prior
    if drift_c is not None:
        notes.append(
            f"the crop warmed {drift_c:.1f} C between the first frame and the last, over "
            f"{seconds.max() / 60:.0f} minutes, and the camera's own reading wanders as it "
            "works. Both are taken out by lining every frame up with the frames it overlaps, "
            f"a {np.median(np.abs(levelled)):.1f} C move for the middling frame and "
            f"{np.abs(levelled).max():.1f} C for the worst. What that costs: anything that ran "
            "hot over a stretch wider than the camera's own view is levelled away with it, so "
            "a whole field running hot has to be read from the checkbook, not from here."
        )

    sum_c = np.zeros(height * width)
    n_leaf = np.zeros(height * width, dtype=np.int64)
    for (cells, counts, sums), offset in zip(shares, offsets):
        if cells.size:
            np.add.at(sum_c, cells, sums - offset * counts)
            np.add.at(n_leaf, cells, counts)

    enough = n_leaf >= min_samples
    if not enough.any():
        raise MosaicError(
            f"no cell holds {min_samples} leaf pixels. Either the field was bare when this was "
            f"flown, or nothing in it is {colder_c:g} C cooler than the ground around it, which "
            "is what a leaf that is drinking looks like."
        )
    canopy = np.where(enough, sum_c / np.maximum(n_leaf, 1), np.nan).reshape(height, width)
    fraction = np.where(n_all > 0, n_leaf / np.maximum(n_all, 1), np.nan).reshape(height, width)
    transform = rasterio.transform.from_origin(west, north, cell_m, cell_m)
    thermal_path = _write(out_dir / "thermal.tif", canopy, transform, target_crs)
    leaves_path = _write(out_dir / "leaves.tif", fraction, transform, target_crs)

    cell_area = cell_m ** 2
    times = [f.when for f in placed if f.when is not None]
    scan = Scan(
        frames=len(placed), frames_skipped=len(skipped), unit=unit or "celsius",
        cell_m=cell_m, crs=str(target_crs), bounds=(west, south, east, north),
        shape=(height, width),
        started=min(times).isoformat() if times else None,
        ended=max(times).isoformat() if times else None,
        minutes=round((max(times) - min(times)).total_seconds() / 60, 1) if times else None,
        drift_c=None if drift_c is None else round(drift_c, 2),
        drift_window_s=half_window_s,
        levelled_c=round(float(np.median(np.abs(levelled))), 2) if levelled.size else None,
        levelled_worst_c=round(float(np.abs(levelled).max()), 2) if levelled.size else None,
        camera_pattern_c=round(flat.worst_c, 2) if flat.seconds.size else None,
        leaf_rule_c=colder_c,
        ground_m2=round(float((n_all > 0).sum()) * cell_area, 1),
        measured_m2=round(float(np.isfinite(canopy).sum()) * cell_area, 1),
        leaf_m2=round(float(np.nansum(fraction)) * cell_area, 1),
        canopy_median_c=round(float(np.nanmedian(canopy)), 2) if np.isfinite(canopy).any() else None,
        warming=warming, notes=notes,
    )
    log.info("mosaic: %.0f m2 of leaf on %.0f m2 of ground, %.0f m2 of it with a temperature, "
             "canopy median %.1f C", scan.leaf_m2, scan.ground_m2, scan.measured_m2,
             scan.canopy_median_c or float("nan"))
    return scan, thermal_path, leaves_path


def _write(path: Path, data: np.ndarray, transform, crs) -> Path:
    """One band of float32 with NaN for nodata, the way every later step reads."""
    with rasterio.open(
        path, "w", driver="GTiff", height=data.shape[0], width=data.shape[1], count=1,
        dtype="float32", crs=crs, transform=transform, nodata=float("nan"),
        compress="deflate", predictor=3, tiled=True,
    ) as dataset:
        dataset.write(data.astype(np.float32), 1)
    return path


__all__ = [
    "Frame", "MosaicError", "Scan", "build", "drift_trend", "find_frames", "frame_time",
    "ground_around", "leaves_of",
]
