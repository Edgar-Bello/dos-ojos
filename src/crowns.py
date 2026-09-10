"""Detection: find the units that later steps measure.

Which unit is right depends on the crop, so this module offers three methods.

``rows``
    Primary for sorghum and cane. Individual plants in a 0.76 m row sit about
    15 cm apart, which is three pixels at 5 cm and not separable. The row
    segment is the smallest thing that can honestly be measured, so rows are
    found from the canopy model's own periodicity and cut into fixed lengths.

``watershed``
    For orchards and anything with discrete crowns. Local maxima in the canopy
    seed a marker-controlled watershed, giving one polygon per crown.

``deepforest``
    A pretrained RGB crown detector, for citrus. Optional: it pulls PyTorch, and
    it was trained on forest canopy, so it is the wrong tool for row crops.

Every method returns polygons with stable ids and a CRS, so everything
downstream is indifferent to which one produced them.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Sequence

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Polygon, box
from shapely.ops import transform as shapely_transform

log = logging.getLogger(__name__)

METHODS = ("rows", "watershed", "deepforest")

#: Row spacings worth searching between, in metres. Narrow enough to exclude the
#: whole-field trend, wide enough for anything from drilled grain to cane.
DEFAULT_MIN_SPACING_M = 0.30
DEFAULT_MAX_SPACING_M = 3.00

#: Length of one measured piece of row. Short enough to localise a gap, long
#: enough that a single noisy pixel does not dominate the segment.
DEFAULT_SEGMENT_M = 2.0

#: Below this the periodic peak is not convincingly a planting pattern.
#:
#: Strength is the peak over the 99th percentile of the searched band, not over
#: its median. Measured on known inputs, the median comparison cannot tell rows
#: from a merely lumpy canopy: clean rows score 814000 but a smooth random field
#: with no row structure at all still scores 340, which would be reported as
#: confident. Against the 99th percentile the same cases score 404 and 4.4.
#:
#: Reference values: clean rows 404, rows under heavy noise 42, a lumpy canopy
#: 4.4, white noise 1.4. Ten sits in the gap.
MIN_ROW_STRENGTH = 10.0

#: Canopy under this height is ground, not crown, for watershed seeding.
DEFAULT_MIN_CROWN_HEIGHT_M = 0.5


class DetectionError(RuntimeError):
    """Raised when no usable units could be detected."""


@dataclass(frozen=True)
class RowGeometry:
    """The planting pattern recovered from a canopy model."""

    spacing_m: float
    direction_deg: float          # direction the rows run, in grid coordinates
    phase_m: float                # offset of the first ridge along the across axis
    strength: float               # peak height over the surrounding spectrum

    @property
    def confident(self) -> bool:
        """True when the periodic signal is clearly a planting pattern."""
        return self.strength >= MIN_ROW_STRENGTH

    @property
    def across_deg(self) -> float:
        """Direction perpendicular to the rows."""
        return (self.direction_deg + 90.0) % 180.0


# --------------------------------------------------------------------------- #
# Row geometry
# --------------------------------------------------------------------------- #


def estimate_row_geometry(
    chm: np.ndarray,
    resolution_m: float,
    *,
    min_spacing_m: float = DEFAULT_MIN_SPACING_M,
    max_spacing_m: float = DEFAULT_MAX_SPACING_M,
) -> RowGeometry:
    """Recover row spacing and direction from the canopy model's periodicity.

    Planted rows make the canopy periodic in the direction across them, which is
    a single bright peak in the two-dimensional power spectrum. The peak's angle
    gives the across-row direction and its frequency gives the spacing, both
    without needing to trace any individual row.

    A peak always exists, so the returned strength is what says whether to
    believe it. See :data:`MIN_ROW_STRENGTH` for how it is measured and why.

    Raises:
        DetectionError: if no periodic signal exists in the searched band.
    """
    data = np.where(np.isfinite(chm), chm, 0.0).astype(np.float64)
    data = data - data.mean()
    if not np.any(data):
        raise DetectionError("the canopy model is flat; there is no pattern to find")

    # A window stops the array edges from ringing across the whole spectrum.
    window = np.hanning(data.shape[0])[:, None] * np.hanning(data.shape[1])[None, :]
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(data * window)))

    freq_y = np.fft.fftshift(np.fft.fftfreq(data.shape[0], d=resolution_m))
    freq_x = np.fft.fftshift(np.fft.fftfreq(data.shape[1], d=resolution_m))
    grid_y, grid_x = np.meshgrid(freq_y, freq_x, indexing="ij")
    radius = np.hypot(grid_x, grid_y)

    band = (radius >= 1.0 / max_spacing_m) & (radius <= 1.0 / min_spacing_m)
    if not band.any():
        raise DetectionError(
            f"no frequencies between {min_spacing_m} m and {max_spacing_m} m fit "
            "in this raster; widen the spacing range or use a larger extent"
        )

    in_band = spectrum[band]
    peak_index = np.unravel_index(np.argmax(np.where(band, spectrum, -np.inf)), spectrum.shape)
    peak_x, peak_y = float(grid_x[peak_index]), float(grid_y[peak_index])
    peak_value = float(spectrum[peak_index])

    spacing = 1.0 / math.hypot(peak_x, peak_y)
    across_deg = math.degrees(math.atan2(peak_y, peak_x)) % 180.0
    direction_deg = (across_deg + 90.0) % 180.0
    reference = float(np.percentile(in_band, 99))
    strength = peak_value / reference if reference > 0 else 0.0

    geometry = RowGeometry(
        spacing_m=spacing, direction_deg=direction_deg,
        phase_m=0.0, strength=strength,
    )
    geometry = _with_phase(chm, resolution_m, geometry)

    log.info(
        "rows: %.3f m apart, running at %.1f deg, signal strength %.1f",
        geometry.spacing_m, geometry.direction_deg, geometry.strength,
    )
    if not geometry.confident:
        log.warning(
            "the periodic signal is weak (%.1f, want %.1f+). This may not be a "
            "row crop, the rows may be obscured, or the canopy may have closed "
            "over between them.", geometry.strength, MIN_ROW_STRENGTH,
        )
    return geometry


def _across_coordinates(shape: tuple[int, int], resolution_m: float, across_deg: float):
    """Distance of every pixel along the across-row axis, in metres."""
    rows, cols = shape
    y, x = np.mgrid[0:rows, 0:cols].astype(np.float64)
    theta = math.radians(across_deg)
    return (x * math.cos(theta) + y * math.sin(theta)) * resolution_m


def _with_phase(
    chm: np.ndarray, resolution_m: float, geometry: RowGeometry
) -> RowGeometry:
    """Find where the ridges actually sit, so generated lines land on rows.

    Without this the spacing and angle would be right but every line could fall
    in the furrow between two rows, and every measurement with it.
    """
    across = _across_coordinates(chm.shape, resolution_m, geometry.across_deg)
    valid = np.isfinite(chm)
    if not valid.any():
        return geometry

    folded = np.mod(across[valid], geometry.spacing_m)
    heights = chm[valid]
    n_bins = 36
    bins = np.clip((folded / geometry.spacing_m * n_bins).astype(int), 0, n_bins - 1)
    profile = np.bincount(bins, weights=heights, minlength=n_bins)
    counts = np.bincount(bins, minlength=n_bins)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_profile = np.where(counts > 0, profile / np.maximum(counts, 1), 0.0)

    phase = float(np.argmax(mean_profile)) / n_bins * geometry.spacing_m
    return RowGeometry(
        spacing_m=geometry.spacing_m, direction_deg=geometry.direction_deg,
        phase_m=phase, strength=geometry.strength,
    )


# --------------------------------------------------------------------------- #
# Row segments
# --------------------------------------------------------------------------- #


def build_row_segments(
    chm: np.ndarray,
    resolution_m: float,
    geometry: RowGeometry,
    *,
    segment_m: float = DEFAULT_SEGMENT_M,
    width_m: float | None = None,
) -> list[tuple[int, int, Polygon]]:
    """Cut the field into row-aligned rectangles, in grid coordinates.

    Returns ``(row_index, segment_index, polygon)`` with polygons in pixel
    units; the caller converts them to world coordinates using the raster's
    transform.
    """
    rows, cols = chm.shape
    width_m = width_m if width_m is not None else geometry.spacing_m
    extent = box(0, 0, cols * resolution_m, rows * resolution_m)

    theta = math.radians(geometry.direction_deg)
    along = np.array([math.cos(theta), math.sin(theta)])
    across = np.array([-math.sin(theta), math.cos(theta)])

    corners = np.array(extent.exterior.coords)
    across_range = corners @ across
    along_range = corners @ along

    segments: list[tuple[int, int, Polygon]] = []
    first = math.floor((across_range.min() - geometry.phase_m) / geometry.spacing_m)
    last = math.ceil((across_range.max() - geometry.phase_m) / geometry.spacing_m)

    for row_index in range(first, last + 1):
        centre = geometry.phase_m + row_index * geometry.spacing_m
        n_segments = max(1, int((along_range.max() - along_range.min()) // segment_m))
        for segment_index in range(n_segments):
            start = along_range.min() + segment_index * segment_m
            polygon = _segment_polygon(centre, start, segment_m, width_m, along, across)
            clipped = polygon.intersection(extent)
            if clipped.is_empty or clipped.area < (segment_m * width_m * 0.2):
                continue
            if clipped.geom_type != "Polygon":
                continue
            segments.append((row_index - first, segment_index, clipped))

    if not segments:
        raise DetectionError(
            "no row segments fell inside the canopy model; check the detected "
            f"spacing of {geometry.spacing_m:.2f} m looks right for this crop"
        )
    log.info("built %d row segment(s)", len(segments))
    return segments


def _segment_polygon(
    centre: float,
    start: float,
    length: float,
    width: float,
    along: np.ndarray,
    across: np.ndarray,
) -> Polygon:
    """One row-aligned rectangle, from its centreline offset and start."""
    half = width / 2.0
    corners = [
        along * start + across * (centre - half),
        along * (start + length) + across * (centre - half),
        along * (start + length) + across * (centre + half),
        along * start + across * (centre + half),
    ]
    return Polygon([tuple(point) for point in corners])


def row_centreline(
    geometry: RowGeometry, row_index: int, start: float, length: float
) -> LineString:
    """The centreline of one segment, useful for drawing and for gap analysis."""
    theta = math.radians(geometry.direction_deg)
    along = np.array([math.cos(theta), math.sin(theta)])
    across = np.array([-math.sin(theta), math.cos(theta)])
    centre = geometry.phase_m + row_index * geometry.spacing_m
    return LineString([
        tuple(along * start + across * centre),
        tuple(along * (start + length) + across * centre),
    ])


# --------------------------------------------------------------------------- #
# Watershed crowns
# --------------------------------------------------------------------------- #


def detect_crowns_watershed(
    chm: np.ndarray,
    resolution_m: float,
    *,
    min_height_m: float = DEFAULT_MIN_CROWN_HEIGHT_M,
    min_distance_m: float = 1.5,
    smooth_m: float = 0.15,
) -> list[tuple[int, Polygon]]:
    """Segment discrete crowns by marker-controlled watershed on the canopy.

    Each local maximum taller than ``min_height_m`` seeds one crown, and the
    watershed grows those seeds downhill until they meet. Returns polygons in
    grid coordinates.
    """
    from scipy import ndimage
    from skimage.feature import peak_local_max
    from skimage.measure import find_contours
    from skimage.segmentation import watershed

    heights = np.where(np.isfinite(chm), chm, 0.0)
    sigma_px = max(smooth_m / resolution_m, 0.5)
    smoothed = ndimage.gaussian_filter(heights, sigma_px)

    canopy = smoothed >= min_height_m
    if not canopy.any():
        raise DetectionError(
            f"nothing reaches {min_height_m} m in this canopy model; lower "
            "--min-height or check the model is not empty"
        )

    min_distance_px = max(int(round(min_distance_m / resolution_m)), 1)
    peaks = peak_local_max(
        smoothed, min_distance=min_distance_px, labels=canopy, exclude_border=False
    )
    if peaks.size == 0:
        raise DetectionError(
            "no canopy peaks were found; try a smaller --min-distance"
        )

    markers = np.zeros(smoothed.shape, dtype=np.int32)
    markers[tuple(peaks.T)] = np.arange(1, len(peaks) + 1)
    labels = watershed(-smoothed, markers, mask=canopy)

    crowns: list[tuple[int, Polygon]] = []
    for label in range(1, labels.max() + 1):
        mask = labels == label
        if mask.sum() < 4:
            continue
        polygon = _mask_to_polygon(mask, resolution_m)
        if polygon is not None:
            crowns.append((label, polygon))

    if not crowns:
        raise DetectionError("watershed produced no usable crown polygons")
    log.info("watershed found %d crown(s) from %d peak(s)", len(crowns), len(peaks))
    return crowns


def _mask_to_polygon(mask: np.ndarray, resolution_m: float) -> Polygon | None:
    """Trace the outline of a labelled region into a simplified polygon."""
    from skimage.measure import find_contours

    padded = np.pad(mask.astype(float), 1)
    contours = find_contours(padded, 0.5)
    if not contours:
        return None
    longest = max(contours, key=len)
    if len(longest) < 4:
        return None
    # find_contours yields (row, col); grid coordinates are (x, y) = (col, row).
    points = [((c - 1) * resolution_m, (r - 1) * resolution_m) for r, c in longest]
    polygon = Polygon(points).buffer(0)
    if polygon.is_empty or polygon.geom_type != "Polygon":
        return None
    return polygon.simplify(resolution_m * 0.5)


# --------------------------------------------------------------------------- #
# deepforest
# --------------------------------------------------------------------------- #


def detect_crowns_deepforest(
    ortho_path,
    *,
    tile_px: int = 1500,
    overlap: float = 0.15,
    iou_threshold: float = 0.4,
):
    """Run the pretrained deepforest crown detector over an orthophoto.

    Optional, and deliberately not a default. It pulls PyTorch, and its
    pretrained weights come from forest canopy imagery, so it detects tree crowns
    well and row crops not at all.

    Raises:
        DetectionError: if deepforest is not installed, with how to install it.
    """
    try:
        from deepforest import main as deepforest_main
    except ImportError as exc:
        raise DetectionError(
            "deepforest is not installed. It is optional because it pulls "
            "PyTorch, roughly 2.5 GB, and is the wrong detector for row crops. "
            "For an orchard: pip install deepforest"
        ) from exc

    model = deepforest_main.deepforest()
    model.use_release()
    boxes = model.predict_tile(
        str(ortho_path), patch_size=tile_px,
        patch_overlap=overlap, iou_threshold=iou_threshold,
    )
    if boxes is None or boxes.empty:
        raise DetectionError("deepforest found no crowns in this orthophoto")
    log.info("deepforest found %d crown(s)", len(boxes))
    return boxes


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def to_geodataframe(
    units: Sequence[tuple],
    transform,
    crs,
    *,
    method: str,
    geometry: RowGeometry | None = None,
) -> gpd.GeoDataFrame:
    """Turn grid-space detections into a georeferenced frame with stable ids.

    Ids are assigned from position rather than from detection order, so the same
    field detected twice yields the same id for the same piece of ground.
    """
    def to_world(x, y):
        world_x, world_y = transform * (x / abs(transform.a), y / abs(transform.e))
        return world_x, world_y

    records = []
    for unit in units:
        if method == "rows":
            row_index, segment_index, polygon = unit
            unit_id = f"r{row_index:03d}s{segment_index:03d}"
            extra = {"row": row_index, "segment": segment_index}
        else:
            label, polygon = unit
            unit_id = f"c{label:05d}"
            extra = {"label": label}
        records.append({
            "unit_id": unit_id,
            "method": method,
            **extra,
            "geometry": shapely_transform(to_world, polygon),
        })

    frame = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
    frame = frame.sort_values("unit_id").reset_index(drop=True)
    if geometry is not None:
        frame.attrs["row_spacing_m"] = geometry.spacing_m
        frame.attrs["row_direction_deg"] = geometry.direction_deg
        frame.attrs["row_strength"] = geometry.strength
    return frame


def clip_units(frame: gpd.GeoDataFrame, field, *, min_fraction: float = 0.5):
    """Drop units that fall mostly outside the field outline.

    A segment clipped to a sliver at the field edge would report a canopy volume
    that says more about the boundary than about the crop.
    """
    from pyproj import Transformer
    from shapely.ops import transform as shapely_transform

    to_target = Transformer.from_crs("EPSG:4326", frame.crs, always_xy=True).transform
    field_projected = shapely_transform(to_target, field)

    original_area = frame.geometry.area
    clipped = frame.copy()
    clipped["geometry"] = frame.geometry.intersection(field_projected)
    keep = (clipped.geometry.area / original_area.replace(0, np.nan)) >= min_fraction
    kept = clipped[keep & ~clipped.geometry.is_empty].reset_index(drop=True)
    log.info("kept %d of %d unit(s) inside the field", len(kept), len(frame))
    return kept
